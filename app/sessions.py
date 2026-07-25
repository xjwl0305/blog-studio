"""동기화된 작업 세션을 블로그 재료로 쓰기 위한 목록·추출.

다른 장비에서 rsync로 넘어온 세션(~/claude-sync/)을 읽어, 사용자가 대시보드에서
고르면 마스킹된 재료로 뽑아 generate에 넘긴다.
a1-bot-server의 session_export 로직을 자기완결적으로 이식했다.
"""

from __future__ import annotations

import glob
import hashlib
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


# 세션 인덱스 캐시. 매 검색마다 67MB를 다시 읽지 않도록 mtime으로 무효화한다.
# 값: {path: {meta..., "haystack": 소문자 전체 프롬프트, "prompts_list": [...]}}
_INDEX: dict[str, dict] = {}


def _sid(path: str) -> str:
    """경로를 불투명 ID로. 원본 경로엔 사번 등이 박혀 있어 브라우저로 내보내지 않는다."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def _index_one(f: str) -> dict:
    p = Path(f)
    prompts = []
    for r in _iter(p):
        if r.get("type") == "last-prompt":
            t = " ".join((r.get("lastPrompt") or "").split())
            if t and t not in prompts:
                prompts.append(t)
    proj = mask(p.parent.name.replace("-Users-", "").replace("-home-", ""))
    masked_prompts = [mask(t) for t in prompts]
    return {
        "id": _sid(f),        # 브라우저로 나가는 불투명 ID
        "path": f,            # 서버 내부용 실제 경로 (외부로 안 나감)
        "project": proj[:48],
        "prompts": len(prompts),
        "size_kb": p.stat().st_size // 1024,
        "title": (masked_prompts[0] if masked_prompts else "")[:70],
        "mtime": int(p.stat().st_mtime),
        # 검색용: 프로젝트명 + 모든 프롬프트를 소문자로 합친 건초더미.
        "haystack": (proj + " " + " \n".join(masked_prompts)).lower(),
        "prompts_list": masked_prompts,
    }


def _refresh_index() -> None:
    seen = set()
    for f in glob.glob(str(SYNC_ROOT / "*" / "*" / "*.jsonl")):
        seen.add(f)
        mt = int(Path(f).stat().st_mtime)
        if f not in _INDEX or _INDEX[f]["mtime"] != mt:
            entry = _index_one(f)
            if entry["prompts"] > 0:
                _INDEX[f] = entry
    for gone in set(_INDEX) - seen:  # 지워진 세션 제거
        _INDEX.pop(gone, None)


def _meta(entry: dict) -> dict:
    return {k: entry[k] for k in ("id", "project", "prompts", "size_kb", "title", "mtime")}


def list_sessions() -> list[dict]:
    """동기화된 세션 목록 (최신순)."""
    _refresh_index()
    out = [_meta(e) for e in _INDEX.values()]
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def search_sessions(query: str) -> list[dict]:
    """키워드로 세션을 찾아 점수순으로 돌려준다. 빈 쿼리면 최신순 전체.

    점수 = 각 검색어가 건초더미에 등장한 횟수의 합. 매칭된 프롬프트 조각을 함께 준다.
    """
    _refresh_index()
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return list_sessions()

    scored = []
    for e in _INDEX.values():
        hay = e["haystack"]
        score = sum(hay.count(t) for t in terms)
        if score == 0:
            continue
        # 왜 매칭됐는지 보여줄 스니펫: 검색어가 든 첫 프롬프트.
        snippet = next(
            (pp for pp in e["prompts_list"] if any(t in pp.lower() for t in terms)),
            e["title"],
        )
        m = _meta(e)
        m["score"] = score
        m["snippet"] = snippet[:80]
        scored.append(m)
    scored.sort(key=lambda x: (-x["score"], -x["mtime"]))
    return scored


def path_for(sid: str) -> str:
    """불투명 ID → 실제 경로. 인덱스에 있는 것만 허용한다."""
    _refresh_index()
    for entry in _INDEX.values():
        if entry["id"] == sid:
            return entry["path"]
    return ""


def extract(sid: str) -> str:
    """세션(불투명 ID)에서 마스킹된 재료를 뽑는다.

    ID→경로 매핑은 인덱스에 있는 것만 허용하므로, 임의 파일 읽기가 원천 차단된다.
    """
    session_path = path_for(sid)
    if not session_path:
        return ""
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
