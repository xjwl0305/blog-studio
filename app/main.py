"""블로그 대시보드 — tailnet 전용 웹 UI.

localhost:8800에 바인딩하고, `tailscale serve`로 tailnet에만 노출한다.
공개 포트를 열지 않는다. 접근 통제는 Tailscale 계정 + (기본) 로컬 바인딩.

생성은 1분 안팎 걸리므로 비동기 잡으로 처리한다:
  POST /api/generate  → job_id 반환 (즉시)
  GET  /api/job/{id}  → 상태·결과 폴링
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .generate import generate
from .mask import scan_secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("blog.main")

BASE = Path("/home/ubuntu/blog-studio")
UPLOAD_DIR = BASE / "uploads"
DRAFT_DIR = BASE / "drafts"
UPLOAD_DIR.mkdir(exist_ok=True)
DRAFT_DIR.mkdir(exist_ok=True)

# 업로드 상한 (이미지 기준 여유롭게).
_MAX_BYTES = 25 * 1024 * 1024
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".md", ".txt", ".pdf"}
_BAD_NAME = __import__("re").compile(r'[/\\:*?"<>|\x00-\x1f]')

app = FastAPI(title="Blog Studio")


@dataclass
class Job:
    id: str
    status: str = "running"        # running | done | error
    draft: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    created: str = ""


_JOBS: dict[str, Job] = {}
# 동시 생성 금지 — 여러 잡이 같은 claude/파일을 건드리지 않게.
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
        ext = Path(f.filename).suffix.lower()
        if ext not in _ALLOWED_EXT:
            errs.append(f"{f.filename}: 허용되지 않는 형식")
            continue
        data = await f.read()
        if len(data) > _MAX_BYTES:
            errs.append(f"{f.filename}: 너무 큼 ({len(data)/1024/1024:.1f}MB)")
            continue
        target.mkdir(parents=True, exist_ok=True)
        p = target / _safe_name(f.filename)
        p.write_bytes(data)
        saved.append(p)
    return saved, errs


async def _run_job(job: Job, content: str, images: list[Path]) -> None:
    async with _lock:
        result = await generate(content, images)
    if result.error:
        job.status, job.error = "error", result.error
        return
    job.draft = result.text
    job.cost_usd = result.cost_usd
    job.warnings = scan_secrets(result.text)
    # 초안을 디스크에도 저장 (제목 첫 줄 기반 파일명).
    first = next((ln.lstrip("# ").strip() for ln in result.text.splitlines() if ln.strip()), "draft")
    fname = _safe_name(first)[:50] or "draft"
    (DRAFT_DIR / f"{job.id[:8]}_{fname}.md").write_text(result.text, encoding="utf-8")
    job.status = "done"


@app.post("/api/generate")
async def api_generate(content: str = Form(""), files: list[UploadFile] = None):
    job = Job(id=uuid.uuid4().hex, created=datetime.now(timezone.utc).isoformat())
    _JOBS[job.id] = job
    images, upload_errs = await _save_uploads(files or [], job.id)
    if not content.strip() and not images:
        job.status, job.error = "error", "내용이나 이미지 중 하나는 있어야 합니다."
        return {"job_id": job.id}
    if upload_errs:
        job.warnings.extend(upload_errs)
    asyncio.create_task(_run_job(job, content, images))
    return {"job_id": job.id}


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "없는 작업"}, status_code=404)
    return {
        "status": job.status, "draft": job.draft, "error": job.error,
        "warnings": job.warnings, "cost_usd": round(job.cost_usd, 4),
    }


@app.get("/api/job/{job_id}/download")
async def api_download(job_id: str):
    job = _JOBS.get(job_id)
    if not job or job.status != "done":
        return PlainTextResponse("초안이 아직 없습니다.", status_code=404)
    first = next((ln.lstrip("# ").strip() for ln in job.draft.splitlines() if ln.strip()), "draft")
    fname = _safe_name(first)[:50] or "draft"
    return PlainTextResponse(
        job.draft,
        headers={"Content-Disposition": f'attachment; filename="{fname}.md"'},
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE / "app" / "static" / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(BASE / "app" / "static")), name="static")
