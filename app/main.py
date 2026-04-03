import os
import uuid
import json
import asyncio
import shutil
import logging
import tempfile
import subprocess
import re
import sys
from typing import Optional
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from converter import (
    convert,
    list_templates,
    get_session_dir,
    get_session_themes_dir,
    CUSTOM_TEMPLATES_DIR,
    TEMPLATES_DIR,
)
from theme_package import install_theme_package, install_theme_package_from_tar

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/tmp/sessions"))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = Path(os.environ.get("STATIC_DIR", APP_DIR / "static"))
SESSION_TTL_MINUTES = 30

# In-memory record of session creation times
session_registry: dict[str, datetime] = {}
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


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


def _run_gh_json(args: list[str]) -> list[dict]:
    """Run a gh command that returns JSON and parse it."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise ValueError("gh CLI not found. Install from https://cli.github.com") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("gh command timed out") from exc

    if result.returncode != 0:
        raise ValueError((result.stderr or "gh command failed").strip())

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Failed to parse gh command output") from exc

    if not isinstance(data, list):
        raise ValueError("Unexpected gh output format")
    return data


def _install_theme_from_github(repo: str, session_themes_dir: Path) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise ValueError("github_repo must be in format owner/repo")

    releases = _run_gh_json(["release", "list", "-R", repo, "--limit", "1", "--json", "tagName"])
    if not releases:
        raise ValueError(f"No releases found for {repo}")

    tag_name = releases[0].get("tagName")
    if not tag_name:
        raise ValueError(f"No valid release tag found for {repo}")

    with tempfile.TemporaryDirectory(prefix="theme-gh-") as tmpdir:
        try:
            download = subprocess.run(
                ["gh", "release", "download", tag_name, "-R", repo, "-p", "*.zip", "-D", tmpdir],
                capture_output=True,
                text=True,
                timeout=90,
            )
        except FileNotFoundError as exc:
            raise ValueError("gh CLI not found. Install from https://cli.github.com") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("gh release download timed out") from exc

        if download.returncode != 0:
            raise ValueError((download.stderr or "Failed to download release zip").strip())

        zips = sorted(Path(tmpdir).glob("*.zip"))
        if not zips:
            raise ValueError(f"No .zip release assets found for {repo}@{tag_name}")

        zip_bytes = zips[0].read_bytes()
        meta = install_theme_package(zip_bytes, session_themes_dir)
        meta.setdefault("source", "github-release")
        meta.setdefault("source_ref", f"{repo}@{tag_name}")
        return meta


def _install_theme_from_github_via_cli(repo: str, session_themes_dir: Path) -> dict:
    """Install a theme by invoking the CLI command chain used by terminal users."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise ValueError("github_repo must be in format owner/repo")

    session_themes_dir.mkdir(parents=True, exist_ok=True)
    before_dirs = {p.name for p in session_themes_dir.iterdir() if p.is_dir()}

    cli_script = APP_DIR / "cli.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(cli_script),
                "--install-dir",
                str(session_themes_dir),
                "install-github",
                repo,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(APP_DIR),
        )
    except FileNotFoundError as exc:
        raise ValueError("Python runtime not found for CLI install") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("CLI install timed out") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "CLI install failed").strip()
        raise ValueError(message)

    after_dirs = [p for p in session_themes_dir.iterdir() if p.is_dir()]
    new_dirs = [p for p in after_dirs if p.name not in before_dirs]
    if new_dirs:
        installed_dir = max(new_dirs, key=lambda p: p.stat().st_mtime)
    elif after_dirs:
        installed_dir = max(after_dirs, key=lambda p: p.stat().st_mtime)
    else:
        raise ValueError("CLI install succeeded but no installed theme directory was found")

    meta_path = installed_dir / "meta.json"
    if not meta_path.exists():
        raise ValueError("CLI install succeeded but meta.json is missing")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Failed to read installed theme metadata") from exc

    if not isinstance(meta, dict):
        raise ValueError("Installed theme metadata format is invalid")

    meta.setdefault("source", "github-cli")
    meta.setdefault("source_ref", repo)
    return meta


def _extract_repo_from_input(raw_value: str) -> str:
    """Accept owner/repo or a full command/url string and extract owner/repo."""
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("github_repo is required")

    # Direct owner/repo input.
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return value

    # Common CLI form: install-github owner/repo
    cli_match = re.search(r"install-github\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", value)
    if cli_match:
        return cli_match.group(1)

    # GitHub URL form: github.com/owner/repo or https://github.com/owner/repo(.git)
    url_match = re.search(
        r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\.git)?",
        value,
    )
    if url_match:
        return url_match.group(1)

    # Last fallback: any owner/repo-like token in the string.
    token_match = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", value)
    if token_match:
        return token_match.group(1)

    raise ValueError(
        "Could not extract owner/repo from input. Paste owner/repo or a CLI command containing install-github owner/repo"
    )


def _read_theme_meta(theme_dir: Path) -> dict:
    meta_path = theme_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_theme_meta(theme_dir: Path, meta: dict) -> None:
    meta_path = theme_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_theme_dir_by_meta_id(root_dir: Path, template_id: str) -> Path | None:
    if not root_dir.exists():
        return None
    for candidate in root_dir.iterdir():
        if not candidate.is_dir():
            continue
        meta = _read_theme_meta(candidate)
        if str(meta.get("id", "")).strip() == template_id:
            return candidate
    return None


def _init_typst_package_as_theme(package_spec: str, session_themes_dir: Path) -> dict:
    """Initialize a Typst package and make it available as a template."""
    package_spec = (package_spec or "").strip()
    if not package_spec:
        raise ValueError("package_spec is required")

    session_themes_dir.mkdir(parents=True, exist_ok=True)
    
    cli_script = APP_DIR / "cli.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(cli_script),
                "--install-dir",
                str(session_themes_dir),
                "init",
                package_spec,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(APP_DIR),
        )
    except FileNotFoundError as exc:
        raise ValueError("Python runtime not found for CLI init") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("CLI init timed out") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "CLI init failed").strip()
        raise ValueError(message)

    after_dirs = [p for p in session_themes_dir.iterdir() if p.is_dir()]
    if not after_dirs:
        raise ValueError("CLI init succeeded but no output directory was found")

    installed_dir = max(after_dirs, key=lambda p: p.stat().st_mtime)
    
    meta_path = installed_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Failed to read package metadata") from exc
    else:
        package_name = package_spec.split("/")[-1].split(":")[0] if "/" in package_spec else package_spec
        theme_id = installed_dir.name
        meta = {
            "id": theme_id,
            "name": package_name,
            "description": f"Typst package: {package_spec}",
            "scenario": "technical",
            "author": "Typst",
        }

    if not isinstance(meta, dict):
        raise ValueError("Package metadata format is invalid")

    # Keep ID aligned with directory so resolve/delete actions stay stable.
    meta["id"] = installed_dir.name
    meta.setdefault("source", "typst-init")
    meta.setdefault("source_ref", package_spec)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


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
    theme_file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
):
    filename = (theme_file.filename or "").lower()
    is_zip = filename.endswith(".zip")
    is_tar = filename.endswith(".tar.gz") or filename.endswith(".tgz")
    
    if not (is_zip or is_tar):
        raise HTTPException(status_code=400, detail="theme_file must be .zip or .tar.gz")

    safe_session_id = ensure_session(session_id)

    file_bytes = await theme_file.read()
    try:
        if is_zip:
            meta = install_theme_package(file_bytes, get_session_themes_dir(safe_session_id))
        else:
            meta = install_theme_package_from_tar(file_bytes, get_session_themes_dir(safe_session_id))
        meta.setdefault("source", "uploaded-archive")
        meta.setdefault("source_ref", theme_file.filename or "theme-archive")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"session_id": safe_session_id, "template": meta})


@app.post("/api/theme/install")
async def api_theme_install(
    github_repo: str = Form(...),
    session_id: Optional[str] = Form(default=None),
    install_via_cli: bool = Form(default=False),
):
    safe_session_id = ensure_session(session_id)

    try:
        repo = _extract_repo_from_input(github_repo)
        session_themes_dir = get_session_themes_dir(safe_session_id)
        if install_via_cli:
            meta = _install_theme_from_github_via_cli(repo, session_themes_dir)
        else:
            meta = _install_theme_from_github(repo, session_themes_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"session_id": safe_session_id, "template": meta})


@app.post("/api/typst/init")
async def api_typst_init(
    package_spec: str = Form(...),
    session_id: Optional[str] = Form(default=None),
):
    """Initialize a Typst package and make it available as a template."""
    safe_session_id = ensure_session(session_id)

    try:
        meta = _init_typst_package_as_theme(package_spec, get_session_themes_dir(safe_session_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"session_id": safe_session_id, "template": meta})


@app.post("/api/theme/delete")
async def api_theme_delete(
    template_id: str = Form(...),
    session_id: Optional[str] = Form(default=None),
    delete_scope: str = Form(default="auto"),
):
    """Delete a session theme, or a custom unpinned theme."""
    safe_session_id = None
    if session_id:
        try:
            safe_session_id = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="session_id must be a valid UUID") from exc

    safe_template_id = (template_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", safe_template_id):
        raise HTTPException(status_code=400, detail="template_id is invalid")

    scope = (delete_scope or "auto").strip().lower()
    if scope not in {"auto", "session", "custom"}:
        raise HTTPException(status_code=400, detail="delete_scope must be auto, session, or custom")

    # 1) Session-local themes can always be deleted.
    if safe_session_id and scope in {"auto", "session"}:
        themes_root = get_session_themes_dir(safe_session_id)
        session_theme_dir = themes_root / safe_template_id
        if not session_theme_dir.exists() or not session_theme_dir.is_dir():
            matched_dir = _find_theme_dir_by_meta_id(themes_root, safe_template_id)
            if matched_dir is not None:
                session_theme_dir = matched_dir
        if session_theme_dir.exists() and session_theme_dir.is_dir():
            shutil.rmtree(session_theme_dir, ignore_errors=True)
            return JSONResponse({
                "session_id": safe_session_id,
                "deleted": True,
                "template_id": safe_template_id,
                "scope": "session",
            })

    # 2) Custom templates can be deleted only when unpinned (persistent=false).
    custom_theme_dir = CUSTOM_TEMPLATES_DIR / safe_template_id
    if not custom_theme_dir.exists() or not custom_theme_dir.is_dir():
        matched_custom = _find_theme_dir_by_meta_id(CUSTOM_TEMPLATES_DIR, safe_template_id)
        if matched_custom is not None:
            custom_theme_dir = matched_custom
    if custom_theme_dir.exists() and custom_theme_dir.is_dir() and scope in {"auto", "custom"}:
        meta = _read_theme_meta(custom_theme_dir)
        if bool(meta.get("persistent", True)):
            raise HTTPException(
                status_code=400,
                detail="Theme is permanent. Disable permanence in settings before deleting.",
            )
        shutil.rmtree(custom_theme_dir, ignore_errors=True)
        return JSONResponse({
            "session_id": safe_session_id,
            "deleted": True,
            "template_id": safe_template_id,
            "scope": "custom",
        })

    raise HTTPException(status_code=404, detail="Theme not found")


@app.post("/api/theme/persistence")
async def api_theme_persistence(
    template_id: str = Form(...),
    persistent: bool = Form(...),
    session_id: Optional[str] = Form(default=None),
):
    """Toggle theme permanence. Permanent themes are stored under templates_custom and protected from deletion."""
    safe_template_id = (template_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", safe_template_id):
        raise HTTPException(status_code=400, detail="template_id is invalid")

    custom_dir = CUSTOM_TEMPLATES_DIR / safe_template_id
    built_in_dir = TEMPLATES_DIR / safe_template_id

    if persistent:
        # Already persisted: just mark metadata pinned.
        if custom_dir.exists() and custom_dir.is_dir():
            meta = _read_theme_meta(custom_dir)
            if not meta:
                raise HTTPException(status_code=400, detail="Persistent template metadata is missing")
            meta["id"] = custom_dir.name
            meta["persistent"] = True
            meta.setdefault("source", "persistent-custom")
            _write_theme_meta(custom_dir, meta)
            return JSONResponse({"template_id": safe_template_id, "persistent": True, "scope": "custom"})

        if built_in_dir.exists() and built_in_dir.is_dir():
            raise HTTPException(status_code=400, detail="Built-in templates are already permanent")

        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required to persist this theme")
        try:
            safe_session_id = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="session_id must be a valid UUID") from exc

        session_root = get_session_themes_dir(safe_session_id)
        source_dir = session_root / safe_template_id
        if not source_dir.exists() or not source_dir.is_dir():
            matched_source = _find_theme_dir_by_meta_id(session_root, safe_template_id)
            if matched_source is None:
                raise HTTPException(status_code=404, detail="Theme not found in this session")
            source_dir = matched_source

        CUSTOM_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        if custom_dir.exists():
            raise HTTPException(status_code=409, detail="A persistent theme with this id already exists")

        shutil.copytree(source_dir, custom_dir)
        meta = _read_theme_meta(custom_dir)
        if not meta:
            raise HTTPException(status_code=400, detail="Cannot persist theme without meta.json")
        meta["id"] = custom_dir.name
        meta["persistent"] = True
        meta.setdefault("source", "persistent-custom")
        _write_theme_meta(custom_dir, meta)
        shutil.rmtree(source_dir, ignore_errors=True)
        return JSONResponse({"template_id": meta["id"], "persistent": True, "scope": "custom"})

    # persistent=False -> unpin custom template, making it deletable.
    if built_in_dir.exists() and built_in_dir.is_dir():
        raise HTTPException(status_code=400, detail="Built-in templates cannot change permanence")

    if not custom_dir.exists() or not custom_dir.is_dir():
        matched_custom = _find_theme_dir_by_meta_id(CUSTOM_TEMPLATES_DIR, safe_template_id)
        if matched_custom is None:
            raise HTTPException(status_code=404, detail="Persistent theme not found")
        custom_dir = matched_custom

    meta = _read_theme_meta(custom_dir)
    if not meta:
        raise HTTPException(status_code=400, detail="Persistent template metadata is missing")
    meta["id"] = custom_dir.name
    meta["persistent"] = False
    meta.setdefault("source", "persistent-custom")
    _write_theme_meta(custom_dir, meta)
    return JSONResponse({"template_id": meta["id"], "persistent": False, "scope": "custom"})


@app.post("/api/theme/scenario")
async def api_theme_scenario(
    template_id: str = Form(...),
    scenario: str = Form(...),
    session_id: Optional[str] = Form(default=None),
    update_scope: str = Form(default="auto"),
):
    """Update theme scenario tag used by UI filters."""
    safe_template_id = (template_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", safe_template_id):
        raise HTTPException(status_code=400, detail="template_id is invalid")

    scenario_value = (scenario or "").strip().lower()
    allowed_scenarios = {"academic", "business", "resume", "technical", "custom"}
    if scenario_value not in allowed_scenarios:
        raise HTTPException(status_code=400, detail="scenario is invalid")

    scope = (update_scope or "auto").strip().lower()
    if scope not in {"auto", "session", "custom"}:
        raise HTTPException(status_code=400, detail="update_scope must be auto, session, or custom")

    safe_session_id = None
    if session_id:
        try:
            safe_session_id = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="session_id must be a valid UUID") from exc

    # Built-in templates are immutable.
    built_in_dir = TEMPLATES_DIR / safe_template_id
    if built_in_dir.exists() and built_in_dir.is_dir():
        raise HTTPException(status_code=400, detail="Built-in template scenario cannot be changed")

    target_dir = None
    target_scope = None

    if scope in {"auto", "custom"}:
        custom_dir = CUSTOM_TEMPLATES_DIR / safe_template_id
        if not custom_dir.exists() or not custom_dir.is_dir():
            custom_dir = _find_theme_dir_by_meta_id(CUSTOM_TEMPLATES_DIR, safe_template_id)
        if custom_dir is not None and custom_dir.exists() and custom_dir.is_dir():
            target_dir = custom_dir
            target_scope = "custom"

    if target_dir is None and safe_session_id and scope in {"auto", "session"}:
        session_root = get_session_themes_dir(safe_session_id)
        session_dir = session_root / safe_template_id
        if not session_dir.exists() or not session_dir.is_dir():
            session_dir = _find_theme_dir_by_meta_id(session_root, safe_template_id)
        if session_dir is not None and session_dir.exists() and session_dir.is_dir():
            target_dir = session_dir
            target_scope = "session"

    if target_dir is None:
        raise HTTPException(status_code=404, detail="Theme not found")

    meta = _read_theme_meta(target_dir)
    if not meta:
        raise HTTPException(status_code=400, detail="Theme metadata is missing")
    meta["id"] = target_dir.name
    meta["scenario"] = scenario_value
    _write_theme_meta(target_dir, meta)

    return JSONResponse({
        "template_id": meta["id"],
        "scenario": scenario_value,
        "scope": target_scope,
        "session_id": safe_session_id,
    })


@app.post("/api/convert")
async def api_convert(
    md_file: UploadFile = File(...),
    template_id: str = Form(...),
    template_version: Optional[str] = Form(default=None),
    params: str = Form(default="{}"),
    logo_file: UploadFile = File(default=None),
    session_id: Optional[str] = Form(default=None),
):
    logger.info(f"[CONVERT] Starting conversion. File: {md_file.filename}, Template: {template_id}, Session: {session_id}")
    
    try:
        md_bytes = await md_file.read()
        md_content = md_bytes.decode("utf-8")
        logger.info(f"[CONVERT] MD file read successfully. Size: {len(md_bytes)} bytes")
    except UnicodeDecodeError as e:
        logger.error(f"[CONVERT] Unicode decode error: {e}")
        raise HTTPException(status_code=400, detail="md_file must be UTF-8 encoded text.")

    try:
        params_dict = json.loads(params)
        logger.info(f"[CONVERT] Params parsed: {params_dict}")
    except json.JSONDecodeError as e:
        logger.error(f"[CONVERT] JSON parse error: {e}")
        raise HTTPException(status_code=400, detail="params must be valid JSON.")

    if not isinstance(params_dict, dict):
        raise HTTPException(status_code=400, detail="params must be a JSON object.")

    # If title is missing, default to the uploaded markdown filename stem.
    if not params_dict.get("title"):
        fallback_title = Path(md_file.filename or "").stem.strip()
        if fallback_title:
            params_dict["title"] = fallback_title
        logger.info(f"[CONVERT] Title set to: {params_dict.get('title')}")

    logo_bytes = None
    logo_filename = None
    if logo_file and logo_file.filename:
        logo_bytes = await logo_file.read()
        logo_filename = logo_file.filename
        logger.info(f"[CONVERT] Logo file read. Size: {len(logo_bytes) if logo_bytes else 0} bytes")

    safe_session_id = ensure_session(session_id)
    logger.info(f"[CONVERT] Session initialized: {safe_session_id}")

    try:
        logger.info(f"[CONVERT] Calling convert() function...")
        result = convert(
            session_id=safe_session_id,
            md_content=md_content,
            template_id=template_id,
            template_version=template_version,
            params_override=params_dict,
            logo_bytes=logo_bytes,
            logo_filename=logo_filename,
        )
        logger.info(f"[CONVERT] Conversion completed successfully for session {safe_session_id}")
    except (ValueError, FileNotFoundError) as e:
        logger.error(f"[CONVERT] ValueError/FileNotFoundError: {e}", exc_info=True)
        if not session_id:
            shutil.rmtree(get_session_dir(safe_session_id), ignore_errors=True)
            session_registry.pop(safe_session_id, None)
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[CONVERT] RuntimeError: {e}", exc_info=True)
        if not session_id:
            shutil.rmtree(get_session_dir(safe_session_id), ignore_errors=True)
            session_registry.pop(safe_session_id, None)
        logger.exception("Conversion failed for session %s", safe_session_id)
        raise HTTPException(status_code=422, detail="Conversion failed. Please check your input and try again.")

    logger.info(f"[CONVERT] Returning result for session {safe_session_id}")
    return JSONResponse({
        "session_id": safe_session_id,
        "files": {
            "pdf":  f"/api/session/{safe_session_id}/output.pdf",
            "docx": f"/api/session/{safe_session_id}/output.docx",
            "odt":  f"/api/session/{safe_session_id}/output.odt",
        },
        "build": result.get("build", {}),
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
