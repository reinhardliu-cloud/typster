import os
import uuid
import json
import asyncio
import shutil
import logging
from typing import Optional
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from converter import convert, list_templates, get_session_dir, get_session_themes_dir
from theme_package import install_theme_package

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/tmp/sessions"))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = Path(os.environ.get("STATIC_DIR", APP_DIR / "static"))
SESSION_TTL_MINUTES = 30

# In-memory record of session creation times
session_registry: dict[str, datetime] = {}
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="MD→Typst Converter", lifespan=lifespan)


def ensure_session(session_id: str | None = None) -> str:
    if session_id:
        try:
            session_id = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="session_id must be a valid UUID") from exc
    else:
        session_id = str(uuid.uuid4())

    session_registry[session_id] = datetime.now(timezone.utc)
    get_session_dir(session_id).mkdir(parents=True, exist_ok=True)
    return session_id


async def cleanup_loop():
    """Background task: every 5 minutes, delete sessions older than 30 minutes."""
    while True:
        await asyncio.sleep(300)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=SESSION_TTL_MINUTES)
        expired_sessions = {sid for sid, ts in list(session_registry.items()) if ts < cutoff}

        # Also scan on-disk sessions so restarts do not leak old folders.
        for session_dir in SESSIONS_DIR.iterdir():
            if not session_dir.is_dir():
                continue
            if datetime.fromtimestamp(session_dir.stat().st_mtime, tz=timezone.utc) < cutoff:
                expired_sessions.add(session_dir.name)

        for sid in expired_sessions:
            session_dir = get_session_dir(sid)
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            session_registry.pop(sid, None)


@app.get("/api/templates")
def api_templates(session_id: Optional[str] = None):
    safe_session = None
    if session_id:
        try:
            safe_session = str(uuid.UUID(session_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="session_id must be a valid UUID")
    return list_templates(session_id=safe_session)


@app.post("/api/theme/upload")
async def api_theme_upload(
    theme_zip: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
):
    if not theme_zip.filename or not theme_zip.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="theme_zip must be a .zip file")

    safe_session_id = ensure_session(session_id)

    zip_bytes = await theme_zip.read()
    try:
        meta = install_theme_package(zip_bytes, get_session_themes_dir(safe_session_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"session_id": safe_session_id, "template": meta})


@app.post("/api/convert")
async def api_convert(
    md_file: UploadFile = File(...),
    template_id: str = Form(...),
    params: str = Form(default="{}"),
    logo_file: UploadFile = File(default=None),
    session_id: Optional[str] = Form(default=None),
):
    try:
        md_bytes = await md_file.read()
        md_content = md_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="md_file must be UTF-8 encoded text.")

    try:
        params_dict = json.loads(params)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="params must be valid JSON.")

    if not isinstance(params_dict, dict):
        raise HTTPException(status_code=400, detail="params must be a JSON object.")

    # If title is missing, default to the uploaded markdown filename stem.
    if not params_dict.get("title"):
        fallback_title = Path(md_file.filename or "").stem.strip()
        if fallback_title:
            params_dict["title"] = fallback_title

    logo_bytes = None
    logo_filename = None
    if logo_file and logo_file.filename:
        logo_bytes = await logo_file.read()
        logo_filename = logo_file.filename

    safe_session_id = ensure_session(session_id)

    try:
        convert(
            session_id=safe_session_id,
            md_content=md_content,
            template_id=template_id,
            params_override=params_dict,
            logo_bytes=logo_bytes,
            logo_filename=logo_filename,
        )
    except (ValueError, FileNotFoundError) as e:
        if not session_id:
            shutil.rmtree(get_session_dir(safe_session_id), ignore_errors=True)
            session_registry.pop(safe_session_id, None)
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        if not session_id:
            shutil.rmtree(get_session_dir(safe_session_id), ignore_errors=True)
            session_registry.pop(safe_session_id, None)
        logger.exception("Conversion failed for session %s", safe_session_id)
        raise HTTPException(status_code=422, detail="Conversion failed. Please check your input and try again.")

    return JSONResponse({
        "session_id": safe_session_id,
        "files": {
            "pdf":  f"/api/session/{safe_session_id}/output.pdf",
            "docx": f"/api/session/{safe_session_id}/output.docx",
            "odt":  f"/api/session/{safe_session_id}/output.odt",
        }
    })


@app.get("/api/session/{session_id}/{filename}")
def api_download(session_id: str, filename: str):
    allowed = {"output.pdf", "output.docx", "output.odt"}
    if filename not in allowed:
        raise HTTPException(status_code=404)
    # Validate session_id is a UUID to prevent path traversal; use canonical form
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError:
        raise HTTPException(status_code=404)
    path = get_session_dir(session_id) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found or expired")
    media_types = {
        "output.pdf":  "application/pdf",
        "output.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "output.odt":  "application/vnd.oasis.opendocument.text",
    }
    if filename == "output.pdf":
        return FileResponse(
            str(path),
            media_type=media_types[filename],
            headers={"Content-Disposition": "inline"},
        )
    return FileResponse(str(path), media_type=media_types[filename], filename=filename)


# Serve static frontend
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
