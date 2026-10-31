from __future__ import annotations

import asyncio
import copy
import io
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import qrcode
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.agent import (
    CitationVerificationAgent,
    STATUS_LABELS,
    SUPPORT_LABELS,
    rebuild_report_derived,
)
from backend.config import settings
from backend.documents import DocumentParseError, SUPPORTED_EXTENSIONS
from backend.exports import report_to_docx, report_to_json, report_to_pdf, report_to_xlsx
from backend.references import normalize_doi, title_similarity
from backend.sources import MetadataResolver
from backend.store import store


ROOT = Path(__file__).parent
STATIC = ROOT / "static"
PUBLIC_URL_FILE = ROOT / ".runtime" / "public_url.txt"
STANDALONE_FILES = (
    ROOT / "docs" / "index.html",
    ROOT / "悟真鉴引-离线单页版.html",
    ROOT / "悟真鉴引.html",
)
agent = CitationVerificationAgent()
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _public_base_url(request: Request) -> str:
    configured = settings.app_base_url
    if urlparse(configured).hostname not in LOCAL_HOSTS:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def _external_app_url(request: Request) -> str:
    current = _public_base_url(request)
    if urlparse(current).hostname not in LOCAL_HOSTS:
        return current
    try:
        published = PUBLIC_URL_FILE.read_text(encoding="utf-8").strip().rstrip("/")
    except OSError:
        published = ""
    return published if urlparse(published).hostname else current


def _is_local_request(request: Request) -> bool:
    return urlparse(f"//{request.headers.get('host', '')}").hostname in LOCAL_HOSTS


class SupplementRequest(BaseModel):
    ref_no: int
    doi: str


class ShareRequest(BaseModel):
    job_id: str
    redacted: bool = True


app = FastAPI(
    title="悟真鉴引 API",
    version="1.0.0",
    description="面向学术论著的引文核验与证据链优化智能体。",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/standalone")
async def standalone() -> FileResponse:
    for candidate in STANDALONE_FILES:
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="单页版文件尚未随项目发布。")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/share/{token}")
async def share_page(token: str) -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "llm_enabled": settings.llm_enabled,
        "llm_model": settings.llm_model if settings.llm_enabled else "规则辅助模式",
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "statuses": list(STATUS_LABELS.values()),
        "support_statuses": list(SUPPORT_LABELS.values()),
        "local_editor": _is_local_request(request),
    }


@app.post("/api/jobs")
async def create_job(
    request: Request,
    background_tasks: BackgroundTasks,
    mode: str = Form(...),
    text: str = Form(""),
    file: UploadFile | None = File(None),
) -> dict[str, str]:
    if mode not in {"file", "paste"}:
        raise HTTPException(status_code=400, detail="mode 必须为 file 或 paste。")
    if mode == "paste" and not text.strip():
        raise HTTPException(status_code=400, detail="粘贴内容为空，请提供参考文献列表。")
    if mode == "file" and file is None:
        raise HTTPException(status_code=400, detail="请选择要上传的论文文件。")

    file_name = ""
    file_bytes = b""
    if mode == "file" and file is not None:
        file_name = file.filename or "未命名文件"
        suffix = Path(file_name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="仅支持 PDF、DOCX、TXT 和 Markdown 文件。")
        file_bytes = await file.read(settings.max_file_bytes + 1)
        if len(file_bytes) > settings.max_file_bytes:
            raise HTTPException(status_code=413, detail=f"文件不能超过 {settings.max_file_bytes // 1024 // 1024} MB。")
        if not file_bytes:
            raise HTTPException(status_code=400, detail="上传文件为空。")

    await store.cleanup()
    job_id = await store.create()
    use_cnki = settings.allow_shared_cnki or _is_local_request(request)
    background_tasks.add_task(
        _run_job,
        job_id=job_id,
        mode=mode,
        file_name=file_name,
        file_bytes=file_bytes,
        pasted_text=text,
        use_cnki=use_cnki,
    )
    return {"job_id": job_id}


async def _run_job(
    *,
    job_id: str,
    mode: str,
    file_name: str,
    file_bytes: bytes,
    pasted_text: str,
    use_cnki: bool,
) -> None:
    async def progress(
        key: str,
        label: str,
        state: str,
        percent: int,
        detail: str = "",
    ) -> None:
        await store.update(
            job_id,
            status="running",
            stage=label,
            progress=percent,
            step={"key": key, "label": label, "state": state, "detail": detail},
        )

    try:
        await store.update(job_id, status="running", stage="初始化", progress=2)
        report = await agent.run(
            job_id=job_id,
            mode=mode,
            file_name=file_name,
            file_bytes=file_bytes,
            pasted_text=pasted_text,
            progress=progress,
            use_cnki=use_cnki,
        )
        await store.update(
            job_id,
            status="completed",
            stage="核验完成",
            progress=100,
            result=report,
            error=None,
        )
    except DocumentParseError as exc:
        await store.update(job_id, status="failed", stage="输入检查失败", error=str(exc))
    except Exception as exc:
        await store.update(
            job_id,
            status="failed",
            stage="核验失败",
            error=f"核验过程异常：{exc}",
        )


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    if not await store.get(job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")

    async def stream():
        last_signature = None
        started = time.monotonic()
        while time.monotonic() - started < 600:
            if await request.is_disconnected():
                break
            job = await store.get(job_id)
            if not job:
                yield "event: error\ndata: {\"message\":\"任务已过期\"}\n\n"
                break
            signature = (job["status"], job["progress"], len(job["steps"]))
            if signature != last_signature:
                payload = json.dumps(
                    {
                        "status": job["status"],
                        "stage": job["stage"],
                        "progress": job["progress"],
                        "steps": job["steps"],
                        "error": job["error"],
                    },
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n"
                last_signature = signature
            if job["status"] in {"completed", "failed"}:
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.45)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/reports/{job_id}")
async def get_report(job_id: str) -> dict[str, Any]:
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    if job["status"] != "completed" or not job["result"]:
        raise HTTPException(status_code=409, detail=job.get("error") or "任务尚未完成。")
    return job["result"]


@app.post("/api/reports/{job_id}/supplement")
async def supplement_reference(
    job_id: str,
    payload: SupplementRequest,
    request: Request,
) -> dict[str, Any]:
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="补充 DOI 仅允许在本机编辑报告时使用。")
    job = await store.get(job_id)
    if not job or not job.get("result"):
        raise HTTPException(status_code=404, detail="报告不存在或已过期。")
    report = copy.deepcopy(job["result"])
    target = next(
        (item for item in report["references"] if item["ref_no"] == payload.ref_no),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="未找到对应文献编号。")

    doi = normalize_doi(payload.doi)
    if not doi:
        raise HTTPException(status_code=400, detail="DOI 不能为空。")
    if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        raise HTTPException(status_code=400, detail="DOI 格式不正确，应形如 10.1109/CVPR.2016.90。")
    resolver = MetadataResolver()
    try:
        candidate = await resolver.resolve_supplement(doi, target.get("title", ""))
    finally:
        await resolver.close()
    if not candidate:
        raise HTTPException(status_code=422, detail="该 DOI 未在公开来源中解析，暂不能补全。")

    target["doi"] = doi
    target["missing_fields"] = [item for item in target["missing_fields"] if item != "DOI"]
    title_match = title_similarity(target.get("title", ""), candidate.get("title", ""))
    year_match = (
        not target.get("year")
        or not candidate.get("year")
        or target["year"] == candidate["year"]
    )
    if title_match >= 0.55 and year_match:
        target["verification_status"] = "verified"
        target["status_label"] = "已证实"
        target["coverage"] = "用户补充 DOI 后完成来源匹配"
        target["risk_level"] = "low"
    else:
        target["verification_status"] = "conflict"
        target["status_label"] = "信息冲突"
        target["coverage"] = "用户补充 DOI 指向的题录与原文不一致"
        target["risk_level"] = "high"
    target["retracted"] = bool(candidate.get("retracted"))
    target["retraction_note"] = (
        f"{candidate.get('source', '公开来源')} 标记该文献已被撤稿"
        if target["retracted"]
        else ""
    )
    if target["retracted"]:
        target["risk_level"] = "high"
        target["coverage"] += "；公开来源同时标记该文献已被撤稿"
    target["sources"].append(
        {
            "source": "用户补充 DOI",
            "ok": True,
            "url": candidate["source_url"],
            "title": candidate["title"],
            "year": candidate["year"],
        }
    )
    target["evidence"] = candidate["abstract"]
    target["evidence_url"] = candidate["source_url"]
    target["risk_reason"] = (
        "公开来源题录匹配" if target["verification_status"] == "verified" else "DOI 与题录字段不一致"
    )
    target["repair_suggestion"] = (
        "已补充 DOI；可继续人工核验正文证据。"
        if target["verification_status"] == "verified"
        else "DOI 已解析，但题录字段不一致，请核对原始文献。"
    )
    if target["retracted"]:
        target["risk_reason"] = target["retraction_note"]
        target["repair_suggestion"] = (
            "该文献已被公开来源标记为撤稿，请核查撤稿声明，并删除或改写相关引用。"
        )
    report["issues"] = [
        issue
        for issue in report["issues"]
        if not (
            issue.get("ref_no") == payload.ref_no
            and issue.get("type")
            in {
                "待人工补证",
                "数据源覆盖不足",
                "等待授权库核验",
                "题录信息冲突",
                "已撤稿文献",
            }
        )
    ]
    if target["verification_status"] == "conflict":
        report["issues"].append(
            {
                "severity": "high",
                "severity_label": "高风险",
                "type": "题录信息冲突",
                "ref_no": payload.ref_no,
                "message": f"第 {payload.ref_no} 条补充 DOI 后仍与题录信息不一致。",
                "action": "核对原始文献的作者、年份、标题和期刊。",
            }
        )
    if target["retracted"]:
        report["issues"].append(
            {
                "severity": "high",
                "severity_label": "高风险",
                "type": "已撤稿文献",
                "ref_no": payload.ref_no,
                "message": (
                    f"第 {payload.ref_no} 条补充 DOI 后确认已被公开来源标记为撤稿。"
                ),
                "action": "核查撤稿声明，删除该引用或改写为对撤稿事实的说明。",
            }
        )
    rebuild_report_derived(report)
    await store.update(job_id, result=report)
    return report


@app.get("/api/reports/{job_id}/export/{file_type}")
async def export_report(job_id: str, file_type: str) -> Response:
    job = await store.get(job_id)
    if not job or not job.get("result"):
        raise HTTPException(status_code=404, detail="报告不存在或已过期。")
    report = job["result"]
    exporters = {
        "docx": (
            report_to_docx,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "wuzhen-jianyin-report.docx",
        ),
        "xlsx": (
            report_to_xlsx,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "wuzhen-jianyin-issues.xlsx",
        ),
        "pdf": (report_to_pdf, "application/pdf", "wuzhen-jianyin-report.pdf"),
        "json": (report_to_json, "application/json", "wuzhen-jianyin-report.json"),
    }
    if file_type not in exporters:
        raise HTTPException(status_code=400, detail="不支持的导出格式。")
    exporter, media_type, filename = exporters[file_type]
    payload = await asyncio.to_thread(exporter, report)
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/share")
async def create_share(payload: ShareRequest, request: Request) -> dict[str, Any]:
    job = await store.get(payload.job_id)
    if not job or not job.get("result"):
        raise HTTPException(status_code=404, detail="报告不存在或已过期。")
    token = await store.create_share(payload.job_id, payload.redacted)
    share_url = f"{_public_base_url(request)}/share/{token}"
    return {
        "token": token,
        "url": share_url,
        "qr_url": f"/api/share/{token}/qr",
        "expires_in_seconds": settings.job_ttl_seconds,
    }


@app.get("/api/share/{token}")
async def get_shared_report(token: str) -> dict[str, Any]:
    share = await store.get_share(token)
    if not share:
        raise HTTPException(status_code=404, detail="分享已过期或已撤销。")
    job = await store.get(share["job_id"])
    if not job or not job.get("result"):
        raise HTTPException(status_code=404, detail="分享报告已过期。")
    report = copy.deepcopy(job["result"])
    if share["redacted"]:
        report["source_name"] = "已脱敏论文"
        report["mode"] = "shared"
    report["id"] = ""
    report["share"] = {
        "redacted": share["redacted"],
        "expires_in_seconds": max(0, int(settings.job_ttl_seconds - (time.time() - share["created_at"]))),
    }
    return report


@app.get("/api/share/{token}/qr")
async def share_qr(token: str, request: Request) -> Response:
    share = await store.get_share(token)
    if not share:
        raise HTTPException(status_code=404, detail="分享已过期或已撤销。")
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(f"{_public_base_url(request)}/share/{token}")
    qr.make(fit=True)
    image = qr.make_image(fill_color="#17202a", back_color="#fffdf8")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(content=output.getvalue(), media_type="image/png")


@app.get("/api/app-share")
async def app_share(request: Request) -> dict[str, Any]:
    url = _external_app_url(request)
    if urlparse(url).hostname in LOCAL_HOSTS:
        raise HTTPException(
            status_code=409,
            detail="当前还是本机地址，对方无法打开。请先运行 .\\share.ps1 建立公网入口。",
        )
    host = urlparse(url).hostname or ""
    return {
        "url": url,
        "qr_url": "/api/app-qr",
        "persistent": not host.endswith(".trycloudflare.com"),
    }


@app.get("/api/app-qr")
async def app_qr(request: Request) -> Response:
    url = _external_app_url(request)
    if urlparse(url).hostname in LOCAL_HOSTS:
        raise HTTPException(status_code=409, detail="公网入口尚未建立。")
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#17202a", back_color="#fffdf8")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(content=output.getvalue(), media_type="image/png")


@app.exception_handler(DocumentParseError)
async def document_error_handler(_: Request, exc: DocumentParseError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)

