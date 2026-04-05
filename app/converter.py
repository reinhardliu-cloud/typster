import os
import subprocess
import shutil
import uuid
import re
import json
from datetime import datetime, timezone
from pathlib import Path
import yaml
from jinja2 import Environment, FileSystemLoader
from typst_theme_import import serialize_typst_value

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/tmp/sessions"))
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", APP_DIR / "templates"))
CUSTOM_TEMPLATES_DIR = Path(os.environ.get("CUSTOM_TEMPLATES_DIR", APP_DIR / "templates_custom"))
CONVERTER_VERSION = os.environ.get("CONVERTER_VERSION", "dev")

DEFAULT_TEMPLATE_PARAMS: list[dict[str, object]] = [
    {"key": "title", "type": "text", "label": "Title", "required": True, "default": ""},
    {"key": "subtitle", "type": "text", "label": "Subtitle", "required": False, "default": ""},
    {"key": "author", "type": "text", "label": "Author", "required": False, "default": ""},
    {"key": "affiliation", "type": "text", "label": "Affiliation", "required": False, "default": ""},
    {"key": "year", "type": "text", "label": "Year", "required": False, "default": ""},
    {"key": "class", "type": "text", "label": "Class", "required": False, "default": ""},
    {"key": "main-color", "type": "color", "label": "Accent Color", "required": False, "default": "E94845"},
    {"key": "logo", "type": "file", "label": "Logo Image", "required": False},
]


def get_session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def get_session_themes_dir(session_id: str) -> Path:
    return get_session_dir(session_id) / "themes"


def parse_frontmatter(md_content: str) -> dict:
    """Extract YAML frontmatter from markdown content."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', md_content, re.DOTALL)
    if match:
        try:
            return yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return {}
    return {}


def strip_frontmatter(md_content: str) -> str:
    """Remove YAML frontmatter from markdown content."""
    return re.sub(r'^---\s*\n.*?\n---\s*\n', '', md_content, count=1, flags=re.DOTALL)


def load_template_meta(template_dir: Path) -> dict:
    meta_path = template_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Template metadata not found: {meta_path}")
    with open(meta_path) as f:
        raw_meta = json.load(f)
    return normalize_template_meta(raw_meta, template_dir)


def normalize_template_id(template_id: str) -> str:
    m = re.fullmatch(r'[a-zA-Z0-9_.-]+', template_id)
    if not m:
        raise ValueError(f"Invalid template ID: {template_id!r}")
    return m.group(0)


def _clone_default_params() -> list[dict[str, object]]:
    return [dict(param) for param in DEFAULT_TEMPLATE_PARAMS]


def normalize_template_meta(meta: dict, template_dir: Path | None = None) -> dict:
    normalized = dict(meta) if isinstance(meta, dict) else {}

    params = normalized.get("params")
    if isinstance(params, list):
        normalized["params"] = params
        normalized["params_source"] = "declared"
    else:
        normalized["params"] = []
        normalized["params_source"] = "missing"

    wrapper_template = str(normalized.get("wrapper_template") or "wrapper.typ.jinja")
    normalized["wrapper_template"] = wrapper_template

    compatibility_message = ""
    compatible = True
    if template_dir is not None:
        missing_files = []
        if not (template_dir / "template.typ").exists():
            missing_files.append("template.typ")
        if not (template_dir / wrapper_template).exists():
            missing_files.append(wrapper_template)
        if missing_files:
            compatible = False
            missing_text = ", ".join(missing_files)
            if str(normalized.get("source", "")) == "typst-init":
                compatibility_message = (
                    "This package was installed with typst init, but it is a raw Typst template package rather "
                    "than a converter theme. The web converter requires template.typ and wrapper.typ.jinja so it "
                    f"can map Markdown into the theme. Missing: {missing_text}."
                )
            else:
                compatibility_message = (
                    "This theme is missing required converter files and cannot be used by the web converter. "
                    f"Missing: {missing_text}."
                )

    normalized["converter_compatible"] = compatible
    if compatibility_message:
        normalized["converter_message"] = compatibility_message

    if not normalized["params"]:
        if compatible:
            normalized["params_message"] = (
                "This theme does not declare any editable document fields in meta.json, so Step 3 has no "
                "frontmatter overrides to show."
            )
        else:
            normalized["params_message"] = compatibility_message or (
                "This theme does not expose editable document fields for the web converter."
            )

    if compatible and normalized["params_source"] == "missing":
        normalized["fallback_params"] = _clone_default_params()

    return normalized


def _find_template_thumbnail(template_dir: Path) -> str | None:
    """Return a relative path (within template_dir) to the best thumbnail image, or None."""
    # 1. Check typst.toml [template] thumbnail = "..."
    toml_path = template_dir / "typst.toml"
    if toml_path.exists():
        try:
            content = toml_path.read_text(encoding="utf-8")
            m = re.search(r'^\s*thumbnail\s*=\s*"([^"]+)"', content, re.MULTILINE)
            if m:
                rel = m.group(1)
                if (template_dir / rel).exists():
                    return rel
        except OSError:
            pass

    # 2. thumbnails/1.png (typst package convention)
    for candidate in ["thumbnails/1.png", "thumbnails/1.jpg"]:
        if (template_dir / candidate).exists():
            return candidate

    # 3. First PNG in thumbnails/
    thumb_dir = template_dir / "thumbnails"
    if thumb_dir.is_dir():
        for img in sorted(thumb_dir.iterdir()):
            if img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                return "thumbnails/" + img.name

    # 4. First non-logo PNG in assets/ (built-in bubble convention)
    assets_dir = template_dir / "assets"
    if assets_dir.is_dir():
        for img in sorted(assets_dir.iterdir()):
            if img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and "logo" not in img.name.lower():
                return "assets/" + img.name

    return None


def _load_templates_from_dir(templates_dir: Path, scope: str) -> list[dict]:
    if not templates_dir.exists():
        return []

    templates = []
    for d in templates_dir.iterdir():
        if d.is_dir() and (d / "meta.json").exists():
            with open(d / "meta.json") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                continue
            meta["_scope"] = scope
            if scope == "session":
                meta.setdefault("source", "session-installed")
                meta["persistent"] = False
                meta["deletable"] = True
            elif scope == "custom":
                meta.setdefault("source", "persistent-custom")
                meta["persistent"] = bool(meta.get("persistent", True))
                meta["deletable"] = not meta["persistent"]
            else:
                meta.setdefault("source", "built-in")
                meta["persistent"] = True
                meta["deletable"] = False
            meta = normalize_template_meta(meta, d)
            thumbnail_rel = _find_template_thumbnail(d)
            if thumbnail_rel:
                meta["_thumbnail_rel"] = thumbnail_rel
            templates.append(meta)
    return templates


def resolve_template_dir(template_id: str, session_id: str | None = None) -> Path:
    safe_template_id = normalize_template_id(template_id)

    built_in = TEMPLATES_DIR / safe_template_id
    if (built_in / "meta.json").exists():
        return built_in

    installed_custom = CUSTOM_TEMPLATES_DIR / safe_template_id
    if (installed_custom / "meta.json").exists():
        return installed_custom

    if session_id:
        session_theme_dir = get_session_themes_dir(session_id) / safe_template_id
        if (session_theme_dir / "meta.json").exists():
            return session_theme_dir

    raise FileNotFoundError(f"Template not found: {safe_template_id!r}")


def list_templates(session_id: str | None = None) -> list:
    templates = _load_templates_from_dir(TEMPLATES_DIR, scope="built-in")
    templates.extend(_load_templates_from_dir(CUSTOM_TEMPLATES_DIR, scope="custom"))

    if session_id:
        templates.extend(_load_templates_from_dir(get_session_themes_dir(session_id), scope="session"))

    # Prefer custom theme over session over built-in when IDs overlap.
    scope_rank = {"custom": 0, "session": 1, "built-in": 2}
    deduped: dict[str, dict] = {}
    for tpl in templates:
        tpl_id = str(tpl.get("id", "")).strip()
        if not tpl_id:
            continue
        current = deduped.get(tpl_id)
        if current is None:
            deduped[tpl_id] = tpl
            continue
        current_rank = scope_rank.get(str(current.get("_scope", "built-in")), 3)
        new_rank = scope_rank.get(str(tpl.get("_scope", "built-in")), 3)
        if new_rank < current_rank:
            deduped[tpl_id] = tpl

    result = list(deduped.values())
    result.sort(key=lambda t: (0 if str(t.get("id", "")).startswith("bubble") else 1, str(t.get("name", ""))))
    return result


def convert(
    session_id: str,
    md_content: str,
    template_id: str,
    template_version: str | None,
    params_override: dict,
    logo_bytes: bytes | None = None,
    logo_filename: str | None = None,
) -> dict:
    """
    Full conversion pipeline. Returns dict of output file paths.
    Steps:
    1. Create session dir
    2. Write input.md (full markdown, including frontmatter)
    3. Parse frontmatter, merge with overrides (overrides win)
    4. Run pandoc input.md --to=typst -o body.typ
    5. Strip pandoc typst preamble from body.typ (keep only content)
    6. Render Jinja2 wrapper → main.typ
    7. Run typst compile main.typ output.pdf
    8. Run pandoc input.md -o output.docx
    9. Run pandoc input.md -o output.odt
    10. Return paths
    """
    session_dir = get_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    safe_template_id = normalize_template_id(template_id)
    template_dir = resolve_template_dir(safe_template_id, session_id=session_id)
    meta = load_template_meta(template_dir)

    if not meta.get("converter_compatible", True):
        raise ValueError(
            str(meta.get("converter_message") or "This theme is not compatible with the web converter.")
        )

    resolved_template_version = str(meta.get("version", "local"))
    if template_version and str(template_version) != resolved_template_version:
        raise ValueError(
            f"Requested template_version={template_version!r} does not match "
            f"installed version {resolved_template_version!r}"
        )

    # Write full markdown (with frontmatter) for docx/odt
    input_md = session_dir / "input.md"
    input_md.write_text(md_content, encoding="utf-8")

    # Parse frontmatter and merge params (override wins over frontmatter)
    frontmatter = parse_frontmatter(md_content)
    params = {}
    # Start with defaults from meta
    effective_params = meta["params"] or meta.get("fallback_params") or []
    for p in effective_params:
        if "default" in p:
            params[p["key"]] = p["default"]
    # Apply frontmatter values
    for k, v in frontmatter.items():
        params[k] = str(v) if v is not None else ""
    # Apply UI overrides (these win)
    for k, v in params_override.items():
        if v:  # only override if non-empty
            params[k] = v

    # Handle logo
    logo_path = None
    if logo_bytes:
        allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".svg"}
        ext = Path(logo_filename).suffix.lower() if logo_filename else ".png"
        if ext not in allowed_exts:
            ext = ".png"
        logo_file = session_dir / f"logo{ext}"
        logo_file.write_bytes(logo_bytes)
        logo_path = str(logo_file)

    # Step 4: pandoc md → typst body
    body_typ = session_dir / "body.typ"
    run_cmd(
        ["pandoc", str(input_md), "--to=typst", "--wrap=none", "-o", str(body_typ)],
        cwd=str(session_dir),
    )

    # Step 5: strip pandoc preamble from body.typ
    body_content = body_typ.read_text(encoding="utf-8")
    body_content = strip_pandoc_typst_preamble(body_content)

    # Step 6: render Jinja2 wrapper
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    env.policies["json.dumps_kwargs"] = {"ensure_ascii": False}
    tmpl = env.get_template(meta["wrapper_template"])

    main_color = params.get("main-color", "E94845").lstrip("#")

    # Build typst_params: serialized Typst expressions for auto-generated wrappers
    # that use {{ typst_params["key"] }} syntax.
    typst_params_serialized: dict[str, str] = {}
    for p in effective_params:
        if p.get("type") == "file":
            continue
        key = p["key"]
        val = params.get(key, p.get("default", ""))
        typst_params_serialized[key] = serialize_typst_value(val, p)

    main_typ_content = tmpl.render(
        template_path=str(template_dir),
        title=params.get("title", ""),
        subtitle=params.get("subtitle", ""),
        author=params.get("author", ""),
        affiliation=params.get("affiliation", ""),
        year=params.get("year", ""),
        class_=params.get("class", ""),
        main_color=main_color,
        logo_path=logo_path,
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        body=body_content,
        typst_params=typst_params_serialized,
    )

    main_typ = session_dir / "main.typ"
    main_typ.write_text(main_typ_content, encoding="utf-8")

    # Step 7: typst compile
    font_path = str(template_dir / "fonts")
    output_pdf = session_dir / "output.pdf"
    run_cmd(
        ["typst", "compile", str(main_typ), str(output_pdf), "--font-path", font_path, "--root", "/"],
        cwd=str(session_dir),
    )

    # Step 8: pandoc → docx
    output_docx = session_dir / "output.docx"
    run_cmd(
        ["pandoc", str(input_md), "-o", str(output_docx)],
        cwd=str(session_dir),
    )

    # Step 9: pandoc → odt
    output_odt = session_dir / "output.odt"
    run_cmd(
        ["pandoc", str(input_md), "-o", str(output_odt)],
        cwd=str(session_dir),
    )

    build_id = str(uuid.uuid4())
    build_meta = {
        "build_id": build_id,
        "theme_id": safe_template_id,
        "theme_version": resolved_template_version,
        "converter_version": CONVERTER_VERSION,
        "typst_version": _read_cmd_version("typst"),
        "pandoc_version": _read_cmd_version("pandoc"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Persist build metadata with the generated artifacts for reproducibility.
    (session_dir / "build_meta.json").write_text(json.dumps(build_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "pdf": str(output_pdf),
        "docx": str(output_docx),
        "odt": str(output_odt),
        "build": build_meta,
    }


def strip_pandoc_typst_preamble(content: str) -> str:
    """Remove pandoc-generated typst preamble lines, keep document body."""
    lines = content.split("\n")
    # Find where the actual content starts (after #set, #show, etc. preamble)
    # Pandoc typst output starts with things like:
    # #set document(...) #set page(...) etc.
    # We want to skip those and keep heading/paragraph content
    # Simple approach: skip leading lines that start with #set or #show or are blank
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#set ") or stripped.startswith("#show ") or stripped == "":
            start = i + 1
        else:
            break
    body = "\n".join(lines[start:])
    # Pandoc can emit #horizontalrule, which is not available in Typst 0.11.
    return body.replace("#horizontalrule", "#line(length: 100%)")


def run_cmd(cmd: list, cwd: str = None, timeout: int = 60):
    """Run a subprocess command, raise RuntimeError on failure.

    cmd is always a list (never passed to a shell), so shell injection is not possible.
    """
    result = subprocess.run(  # noqa: S603 – list form, shell=False
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {cmd[0]} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _read_cmd_version(cmd: str) -> str:
    """Read tool version in a fail-safe way for build metadata."""
    try:
        result = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"

    if result.returncode != 0:
        return "unknown"

    first_line = (result.stdout or "").splitlines()
    return first_line[0].strip() if first_line else "unknown"
