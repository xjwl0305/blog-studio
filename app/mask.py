"""자격증명·소속 식별자 마스킹.

블로그 초안이 공개 인터넷으로 나가므로, 생성 전(입력)과 생성 후(초안) 양쪽에서
가린다. a1-bot-server의 logging_setup/session_export에서 검증된 규칙을 옮겨왔다.
자기완결적으로 두어 이 레포만으로 동작하게 한다.

두 단계로 나뉜다:
  mask()        — 확실한 것을 치환한다 (키·토큰·경로 사용자명·IP·사용자 지정 단어)
  scan_secrets() — 자동 치환이 위험한 것을 '경고'만 한다 (채팅으로 친 비밀번호 등)
"""

from __future__ import annotations

import re
from pathlib import Path

# 쿼리스트링에 담긴 자격증명
_QS_SECRET = re.compile(
    r"(?i)\b(serviceKey|apiKey|api_key|access_token|token|key|passkey)=([^&\s\"'<>]{8,})"
)
# 디스코드/슬랙류 봇 토큰 모양
_BOT_TOKEN = re.compile(r"\b[\w-]{24,28}\.[\w-]{6}\.[\w-]{27,40}\b")
# UUID
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# 소속·업무 식별자 (블로그엔 불필요)
_IDENTIFIERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[a-zA-Z]\d{6,}\b"), "<user>"),          # 사번 형태
    (re.compile(r"/Users/[^/\s]+"), "/Users/<user>"),
    (re.compile(r"/home/[^/\s]+"), "/home/<user>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"https?://[^\s\"'<>)]*\.(?:local|internal|corp)[^\s\"'<>)]*"),
     "<internal-url>"),
]

# 자격증명으로 의심되는 줄 — 자동 치환 대신 경고
_SECRET_HINTS = [
    re.compile(r"(?i)\b(?:pass(?:word|wd)?|비밀번호|암호|pw)\s*[:=/]\s*\S{4,}"),
    re.compile(r"(?i)\b(?:passkey|apikey|api[_-]?key|secret|token)\s*[:=]\s*\S{8,}"),
    re.compile(r"(?<![\w./-])[a-zA-Z][\w.-]{2,15}\s?/\s?[\w.-]{4,20}[!@#$%^&*]+(?![\w/])"),
    re.compile(r"\|\s*\w*admin\w*\s*\|\s*\S{4,}\s*\|"),
]

_TERMS_FILE = Path(__file__).resolve().parent.parent / "redact_terms.txt"


def extra_terms() -> list[str]:
    """사용자가 지정한 추가 마스킹 단어(회사명·제품명·사내 시스템명·노드명 등)."""
    if not _TERMS_FILE.exists():
        return []
    out = [
        ln.strip()
        for ln in _TERMS_FILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return sorted(set(out), key=len, reverse=True)


def mask(text: str, terms: list[str] | None = None) -> str:
    if not text:
        return ""
    if terms is None:
        terms = extra_terms()
    text = _QS_SECRET.sub(r"\1=<redacted>", text)
    text = _BOT_TOKEN.sub("<redacted-token>", text)
    text = _UUID.sub("<redacted-id>", text)
    for pattern, repl in _IDENTIFIERS:
        text = pattern.sub(repl, text)
    for t in terms:
        if t:
            text = re.sub(re.escape(t), "<redacted-term>", text, flags=re.IGNORECASE)
    return text


def scan_secrets(text: str) -> list[str]:
    """공개 전 사람이 확인해야 할, 자격증명 의심 줄 목록."""
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        for pat in _SECRET_HINTS:
            if pat.search(line):
                found.append(f"L{i}: {line.strip()[:100]}")
                break
    return found
