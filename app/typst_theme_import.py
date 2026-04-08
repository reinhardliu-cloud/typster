import json
import re
from pathlib import Path


def _derive_package_name(package_spec: str | None, fallback: str) -> str:
	if not package_spec:
		return fallback
	match = re.match(r"^@?[^/]+/([^:]+)(?::(.+))?$", package_spec.strip())
	if not match:
		return fallback
	return match.group(1) or fallback


def _humanize_label(key: str) -> str:
	words = re.split(r"[-_]+", key.strip())
	return " ".join(word[:1].upper() + word[1:] for word in words if word)


def _escape_typst_content(value: str) -> str:
	escaped = value.replace("\\", "\\\\")
	escaped = escaped.replace("[", "\\[").replace("]", "\\]")
	escaped = escaped.replace("#", "\\#")
	return escaped


def serialize_typst_value(value: object, param_def: dict) -> str:
	text = "" if value is None else str(value)
	kind = str(param_def.get("typst_kind", "string"))
	if kind == "content":
		return f"[{_escape_typst_content(text)}]"
	if kind == "raw":
		return text
	return json.dumps(text, ensure_ascii=False)


def _find_matching_delimiter(text: str, start_index: int, open_char: str, close_char: str) -> int:
	depth = 0
	in_string: str | None = None
	escape = False
	i = start_index
	while i < len(text):
		ch = text[i]
		if in_string:
			if escape:
				escape = False
			elif ch == "\\":
				escape = True
			elif ch == in_string:
				in_string = None
			i += 1
			continue

		if ch in {'"', "'"}:
			in_string = ch
		elif ch == open_char:
			depth += 1
		elif ch == close_char:
			depth -= 1
			if depth == 0:
				return i
		i += 1
	raise ValueError("Unbalanced Typst delimiter while generating adapter")


def _split_top_level_args(block: str) -> list[str]:
	parts: list[str] = []
	current: list[str] = []
	depth_round = 0
	depth_square = 0
	depth_curly = 0
	in_string: str | None = None
	in_line_comment = False
	escape = False
	i = 0

	while i < len(block):
		ch = block[i]

		if in_line_comment:
			current.append(ch)
			if ch == "\n":
				in_line_comment = False
			i += 1
			continue

		if in_string:
			current.append(ch)
			if escape:
				escape = False
			elif ch == "\\":
				escape = True
			elif ch == in_string:
				in_string = None
			i += 1
			continue

		if ch == "/" and i + 1 < len(block) and block[i + 1] == "/":
			current.append("//")
			in_line_comment = True
			i += 2
			continue

		if ch in {'"', "'"}:
			in_string = ch
			current.append(ch)
		elif ch == "(":
			depth_round += 1
			current.append(ch)
		elif ch == ")":
			depth_round -= 1
			current.append(ch)
		elif ch == "[":
			depth_square += 1
			current.append(ch)
		elif ch == "]":
			depth_square -= 1
			current.append(ch)
		elif ch == "{":
			depth_curly += 1
			current.append(ch)
		elif ch == "}":
			depth_curly -= 1
			current.append(ch)
		elif ch == "," and depth_round == 0 and depth_square == 0 and depth_curly == 0:
			part = "".join(current).strip()
			if part:
				parts.append(part)
			current = []
		else:
			current.append(ch)
		i += 1

	tail = "".join(current).strip()
	if tail:
		parts.append(tail)
	return parts


def _infer_param_from_argument(key: str, raw_value: str, optional: bool) -> dict | None:
	value = raw_value.strip()
	if not value:
		return None

	kind = "raw"
	default = ""
	expose = True

	if value.startswith('"') and value.endswith('"') and len(value) >= 2:
		kind = "string"
		# Strip surrounding quotes; handle only basic backslash escapes
		inner = value[1:-1]
		default = inner.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")
	elif value.startswith("[") and value.endswith("]"):
		kind = "content"
		default = value[1:-1].strip().replace("\\\n", "\n")
	elif re.fullmatch(r"-?\d+(?:\.\d+)?", value):
		kind = "raw"
		default = value
	elif value in {"true", "false", "auto", "none"}:
		kind = "raw"
		default = value
	else:
		expose = False

	if optional:
		default = ""

	if not expose:
		return None

	return {
		"key": key,
		"type": "text",
		"label": _humanize_label(key),
		"required": False,
		"default": default,
		"typst_kind": kind,
		"typst_optional": optional,
	}


def _extract_show_with_template(main_content: str) -> tuple[str, str, list[dict], list[dict]] | None:
	show_match = re.search(r"#show\s*:\s*", main_content)
	if not show_match:
		return None

	with_match = re.search(r"\.with\s*\(", main_content[show_match.end():])
	if not with_match:
		return None

	with_start = show_match.end() + with_match.start()
	open_paren = show_match.end() + with_match.end() - 1
	close_paren = _find_matching_delimiter(main_content, open_paren, "(", ")")

	show_expr = main_content[show_match.end():with_start].strip()
	args_block = main_content[open_paren + 1:close_paren]
	prefix = main_content[:show_match.start()].rstrip()

	parsed_args: list[dict] = []
	params: list[dict] = []
	for snippet in _split_top_level_args(args_block):
		raw_snippet = snippet.strip()
		optional = False
		if raw_snippet.startswith("//"):
			optional = True
			raw_snippet = raw_snippet[2:].strip()
		match = re.match(r"([A-Za-z0-9_-]+)\s*:\s*(.+)\Z", raw_snippet, re.DOTALL)
		if not match:
			continue
		key = match.group(1)
		raw_value = match.group(2).strip()
		parsed_args.append({
			"key": key,
			"raw_value": raw_value,
			"optional": optional,
		})
		param = _infer_param_from_argument(key, raw_value, optional)
		if param:
			params.append(param)

	return prefix, show_expr, parsed_args, params


_IMAGE_RE = re.compile(r'image\((["\'])([^"\']+)\1\)')


def _fix_image_paths_for_jinja(raw_value: str) -> str:
	"""Replace image("relative") with image({{ template_path|tojson }} + "/relative")."""
	def _repl(m: re.Match) -> str:
		rel_path = m.group(2)
		return f'image({{{{ template_path|tojson }}}} + "/{rel_path}")'
	return _IMAGE_RE.sub(_repl, raw_value)


def _build_wrapper(prefix: str, show_expr: str, parsed_args: list[dict], params: list[dict]) -> str:
	param_map = {param["key"]: param for param in params}
	lines: list[str] = []
	if prefix.strip():
		lines.append(prefix.rstrip())
		lines.append("")

	lines.append(f"#show: {show_expr}.with(")
	for arg in parsed_args:
		key = arg["key"]
		param = param_map.get(key)
		if param:
			rendered = f'{{{{ typst_params[{json.dumps(key, ensure_ascii=False)}] }}}}'
			if param.get("typst_optional"):
				lines.append(
					f'{{% if params.get({json.dumps(key, ensure_ascii=False)}) %}}  {key}: {rendered},{{% endif %}}'
				)
			else:
				lines.append(f"  {key}: {rendered},")
		else:
			fixed_value = _fix_image_paths_for_jinja(arg["raw_value"])
			lines.append(f"  {key}: {fixed_value},")
	lines.append(")")
	lines.append("")
	lines.append("{{ body }}")
	lines.append("")
	return "\n".join(lines)


def _extract_directive_prefix(main_content: str) -> str:
	lines: list[str] = []
	for line in main_content.splitlines():
		stripped = line.strip()
		if not stripped:
			if lines:
				lines.append(line)
			continue
		if stripped.startswith(("#import ", "#set ", "#let ", "//")):
			lines.append(line)
			continue
		break
	return "\n".join(lines).rstrip()


def ensure_typst_init_adapter(theme_dir: Path, package_spec: str | None = None, meta: dict | None = None) -> dict:
	main_path = theme_dir / "main.typ"
	entrypoint = "main.typ"
	if not main_path.exists():
		fallback_main = theme_dir / "template" / "main.typ"
		if fallback_main.exists():
			main_path = fallback_main
			entrypoint = "template/main.typ"
	if not main_path.exists():
		if meta is None:
			raise ValueError("Cannot generate adapter without main.typ")
		return meta

	current_meta = dict(meta) if isinstance(meta, dict) else {}
	current_meta.setdefault("id", theme_dir.name)
	current_meta.setdefault("name", _derive_package_name(package_spec, theme_dir.name))
	current_meta.setdefault("description", f"Typst init template adapted from {package_spec or current_meta['name']}")
	current_meta.setdefault("scenario", "custom")
	current_meta.setdefault("source", "typst-init")
	if package_spec:
		current_meta.setdefault("source_ref", package_spec)

	main_content = main_path.read_text(encoding="utf-8")
	extracted = _extract_show_with_template(main_content)
	if extracted is None:
		prefix = _extract_directive_prefix(main_content)
		parsed_args: list[dict] = []
		params: list[dict] = []
		wrapper_content = (prefix + "\n\n" if prefix else "") + "{{ body }}\n"
	else:
		prefix, show_expr, parsed_args, params = extracted
		wrapper_content = _build_wrapper(prefix, show_expr, parsed_args, params)

	(theme_dir / "template.typ").write_text(main_content, encoding="utf-8")
	(theme_dir / "wrapper.typ.jinja").write_text(wrapper_content, encoding="utf-8")

	current_meta["wrapper_template"] = "wrapper.typ.jinja"
	current_meta["params"] = params
	current_meta["adapter_generated"] = True
	current_meta["adapter_mode"] = "typst-init-main"
	current_meta["adapter_entrypoint"] = entrypoint
	current_meta["adapter_args"] = [arg["key"] for arg in parsed_args]

	meta_path = theme_dir / "meta.json"
	meta_path.write_text(json.dumps(current_meta, ensure_ascii=False, indent=2), encoding="utf-8")
	return current_meta
