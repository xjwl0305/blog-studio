"""블로그 대시보드 — tailnet 전용 웹 UI.

Tailscale IP에 바인딩하고, tailnet 기기에서만 접근한다. 공개 포트를 열지 않는다.

생성은 스트리밍으로 돌리며 진행상황을 잡에 쌓는다:
  POST /api/generate  → job_id (즉시)
  GET  /api/job/{id}  → 상태 + 진행 로그 + 결과 폴링
  POST /api/job/{id}/cancel → 취소
  GET  /api/sessions  → 동기화된 작업 세션 목록 (블로그 재료 선택용)
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .generate import Handle, generate
from .mask import scan_secrets
from .sessions import extract, list_sessions, search_sessions

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("blog.main")

BASE = Path("/home/ubuntu/blog-studio")
UPLOAD_DIR = BASE / "uploads"
DRAFT_DIR = BASE / "drafts"
UPLOAD_DIR.mkdir(exist_ok=True)
DRAFT_DIR.mkdir(exist_ok=True)

_MAX_BYTES = 25 * 1024 * 1024
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".md", ".txt", ".pdf"}
_BAD_NAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

app = FastAPI(title="Blog Studio")


@dataclass
class Job:
    id: str
    status: str = "running"          # running | done | error | cancelled
    progress: list[str] = field(default_factory=list)   # 진행 로그 (도구 사용 등)
    draft: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    handle: Handle = field(default_factory=Handle)
    started: float = 0.0


_JOBS: dict[str, Job] = {}
_lock = asyncio.Lock()


def _safe_name(name: str) -> str:
    name = _BAD_NAME.sub("_", name).strip().lstrip(".")
    return Path(name).name[:120] or "upload.bin"


async def _save_uploads(files: list[UploadFile], job_id: str) -> tuple[list[Path], list[str]]:
    saved, errs = [], []
    target = UPLOAD_DIR / job_id
    for f in files:
        if not f.filename:
            continue
        if Path(f.filename).suffix.lower() not in _ALLOWED_EXT:
            errs.append(f"{f.filename}: 허용되지 않는 형식"); continue
        data = await f.read()
        if len(data) > _MAX_BYTES:
            errs.append(f"{f.filename}: 너무 큼 ({len(data)/1024/1024:.1f}MB)"); continue
        target.mkdir(parents=True, exist_ok=True)
        p = target / _safe_name(f.filename)
        p.write_bytes(data)
        saved.append(p)
    return saved, errs


async def _run_job(job: Job, content: str, images: list[Path], material: str,
                   blog_type: str) -> None:
    from time import monotonic
    job.started = monotonic()
    # 첫 이벤트가 오기까지(재료가 크면 수십 초) 빈 화면이 되지 않게 초기 표시.
    wait_hint = ("이미지가 많아 첫 응답까지 1~3분 걸릴 수 있습니다"
                 if len(images) >= 6 else "첫 응답까지 잠시 걸립니다")
    job.progress.append(
        "🧠 재료 분석 중… "
        + (f"세션 {len(material):,}자 · " if material else "")
        + (f"이미지 {len(images)}장 · " if images else "")
        + wait_hint
    )

    def on_progress(line: str) -> None:
        # 같은 줄이 연속으로 쌓이지 않게 (초안 작성 중… 반복 방지)
        if not job.progress or job.progress[-1] != line:
            job.progress.append(line)
        job.progress[:] = job.progress[-40:]

    # 어떤 예외가 나든 작업이 'running'에 영원히 멈추지 않게 감싼다.
    # (예전엔 generate가 예외를 던지면 상태가 running으로 남아 UI가 무한 대기했다.)
    try:
        async with _lock:
            result = await generate(content, images, material, blog_type,
                                    on_progress=on_progress, handle=job.handle)
    except Exception as exc:
        log.exception("생성 중 예외")
        job.status, job.error = "error", f"내부 오류: {type(exc).__name__}: {exc}"[:200]
        return

    if result.cancelled:
        job.status = "cancelled"; return
    if result.error:
        job.status, job.error = "error", result.error; return
    job.draft = result.text
    job.cost_usd = result.cost_usd
    job.warnings = scan_secrets(result.text)
    first = next((ln.lstrip("# ").strip() for ln in result.text.splitlines() if ln.strip()), "draft")
    (DRAFT_DIR / f"{job.id[:8]}_{_safe_name(first)[:50] or 'draft'}.md").write_text(
        result.text, encoding="utf-8")
    job.status = "done"


@app.post("/api/generate")
async def api_generate(
    content: str = Form(""),
    session_id: str = Form(""),
    blog_type: str = Form("tech"),
    files: list[UploadFile] = None,
):
    job = Job(id=uuid.uuid4().hex)
    _JOBS[job.id] = job
    images, upload_errs = await _save_uploads(files or [], job.id)
    material = extract(session_id) if session_id else ""
    if not content.strip() and not images and not material:
        job.status, job.error = "error", "내용·이미지·세션 중 하나는 있어야 합니다."
        return {"job_id": job.id}
    if session_id and not material:
        job.warnings.append("선택한 세션에서 재료를 뽑지 못했습니다(경로 확인).")
    if upload_errs:
        job.warnings.extend(upload_errs)
    asyncio.create_task(_run_job(job, content, images, material, blog_type))
    return {"job_id": job.id}


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "없는 작업"}, status_code=404)
    from time import monotonic
    elapsed = int(monotonic() - job.started) if job.started else 0
    return {
        "status": job.status, "progress": job.progress, "draft": job.draft,
        "error": job.error, "warnings": job.warnings,
        "cost_usd": round(job.cost_usd, 4), "elapsed": elapsed,
    }


@app.post("/api/job/{job_id}/cancel")
async def api_cancel(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "없는 작업"}, status_code=404)
    if job.status == "running":
        job.handle.cancel()
        log.info("작업 취소 요청: %s", job_id)
    return {"status": "cancel-requested"}


@app.get("/api/job/{job_id}/download")
async def api_download(job_id: str):
    job = _JOBS.get(job_id)
    if not job or job.status != "done":
        return PlainTextResponse("초안이 아직 없습니다.", status_code=404)
    first = next((ln.lstrip("# ").strip() for ln in job.draft.splitlines() if ln.strip()), "draft")
    return PlainTextResponse(
        job.draft,
        headers={"Content-Disposition": f'attachment; filename="{_safe_name(first)[:50] or "draft"}.md"'},
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/api/sessions")
async def api_sessions(q: str = ""):
    return {"sessions": search_sessions(q) if q.strip() else list_sessions()}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE / "app" / "static" / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(BASE / "app" / "static")), name="static")
