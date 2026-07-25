"""동기화된 작업 세션을 블로그 재료로 쓰기 위한 목록·추출.

다른 장비에서 rsync로 넘어온 세션(~/claude-sync/)을 읽어, 사용자가 대시보드에서
고르면 마스킹된 재료로 뽑아 generate에 넘긴다.
a1-bot-server의 session_export 로직을 자기완결적으로 이식했다.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

from .mask import mask

SYNC_ROOT = Path("/home/ubuntu/claude-sync")

# 재료로 넘길 때 tool_result 하나에서 남길 최대 길이.
_RESULT_CAP = 600
# 세션 하나에서 뽑는 재료의 총 상한 (프롬프트 폭증 방지).
_MATERIAL_CAP = 40000


def _iter(path: Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _first_prompt(path: Path) -> str:
    for rec in _iter(path):
        if rec.get("type") == "last-prompt":
            p = " ".join((rec.get("lastPrompt") or "").split())
            if p:
                return p
    return ""


def list_sessions() -> list[dict]:
    """동기화된 세션 목록. 각 항목: id, project, prompts, size_kb, title, mtime."""
    out = []
    for f in glob.glob(str(SYNC_ROOT / "*" / "*" / "*.jsonl")):
        p = Path(f)
        proj = p.parent.name.replace("-Users-", "").replace("-home-", "")
        # 사번 등 식별자가 프로젝트명 경로에 있으므로 마스킹해 표시.
        proj = mask(proj)
        n_prompt = sum(1 for r in _iter(p) if r.get("type") == "last-prompt")
        if n_prompt == 0:
            continue
        out.append({
            "id": f,  # 전체 경로를 id로 (선택 시 그대로 넘김)
            "project": proj[:48],
            "prompts": n_prompt,
            "size_kb": p.stat().st_size // 1024,
            "title": mask(_first_prompt(p))[:70],
            "mtime": int(p.stat().st_mtime),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def extract(session_path: str) -> str:
    """세션에서 마스킹된 재료를 뽑는다. 경로 검증으로 임의 파일 읽기를 막는다."""
    p = Path(session_path).resolve()
    if SYNC_ROOT not in p.parents or p.suffix != ".jsonl" or not p.exists():
        return ""

    lines: list[str] = []
    total = 0
    for rec in _iter(p):
        t = rec.get("type")
        if t == "user":
            c = rec.get("message", {}).get("content")
            if isinstance(c, str):
                s = c.strip()
                if s and not s.startswith("아래는 최근"):
                    lines.append(f"\n## 사용자\n{mask(s)}")
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        body = b.get("content")
                        if isinstance(body, list):
                            body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                        body = mask(str(body or "")).strip()
                        if body:
                            lines.append(f"```\n{body[:_RESULT_CAP]}\n```")
        elif t == "assistant":
            for b in rec.get("message", {}).get("content", []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    txt = mask(b.get("text", "")).strip()
                    if txt:
                        lines.append(f"\n### 어시스턴트\n{txt}")
                elif b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    ti = b.get("input", {}) or {}
                    if name == "Bash":
                        lines.append(mask(f"$ {' '.join(str(ti.get('command','')).split())[:200]}"))
                    elif name in ("Edit", "Write", "Read"):
                        # 경로에 사번·홈 사용자명이 박혀 있어 반드시 마스킹한다.
                        lines.append(mask(f"[{name}] {ti.get('file_path','')}"))
        if lines:
            total = sum(len(x) for x in lines)
            if total > _MATERIAL_CAP:
                lines.append("\n\n(재료가 길어 이후는 생략)")
                break
    return "\n".join(lines)
