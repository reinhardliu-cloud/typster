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

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/tmp/sessions"))
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", APP_DIR / "templates"))
CUSTOM_TEMPLATES_DIR = Path(os.environ.get("CUSTOM_TEMPLATES_DIR", APP_DIR / "templates_custom"))


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
        return json.load(f)


def normalize_template_id(template_id: str) -> str:
    m = re.fullmatch(r'[a-zA-Z0-9_-]+', template_id)
    if not m:
        raise ValueError(f"Invalid template ID: {template_id!r}")
    return m.group(0)


def _load_templates_from_dir(templates_dir: Path) -> list[dict]:
    if not templates_dir.exists():
        return []

    templates = []
    for d in templates_dir.iterdir():
        if d.is_dir() and (d / "meta.json").exists():
            with open(d / "meta.json") as f:
                templates.append(json.load(f))
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
    templates = _load_templates_from_dir(TEMPLATES_DIR)
    templates.extend(_load_templates_from_dir(CUSTOM_TEMPLATES_DIR))

    if session_id:
        templates.extend(_load_templates_from_dir(get_session_themes_dir(session_id)))

    templates.sort(key=lambda t: (0 if str(t.get("id", "")).startswith("bubble") else 1, str(t.get("name", ""))))
    return templates


def convert(
    session_id: str,
    md_content: str,
    template_id: str,
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

    # Write full markdown (with frontmatter) for docx/odt
    input_md = session_dir / "input.md"
    input_md.write_text(md_content, encoding="utf-8")

    # Parse frontmatter and merge params (override wins over frontmatter)
    frontmatter = parse_frontmatter(md_content)
    params = {}
    # Start with defaults from meta
    for p in meta["params"]:
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

    return {
        "pdf": str(output_pdf),
        "docx": str(output_docx),
        "odt": str(output_odt),
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
