"""참고 블로그 링크에서 본문·문체를 읽어온다.

소유자가 말투 참고용으로 네이버 블로그 링크를 주면, 그 본문을 뽑아
글 생성 시 '이 말투를 따르라'는 문체 참고 자료로 넘긴다.

네이버 블로그는 본문이 iframe/JS로 렌더링되지만, 모바일 PostView 엔드포인트
(m.blog.naver.com/PostView.naver)는 서버사이드 HTML로 본문을 준다.
그래서 헤드리스 브라우저 없이 가벼운 HTTP로 읽을 수 있다.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.request

log = logging.getLogger("blog.fetch_ref")

_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# 본문 앞뒤로 붙는 네이버 UI 상투구 — 제거 대상.
_CHROME = [
    "네이버 블로그 본문 바로가기", "블로그 카테고리 이동", "이웃추가",
    "본문 기타 기능", "본문 폰트 크기", "작게 보기", "크게 보기", "공유하기",
    "복사 신고하기", "댓글", "공감", "이 블로그", "카테고리 글",
    "맨위로", "PC버전으로 보기", "저작권", "무단전재",
]


def _naver_ids(url: str) -> tuple[str, str] | None:
    """네이버 블로그 URL에서 blogId, logNo 추출. 아니면 None."""
    if "blog.naver.com" not in url:
        return None
    # https://blog.naver.com/<id>/<logNo>  또는  ?blogId=..&logNo=..
    m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    bid = re.search(r"[?&]blogId=([^&]+)", url)
    log_ = re.search(r"[?&]logNo=(\d+)", url)
    if bid and log_:
        return bid.group(1), log_.group(1)
    return None


def _get(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip_html(raw: str) -> str:
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.S)
    # 문단 경계를 살리려고 블록 태그를 줄바꿈으로
    raw = re.sub(r"</(p|div|br|h[1-6]|li)>", "\n", raw, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    lines = []
    for ln in txt.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if len(ln) < 10 or not re.search(r"[가-힣]", ln):
            continue
        if any(c in ln for c in _CHROME):
            continue
        lines.append(ln)
    # 중복 제거(순서 유지)
    seen, out = set(), []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "\n".join(out)


def fetch_reference(url: str, cap: int = 4000) -> tuple[str, str]:
    """참고 글 본문을 돌려준다. (본문텍스트, 오류메시지).

    본문이 cap자를 넘으면 앞부분만(문체 파악엔 충분).
    """
    url = url.strip()
    try:
        ids = _naver_ids(url)
        if ids:
            bid, logno = ids
            # 모바일 PostView가 서버사이드 본문을 준다.
            raw = _get(f"https://m.blog.naver.com/PostView.naver?blogId={bid}&logNo={logno}")
        else:
            # 그 외 블로그는 일반 HTTP로 시도 (티스토리 등은 대체로 됨).
            raw = _get(url)
    except Exception as exc:
        log.warning("참고 글 조회 실패: %s (%s)", url, type(exc).__name__)
        return "", f"링크를 읽지 못했습니다 ({type(exc).__name__})."

    body = _strip_html(raw)
    if len(body) < 80:
        return "", "본문을 추출하지 못했습니다(비공개 글이거나 형식이 다를 수 있습니다)."
    return body[:cap], ""
