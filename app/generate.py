"""블로그 초안 생성 — Claude Code CLI 스트리밍 호출.

봇의 claude_runner(streaming)를 대시보드용으로 이식했다. 스트림을 파싱해
진행상황(도구 사용)을 콜백으로 흘리고, 취소·무진행 감시(watchdog)를 지원한다.

무진행 감시가 핵심이다: API 스트림이 중간에 끊겨도(연결은 살아있는데 데이터가 안 옴)
전체 타임아웃(수 분)까지 하염없이 기다리던 문제를 잡는다. N초간 새 이벤트가 없으면
stall로 보고 죽인다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .mask import mask

log = logging.getLogger("blog.generate")

CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"
WORKDIR = Path("/home/ubuntu/blog-studio")

# 전체 상한. 이 안에 못 끝나면 중단.
TIMEOUT = 420.0
# 무진행 감시: 마지막 이벤트 이후 이만큼 새 이벤트가 없으면 stall로 보고 죽인다.
STALL_SECONDS = 75.0

_SYSTEM = """너는 기술 블로그 초안을 쓰는 작가다. 아래 재료로 한국어 기술 블로그 글을 써라.

작성 원칙:
- 요즘 기술 블로그 트렌드: 상단 3줄 요약, '~다' 체 1인칭, 질문형 소제목,
  실제 로그/명령어는 코드블록, 표로 비교, 이모지는 절제
- **증상 → 가설 → 검증 → 원인 → 해결 → 배운 것** 서사. 시행착오를 숨기지 마라
- 지어내지 마라. 재료에 있는 수치·명령어만 쓴다. 없는 건 "확인 필요"로 남겨라
- 회사명·사번·사내 IP·내부 URL·서버 번호·계정/비밀번호는 절대 쓰지 마라.
  일반화하라 (예: "42번 서버" → "컨트롤 플레인 노드")
- 검색 유입을 고려해 제목·소제목에 기술 키워드를 자연스럽게 넣어라

출력: 마크다운 본문만. 맨 위에 '# 제목' 한 줄로 시작하라."""


@dataclass(frozen=True)
class Draft:
    text: str
    cost_usd: float
    duration_ms: int
    error: str | None = None
    cancelled: bool = False


class Handle:
    """실행 중 프로세스를 밖에서 취소하기 위한 손잡이."""

    def __init__(self) -> None:
        self._proc = None
        self.cancelled = False

    def attach(self, proc) -> None:
        self._proc = proc

    def cancel(self) -> None:
        self.cancelled = True
        _kill_tree(self._proc)


def _kill_tree(proc) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def build_prompt(content: str, image_paths: list[Path], material: str = "") -> str:
    parts = [_SYSTEM, "\n---\n\n# 재료 (사용자 제공 내용·히스토리)\n", mask(content)]
    if material:
        parts.append(
            "\n\n# 추가 재료 (선택한 작업 세션에서 추출·마스킹됨)\n"
            "아래는 과거 작업 기록에서 뽑은 것이다. 이 사건을 블로그로 정리하라:\n\n"
            + material
        )
    if image_paths:
        listing = "\n".join(f"- {p}" for p in image_paths)
        parts.append(
            "\n\n# 첨부 이미지\n"
            "아래 이미지를 Read 도구로 직접 열어 내용을 파악하고 글에 반영하라:\n" + listing
        )
    return "\n".join(parts)


def _describe_tool(name: str, ti: dict) -> str:
    if name == "Bash":
        d = ti.get("description") or ti.get("command", "")
    elif name in ("Read", "Write", "Edit"):
        d = ti.get("file_path", "")
    elif name in ("Grep", "Glob"):
        d = ti.get("pattern", "")
    elif name in ("WebFetch", "WebSearch"):
        d = ti.get("url") or ti.get("query", "")
    else:
        d = next((str(v) for v in ti.values() if isinstance(v, str)), "")
    d = " ".join(str(d).split())
    if len(d) > 80:
        d = d[:79] + "…"
    return f"{name}: {d}" if d else name


async def generate(
    content: str,
    image_paths: list[Path],
    material: str = "",
    on_progress=None,
    handle: Handle | None = None,
) -> Draft:
    prompt = build_prompt(content, image_paths, material)
    argv = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
    ]
    log.info("초안 생성 시작: 내용 %d자, 이미지 %d장, 세션재료 %d자",
             len(content), len(image_paths), len(material))
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=WORKDIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if handle is not None:
        handle.attach(proc)

    final: dict | None = None
    last_event = time.monotonic()

    async def pump() -> None:
        nonlocal final, last_event
        assert proc.stdout is not None
        async for raw in proc.stdout:
            last_event = time.monotonic()
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = event.get("type")
            if et == "result":
                final = event
            elif et == "assistant" and on_progress is not None:
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        on_progress(_describe_tool(block.get("name", "?"), block.get("input") or {}))
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        on_progress("✍ 초안 작성 중…")

    async def watchdog() -> None:
        # 무진행 감시. 스트림이 끊겨 이벤트가 안 오면 stall로 보고 중단시킨다.
        while proc.returncode is None:
            await asyncio.sleep(5)
            if time.monotonic() - last_event > STALL_SECONDS:
                log.warning("무진행 %d초 — stall로 판단, 중단", int(STALL_SECONDS))
                _kill_tree(proc)
                return

    wd = asyncio.create_task(watchdog())
    try:
        await asyncio.wait_for(pump(), timeout=TIMEOUT)
        await asyncio.wait_for(proc.wait(), timeout=20)
    except asyncio.TimeoutError:
        _kill_tree(proc)
        await proc.wait()
        wd.cancel()
        return Draft("", 0.0, 0, error=f"{int(TIMEOUT)}초 내에 끝나지 않았습니다.")
    finally:
        wd.cancel()

    if handle is not None and handle.cancelled:
        return Draft("", 0.0, 0, cancelled=True)

    if final is None:
        # 결과 이벤트가 없다 = stall로 죽었거나 비정상 종료.
        detail = ""
        if proc.stderr is not None:
            detail = (await proc.stderr.read()).decode("utf-8", "replace").strip()[:300]
        stalled = time.monotonic() - last_event > STALL_SECONDS - 5
        msg = "응답이 중간에 끊겼습니다(stall). 다시 시도해 주세요." if stalled \
            else f"결과를 받지 못했습니다 (종료코드 {proc.returncode}). {detail}"
        return Draft("", 0.0, 0, error=msg)

    if final.get("is_error"):
        return Draft("", 0.0, 0, error=str(final.get("result", "알 수 없는 오류"))[:300])

    text = mask(str(final.get("result", "")).strip())
    log.info("초안 완료: %d자, $%.4f", len(text), final.get("total_cost_usd") or 0)
    return Draft(
        text=text,
        cost_usd=float(final.get("total_cost_usd") or 0.0),
        duration_ms=int(final.get("duration_ms") or 0),
    )
