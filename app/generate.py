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

# 전체 상한. 이미지가 많으면 오래 걸리므로 넉넉히.
TIMEOUT = 600.0
# 무진행 감시: 응답이 시작된 뒤 이만큼 새 이벤트가 없으면 stall로 보고 죽인다.
# (첫 응답 전 warmup은 이미지 수에 비례해 따로 계산한다 — 아래 generate 참고.)
STALL_SECONDS = 75.0

_TECH = """너는 기술 블로그 초안을 쓰는 작가다. 아래 재료로 한국어 기술 블로그 글을 써라.
문체는 '우아한형제들 기술블로그' 스타일을 따른다.

말투(우아한형제들 스타일):
- **정중한 '~습니다/~합니다'체.** 딱딱한 논문투가 아니라, 동료 개발자에게
  차근차근 설명하듯 친근하게. 그러나 가볍지 않게.
- **독자에게 말을 건다.** "왜 이런 일이 생겼을까요?", "한번 살펴볼까요?",
  "혹시 이런 경험 있으신가요?" 처럼 질문을 던지며 끌고 간다.
- **1인칭 경험 공유.** "저는 처음에 ~라고 생각했습니다", "여기서 한참 헤맸습니다"
  처럼 내가 겪은 과정을 솔직하게. 삽질·착각·당황도 숨기지 않는다.
- **전문 용어는 풀어서 설명한다.** 개념을 처음 보는 사람도 따라오게, 필요하면 비유로.
  "etcd는 쿠버네티스의 상태를 저장하는 DB인데, 쉽게 말하면 ~" 처럼.
- **가벼운 위트는 괜찮지만 과하지 않게.** 이모지는 거의 쓰지 않는다.

작성 원칙:
- 구조: **배경/문제 상황 → 원인을 파고든 과정(시행착오 포함) → 해결 → 회고·배운 점.**
  결과만 나열하지 말고 "어떻게 거기에 도달했는지" 사고 과정을 보여줘라.
- 상단에 이 글이 다루는 문제를 2~3줄로 요약. 실제 로그/명령어는 코드블록, 비교는 표.
- 지어내지 마라. 재료에 있는 수치·명령어만 쓴다. 없는 건 "확인 필요"로 남겨라
- 회사명·사번·사내 IP·내부 URL·서버 번호·계정/비밀번호는 절대 쓰지 마라.
  일반화하라 (예: "42번 서버" → "컨트롤 플레인 노드")
- 검색 유입을 고려해 제목·소제목에 기술 키워드를 자연스럽게 넣어라

출력: 마크다운 본문만. 맨 위에 '# 제목' 한 줄로 시작하라."""

_TRAVEL = """너는 여행 블로그를 쓰는 작가다. 아래 재료(사진·메모)로 한국어 여행기를 써라.
목표는 두 가지다: (1) 담백한 일기체로 그날을 진솔하게 남기고, (2) 읽는 사람에게
실제로 도움되는 정보를 준다. 감성과 정보, 둘 다 놓치지 마라.

문체·톤:
- **담백한 일기체.** 과장·미사여구·화려한 감탄을 빼라. 겪은 대로, 느낀 대로 담담하게.
  실패나 아쉬움도 숨기지 말고 솔직하게 (예: "길을 잘못 들어 한참 헤맸다").
- **이모지는 쓰지 마라.** 정 필요하면 글 전체에 한두 개 이하.
- 존댓말·반말 어느 쪽이든 한 글에선 일관되게. 기본은 '~다' 담담체.
- 자기 감정을 직접 말해도 좋다 ("이래서 사람들이 여기 오면 빠져든다는 걸 알 것 같았다").

구조:
- 시간 순서로 죽 나열하지 말고, **장소별로 섹션을 나눠라.** 각 섹션 제목은
  `## <장소명>` 형식으로 (꺾쇠 포함). 예: `## <에라완 사원>`
- 각 섹션: 그 장소의 사진이 들어갈 자리 `[사진: 무엇]` → 그곳에서 겪은 일과 감상.
- 맨 앞에 짧은 도입(왜 갔는지, 그날의 배경), 맨 뒤에 담담한 마무리.

정보(이게 이 블로그의 목적이다):
- **독자가 따라갈 수 있게 실용 정보를 각 장소에 자연스럽게 넣어라**:
  가는 법(교통·역 이름), 위치, 입장료·가격, 소요시간, 웨이팅, 주의할 점, 꿀팁.
- 정보는 문장 속에 녹이되, 장소 섹션 끝에 **`> 📍 가는 법 / 💰 비용 / 💡 팁`**
  형태의 짧은 정보 한 줄을 인용구로 덧붙여도 좋다 (감성 흐름을 끊지 않는 선에서).
- **지어내지 마라.** 재료(사진·메모)에 없는 가격·시간·교통 정보는 만들지 말고,
  "확인 필요" 또는 아예 생략하라. 틀린 정보가 도움말보다 나쁘다.

기타:
- 검색 유입을 위해 제목·섹션명에 지명·장소명을 넣어라. 제목은 담백하게
  (예: "생애 첫 방콕 여행 - 삼롱시장과 에라완 사원").
- 전화번호·집주소·타인의 실명 등 사생활 정보는 쓰지 마라.

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

# 여행·일상·리뷰 글에 붙이는 "사람처럼 쓰기" 규칙.
# '담백하게'만으론 부족하다. AI 문장의 전형적 패턴을 예시로 콕 집어 금지한다.
_HUMAN_VOICE = """

---

## 사람처럼 써라 (AI 티 나는 문장을 피하는 게 이 글의 성패다)

아래는 AI가 쓴 티가 확 나는 문장들이다. 이런 걸 절대 쓰지 마라:

**1. 과한 감각·시적 묘사를 하지 마라.**
   ✗ "물결마다 주황빛이 부서졌다", "노을이 하늘을 붉게 물들였다"
   ○ "해질 무렵이라 강이 주황색이었다", "노을이 예뻤다"
   → 예쁘게 쓰려고 하지 마라. 본 대로 담담하게.

**2. 대구·병렬(짝 맞추기)을 쓰지 마라. AI가 제일 좋아하는 티다.**
   ✗ "한 명은 사진을 찍고, 한 명은 그냥 해를 봤다"
   ✗ "누군가에겐 휴식이었고, 누군가에겐 도전이었다"
   → 이런 리듬감 있는 짝맞추기를 보면 즉시 지워라. 그냥 사실만 툭 써라.

**3. 매 문단을 감상·의미부여로 마무리하지 마라.**
   ✗ "~였다", "이래서 사람들이 여기 오나 싶었다", "오래 기억에 남을 것 같다"
   → 대부분의 문단은 그냥 사실이나 있었던 일로 끝내라. 억지 여운을 넣지 마라.

**4. 상투구를 쓰지 마라.**
   ✗ "은근히 하이라이트였다", "잊을 수 없는", "힐링이 되었다", "인생샷",
      "여행의 묘미", "말로 표현할 수 없는"

**5. 문장을 너무 매끄럽게 다듬지 마라.**
   → 사람 글은 툭툭 끊기고, 가끔 시시하고, 정보가 중간에 끼어든다.
   → 짧은 문장을 섞어라. "배 탔다. 5분 걸렸다. 생각보다 좋았다." 이런 식으로도.
   → 모든 게 다 좋았다는 톤을 피하라. 별거 아니었으면 별거 아니었다고 써라.

**6. 실제로 겪은 구체적 사실을 감상보다 앞세워라.**
   ✗ "짧은 배편이 은근히 여행의 하이라이트였다"
   ○ "선착장에서 배로 5분이면 건넌다. 편도 5바트쯤 했다. 해질 때 타서 강이 예뻤다."
   → 감상 한 스푼이면 충분하다. 나머지는 사실과 정보로 채워라.

핵심: **잘 쓰려고 하지 마라.** 친구한테 여행 다녀와서 말하듯, 담담하고 시시하게.
너무 완성된 문장은 오히려 가짜처럼 보인다."""

# 모든 글 종류에 공통으로 붙이는 검색 최적화(SEO) 지침.
# 핵심 원칙: 검색은 잘 되게 하되 '자연스러움'을 절대 해치지 마라.
# 검색어를 억지로 도배하면 오히려 스팸으로 감점된다.
_SEO = """

---

## 검색 최적화 (아래를 지키되, 글의 자연스러움을 최우선으로 하라)

**1. 검색어를 먼저 상정하라.** 이 글을 검색할 사람이 검색창에 칠 법한 핵심 검색어를
   1~2개 머릿속에 정하라. "내가 쓰고 싶은 표현"이 아니라 "남이 검색하는 말"이다.
   예: 감상 위주 "소소한 강릉 여행"(X) → 검색어 "강릉 초당순두부 맛집"(O).
   지역명·상호명·대상명을 구체적으로 (그냥 "순두부"가 아니라 "강릉 초당순두부").

**2. 제목에 핵심 검색어를 그대로, 앞쪽에 넣어라.** 변형하지 말고.
   가능하면 검색 수식어를 덧붙여라: 후기 / 가격 / 가는 법 / 추천 / 내돈내산 / 정리.
   단 제목이 어색하게 길거나 키워드 나열처럼 되지 않게, 사람이 읽는 문장으로.

**3. 첫 문단(도입 100~200자)에 핵심 검색어를 한 번 자연스럽게 넣어라.**
   검색엔진은 도입부를 무겁게 본다.

**4. 소제목(##, ###)에 관련 검색어를 흩뿌려라.** 소제목이 곧 글의 뼈대이자 주제 신호다.

**5. 본문에 검색어를 자연스럽게 3~5회 정도 반복하라. 도배는 금지.**
   같은 말을 부자연스럽게 여러 번 넣으면 스팸으로 감점된다. 문맥에 맞게만.
   글 전체가 한 지역·주제라면 그 이름을 모든 소제목에 반복하지 마라
   (예: 제목이 이미 "강릉 ~"이면 소제목은 "초당순두부", "안목해변"처럼 두고
   맨 앞에 매번 "강릉"을 붙이지 않는다). 한 번 맥락이 잡히면 반복은 사람이 읽기에 거슬린다.

**6. 이미지 자리 표시([사진: ...])의 설명에도 검색어를 담아라** (이미지 검색·대체텍스트용).
   예: [사진: 강릉 초당순두부 매운 순두부 한 상].

이 지침은 보조 수단이다. 어떤 항목이든 글을 어색하게 만든다면 그 항목은 포기하고
자연스러움을 택하라. 좋은 글이 먼저고, 검색어는 그 위에 얹는 것이다."""


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
    system = SYSTEM_PROMPTS.get(blog_type, _TECH) + _SEO
    # 창작성 글(여행·일상·리뷰)에만 '사람처럼 쓰기' 규칙을 붙인다.
    # 기술 글은 구조적인 게 오히려 낫다.
    if blog_type in ("travel", "daily", "review"):
        system += _HUMAN_VOICE
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
    streaming = False  # 첫 실질 응답(assistant/result)을 받았는가

    # 무진행 감시 임계값. 이미지가 많으면 두 지점에서 오래 걸린다:
    #  1) 첫 응답(warmup): 업로드 + 시각 처리로 첫 토큰이 늦다
    #  2) 이미지를 다 읽은 뒤 → 실제 글 쓰기 시작 전: 방대한 멀티모달 컨텍스트를
    #     재처리하느라 이벤트 사이 간격이 벌어진다 (실측: 16장에서 75초 초과)
    # 둘 다 이미지 수에 비례해 넉넉히 잡는다. 진짜 stall(연결은 살았는데 무한 침묵)은
    # 그래도 결국 걸린다.
    n_img = len(image_paths)
    warmup = max(120, 90 + n_img * 20)          # 첫 응답 전
    stall_gap = max(STALL_SECONDS, 75 + n_img * 15)  # 응답 시작 후

    async def pump() -> None:
        nonlocal final, last_event, streaming
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
                streaming = True
            elif et == "assistant":
                streaming = True
                if on_progress is not None:
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            on_progress(_describe_tool(block.get("name", "?"), block.get("input") or {}))
                        elif block.get("type") == "text" and block.get("text", "").strip():
                            on_progress("✍ 초안 작성 중…")

    async def watchdog() -> None:
        # 무진행 감시. 첫 응답 전엔 warmup, 응답 시작 후엔 STALL_SECONDS를 쓴다.
        while proc.returncode is None:
            await asyncio.sleep(5)
            limit = stall_gap if streaming else warmup
            if time.monotonic() - last_event > limit:
                log.warning("무진행 %d초(%s, 이미지 %d장) — 중단",
                            int(limit), "streaming" if streaming else "warmup", n_img)
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
