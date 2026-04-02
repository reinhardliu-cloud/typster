import json
import re
import uuid
import zipfile
from pathlib import Path

REQUIRED_FILES = {"meta.json", "template.typ", "wrapper.typ.jinja"}
MAX_THEME_ZIP_BYTES = 5 * 1024 * 1024


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value or "")
    slug = slug.strip("-")
    return slug[:40] or "theme"


def _validate_zip_entry(name: str) -> bool:
    # Disallow absolute paths and parent traversal.
    if name.startswith("/") or name.startswith("\\"):
        return False
    normalized = name.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    return all(part not in {".", ".."} for part in parts)


def _collect_required_present(entries: list[str]) -> set[str]:
    present = set()
    normalized = [e.replace("\\", "/") for e in entries if e and not e.endswith("/")]
    for entry in normalized:
        leaf = entry.rsplit("/", 1)[-1]
        if leaf in REQUIRED_FILES:
            present.add(leaf)
    return present


def _load_meta(meta_path: Path) -> dict:
    with meta_path.open("r", encoding="utf-8") as fh:
        meta = json.load(fh)
    if not isinstance(meta, dict):
        raise ValueError("meta.json must be a JSON object")
    if not isinstance(meta.get("name"), str) or not meta["name"].strip():
        raise ValueError("meta.json must include a non-empty string field: name")
    if "params" in meta and not isinstance(meta["params"], list):
        raise ValueError("meta.json field params must be an array")
    return meta


def install_theme_package(zip_bytes: bytes, session_themes_dir: Path) -> dict:
    if not zip_bytes:
        raise ValueError("Theme package is empty")
    if len(zip_bytes) > MAX_THEME_ZIP_BYTES:
        raise ValueError(f"Theme package is too large (max {MAX_THEME_ZIP_BYTES // (1024 * 1024)}MB)")

    session_themes_dir.mkdir(parents=True, exist_ok=True)

    tmp_zip = session_themes_dir / f"upload-{uuid.uuid4().hex}.zip"
    tmp_zip.write_bytes(zip_bytes)

    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            entries = zf.namelist()
            if not entries:
                raise ValueError("Theme package zip is empty")

            for entry in entries:
                if not _validate_zip_entry(entry):
                    raise ValueError("Theme package contains unsafe paths")

            present = _collect_required_present(entries)
            missing = REQUIRED_FILES - present
            if missing:
                missing_list = ", ".join(sorted(missing))
                raise ValueError(f"Theme package missing required files: {missing_list}")

            meta_candidates = [
                e for e in entries
                if e and not e.endswith("/") and e.replace("\\", "/").rsplit("/", 1)[-1] == "meta.json"
            ]
            if len(meta_candidates) != 1:
                raise ValueError("Theme package must contain exactly one meta.json")

            meta_entry = meta_candidates[0]
            root_prefix = ""
            if "/" in meta_entry:
                root_prefix = meta_entry.rsplit("/", 1)[0] + "/"

            # Require template.typ and wrapper.typ.jinja to live alongside meta.json for a strict contract.
            for required in ("template.typ", "wrapper.typ.jinja"):
                if f"{root_prefix}{required}" not in entries:
                    raise ValueError(
                        "Required files must be in the same directory as meta.json: "
                        "template.typ and wrapper.typ.jinja"
                    )

            theme_root_name = _sanitize_slug(Path(meta_entry).parent.name)
            theme_id = f"custom-{theme_root_name}-{uuid.uuid4().hex[:8]}"
            install_dir = session_themes_dir / theme_id
            install_dir.mkdir(parents=True, exist_ok=False)

            for member in zf.infolist():
                member_name = member.filename.replace("\\", "/")
                if member_name.endswith("/"):
                    continue
                rel_name = member_name
                if root_prefix:
                    if not member_name.startswith(root_prefix):
                        continue
                    rel_name = member_name[len(root_prefix):]
                    if not rel_name:
                        continue

                dest = install_dir / rel_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as dst:
                    dst.write(src.read())

        meta_path = install_dir / "meta.json"
        meta = _load_meta(meta_path)
        meta["id"] = theme_id
        meta.setdefault("description", "Uploaded custom theme")
        meta.setdefault("wrapper_template", "wrapper.typ.jinja")
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

        return meta
    except zipfile.BadZipFile as exc:
        raise ValueError("theme_zip must be a valid zip archive") from exc
    finally:
        tmp_zip.unlink(missing_ok=True)
