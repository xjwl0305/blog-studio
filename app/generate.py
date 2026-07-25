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

_TECH = """너는 기술 블로그 초안을 쓰는 작가다. 아래 재료로 한국어 기술 블로그 글을 써라.

작성 원칙:
- 요즘 기술 블로그 트렌드: 상단 3줄 요약, '~다' 체 1인칭, 질문형 소제목,
  실제 로그/명령어는 코드블록, 표로 비교, 이모지는 절제
- **증상 → 가설 → 검증 → 원인 → 해결 → 배운 것** 서사. 시행착오를 숨기지 마라
- 지어내지 마라. 재료에 있는 수치·명령어만 쓴다. 없는 건 "확인 필요"로 남겨라
- 회사명·사번·사내 IP·내부 URL·서버 번호·계정/비밀번호는 절대 쓰지 마라.
  일반화하라 (예: "42번 서버" → "컨트롤 플레인 노드")
- 검색 유입을 고려해 제목·소제목에 기술 키워드를 자연스럽게 넣어라

출력: 마크다운 본문만. 맨 위에 '# 제목' 한 줄로 시작하라."""

_TRAVEL = """너는 여행 리뷰 블로그를 쓰는 작가다. 아래 재료(사진·메모)로 한국어 여행 후기를 써라.

작성 원칙:
- 따뜻하고 생생한 1인칭 후기체. 그날의 분위기·냄새·소리·맛까지 감각적으로 살려라
- 독자가 실제로 가고 싶어지고, 가면 도움되게 써라: **위치·가는 법·비용·소요시간·팁**을
  자연스럽게 녹여라 (딱딱한 표 대신 문장 속에)
- 솔직하게. 좋았던 것만이 아니라 아쉬웠던 점, 웨이팅, 주의할 점도 적어라
- 사진이 있으면 그 장면을 묘사하고, 사진이 들어갈 위치를 `[사진: 무엇]` 으로 표시하라
- 지어내지 마라. 재료에 없는 가격·정보는 "확인 필요"로 남겨라
- 검색 유입을 위해 제목·소제목에 지명·장소명·"후기" 같은 키워드를 넣어라
- 전화번호·집주소·타인의 실명 등 사생활 정보는 쓰지 마라

출력: 마크다운 본문만. 맨 위에 '# 제목' 한 줄로 시작하라."""

_DAILY = """너는 일상 블로그를 쓰는 작가다. 아래 재료(사진·메모)로 한국어 일상 글을 써라.

작성 원칙:
- 편안하고 진솔한 일기체. 잘 쓰려 애쓰지 말고, 그날 느낀 감정과 생각을 자연스럽게
- 공감이 핵심이다. 소소한 디테일에서 독자가 자기 이야기처럼 느끼게 하라
- 억지 교훈이나 과장은 빼라. 담담하게, 그러나 진심이 보이게
- 사진이 있으면 그 순간을 짧게 묘사하고, 위치를 `[사진: 무엇]` 으로 표시하라
- 전화번호·집주소·타인의 실명 등 사생활 정보는 쓰지 마라

출력: 마크다운 본문만. 맨 위에 '# 제목' 한 줄로 시작하라."""

_REVIEW = """너는 리뷰 블로그를 쓰는 작가다. 아래 재료(사진·메모)로 한국어 리뷰(맛집·제품·장소)를 써라.

작성 원칙:
- 방문/사용 계기 → 무엇을(메뉴·제품·구성) → 가격 → 장점 → 아쉬운 점 → 추천 대상 순서
- **솔직함이 신뢰다.** 단점을 숨기지 마라. 별점이나 한줄평으로 총평을 명확히
- 실제 도움되게: 위치·가격·영업시간·재구매/재방문 의사를 구체적으로
- 사진이 있으면 그 장면을 묘사하고 `[사진: 무엇]` 으로 위치를 표시하라
- 지어내지 마라. 재료에 없는 가격·정보는 "확인 필요"로 남겨라
- 검색 유입을 위해 제목에 상호·제품명·"후기"·"내돈내산" 같은 키워드를 넣어라
- 전화번호·타인의 실명 등 사생활 정보는 쓰지 마라

출력: 마크다운 본문만. 맨 위에 '# 제목' 한 줄로 시작하라."""

SYSTEM_PROMPTS = {"tech": _TECH, "travel": _TRAVEL, "daily": _DAILY, "review": _REVIEW}


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


def build_prompt(content: str, image_paths: list[Path], material: str = "", blog_type: str = "tech") -> str:
    system = SYSTEM_PROMPTS.get(blog_type, _TECH)
    parts = [system, "\n---\n\n# 재료 (사용자 제공 내용·히스토리)\n", mask(content)]
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
    blog_type: str = "tech",
    on_progress=None,
    handle: Handle | None = None,
) -> Draft:
    prompt = build_prompt(content, image_paths, material, blog_type)
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
        # 스트림 JSON 한 줄이 기본 64KB를 넘으면 리더가 터진다. 이미지를 Read하면
        # tool_result가 base64로 한 줄에 실려 이미지 하나가 수 MB가 된다.
        # 업로드 상한 25MB → base64 약 34MB이므로 여유를 둬 48MB로 잡는다.
        limit=48 * 1024 * 1024,
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
