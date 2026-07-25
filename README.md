# Blog Studio

tailnet 전용 기술 블로그 초안 생성 대시보드.
내용·히스토리·이미지를 넣으면 Claude가 초안을 쓰고, 자격증명·소속 정보를 마스킹해 돌려준다.
디스코드 왕복 없이 브라우저에서 초안을 받는 게 목적이다.

## 왜 이렇게 만들었나

- **공개 포트를 안 연다.** `tailscale serve`로 tailnet에만 노출 → 방화벽 변경 없음,
  인터넷에서 존재 자체가 안 보인다. 접근 통제는 Tailscale 계정.
- **마스킹이 전제다.** 초안은 공개 인터넷으로 나가므로, 입력·출력 양쪽에서
  키·토큰·사번·사내 IP·내부 URL·사용자 지정 단어(회사명·노드명 등)를 가린다.
  채팅으로 친 비밀번호처럼 자동 치환이 위험한 것은 '경고'만 한다.

## 구조

```
app/
  main.py       FastAPI — 업로드·비동기 잡·초안 다운로드
  generate.py   Claude Code CLI 헤드리스 호출로 초안 생성
  mask.py       자격증명·소속 식별자 마스킹 + 자격증명 경고
  static/
    index.html  단일 페이지 UI (드래그 업로드 + 폴링)
deploy/
  a1-blog.service   systemd 유닛
redact_terms.txt    추가로 가릴 단어 (한 줄에 하나)
```

## 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8800
```

tailnet 노출 — Tailscale IP에 직접 바인딩:

```bash
# HTTPS 인증서 기능이 안 되는 계정 플랜이라 tailscale serve 대신 직접 바인딩.
# systemd 유닛(deploy/a1-blog.service)이 100.x(Tailscale IP):8800에 바인딩한다.
# tailnet 기기에서 http://<Tailscale IP>:8800 으로 접속.
```

tailnet 기기 외에는 물리적으로 도달 불가하다. 공개 포트를 열지 않고,
방화벽 규칙에도 의존하지 않는다 (특정 인터페이스에만 바인딩).

## 마스킹 범위

| 자동 치환 | 경고만 (사람이 확인) |
|---|---|
| 쿼리스트링 키·봇 토큰·UUID | password/apikey 대입 형태 |
| 사번, /Users·/home 사용자명, IP, *.internal URL | "계정 / 비밀번호!" 형태 |
| redact_terms.txt 의 단어 | 접속정보 표 |

초안이 나와도 **발행 전 사람이 한 번 더 확인**하는 게 원칙이다. 경고 0건이어도 마찬가지.
