"""블로그 초안 생성 — Claude Code CLI 헤드리스 호출.

a1-bot-server의 claude_runner와 같은 방식(claude -p --output-format json)이지만,
대시보드 용도에 맞게 단순화했다. 스트리밍 없이 완성분만 받는다.
입력(사용자 내용)과 업로드 이미지 경로를 프롬프트로 조립해 넘긴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .mask import mask

log = logging.getLogger("blog.generate")

CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"
WORKDIR = Path("/home/ubuntu/blog-studio")
TIMEOUT = 600.0

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


def build_prompt(content: str, image_paths: list[Path]) -> str:
    parts = [_SYSTEM, "\n---\n\n# 재료 (사용자 제공 내용·히스토리)\n", mask(content)]
    if image_paths:
        listing = "\n".join(f"- {p}" for p in image_paths)
        parts.append(
            "\n\n# 첨부 이미지\n"
            "아래 이미지를 Read 도구로 직접 열어 내용을 파악하고 글에 반영하라 "
            "(스크린샷·다이어그램 등):\n" + listing
        )
    return "\n".join(parts)


async def generate(content: str, image_paths: list[Path]) -> Draft:
    prompt = build_prompt(content, image_paths)
    argv = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    log.info("초안 생성 시작: 내용 %d자, 이미지 %d장", len(content), len(image_paths))
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=WORKDIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return Draft("", 0.0, 0, error=f"{int(TIMEOUT)}초 내에 끝나지 않았습니다.")

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[:300]
        return Draft("", 0.0, 0, error=f"claude 종료코드 {proc.returncode}: {detail}")

    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return Draft("", 0.0, 0, error="claude 출력 파싱 실패")

    if payload.get("is_error"):
        return Draft("", 0.0, 0, error=str(payload.get("result", "알 수 없는 오류"))[:300])

    # 생성 결과에도 마스킹을 한 번 더 건다 (이중 안전).
    text = mask(str(payload.get("result", "")).strip())
    log.info("초안 완료: %d자, $%.4f", len(text), payload.get("total_cost_usd") or 0)
    return Draft(
        text=text,
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        duration_ms=int(payload.get("duration_ms") or 0),
    )
