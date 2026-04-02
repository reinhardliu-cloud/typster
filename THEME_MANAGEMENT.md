# Typst Theme Management

This document describes how to manage and install Typst themes for the Markdown-to-Typst converter.

## Overview

The converter supports three ways to access themes:

1. **Built-in Themes** - Curated templates (e.g., Bubble)
2. **Web UI Upload** - Upload custom theme .zip files directly in the browser
3. **CLI Installation** - Install themes from GitHub releases or local files using the command line

---

## Web UI Theme Upload

### How to Use

1. Open the converter web application
2. In the **Theme Selection** step, scroll to "Upload Theme Zip"
3. Click **"Upload Theme Zip"** button
4. Select a `.zip` file containing your theme package
5. The theme will be uploaded, validated, and added to your session

### Theme Package Format

A valid theme zip must contain:

```
your-theme.zip
├── meta.json              # Theme metadata (required)
├── template.typ           # Typst template (required)
├── wrapper.typ.jinja      # Jinja2 wrapper template (required)
├── fonts/                 # Font files (optional)
├── assets/                # Images and assets (optional)
└── ...                    # Other files
```

### meta.json Structure

```json
{
  "id": "my-theme",
  "name": "My Theme",
  "author": "Your Name",
  "description": "A brief description of the theme",
  "scenarios": ["letter", "invoice", "article"]
}
```

### Validation Rules

- ✓ Zip file must be under 5MB
- ✓ Must contain `meta.json`, `template.typ`, and `wrapper.typ.jinja`
- ✓ No path traversal attacks (../ paths blocked)
- ✓ Theme ID auto-generated from metadata

---

## CLI Theme Management

### Installation

The CLI tool is located at `app/cli.py`. It requires:
- Python 3.8+
- `gh` (GitHub CLI) for remote installation from releases

### Commands

#### List Installed Themes

List all themes in the custom templates directory:

```bash
python app/cli.py list
```

Output:
```
📚 Installed themes in /path/to/templates_custom:
  • graceful-genetics (custom-graceful-genetics-c0df1549)
    A paper template for journals and conferences
```

#### Install from Local File

Install a theme from a local .zip file:

```bash
python app/cli.py install /path/to/theme.zip
```

Optionally specify a custom installation directory:

```bash
python app/cli.py install /path/to/theme.zip --install-dir /custom/path
```

#### Install from GitHub Release

Install a theme from a GitHub repository's release assets:

```bash
python app/cli.py install-github owner/repo
```

This command:
1. Fetches the latest release from `owner/repo`
2. Downloads the `.zip` asset
3. Validates and installs the theme

Example:
```bash
python app/cli.py install-github typst/templates
```

### CLI Options

All commands support `--install-dir` to specify where to install themes:

```bash
python app/cli.py --install-dir /home/user/.typst/templates list
python app/cli.py --install-dir /home/user/.typst/templates install theme.zip
python app/cli.py --install-dir /home/user/.typst/templates install-github owner/repo
```

### Requirements for GitHub Installation

To use `install-github`, you need the GitHub CLI (`gh`) installed:

```bash
# macOS with Homebrew
brew install gh

# Ubuntu/Debian
sudo apt install gh

# Or download from https://cli.github.com
```

Authenticate with GitHub:

```bash
gh auth login
```

---

## Theme Location and Persistence

### Web UI (Session-scoped)

Themes uploaded via Web UI are temporarily stored in the session directory:
```
/tmp/sessions/{session-id}/themes/
```

These themes are available only for that session and are automatically cleaned up after 30 minutes of inactivity.

### Built-in Themes

Located in:
```
app/templates/
```

Example: `app/templates/bubble/`

### Installed Custom Themes

Located in:
```
app/templates_custom/
```

Custom themes installed via CLI or copied here are persistent across sessions and available to all users of the application.

---

## Example: Create and Install a Custom Theme

### Step 1: Create Theme Package

```bash
mkdir my-theme
cd my-theme

# Create metadata
cat > meta.json << EOF
{
  "id": "my-theme",
  "name": "My Custom Theme",
  "author": "Your Name",
  "description": "My custom document theme",
  "scenarios": ["letter", "article"]
}
EOF

# Create templates
cat > template.typ << 'EOF'
#let template(it) = {
  set page(number-align: center)
  it.body
}
EOF

cat > wrapper.typ.jinja << 'EOF'
{%- set title = title | default("Document") -%}
{%- set author = author | default("Author") -%}

#show heading.where(level: 1): it => {
  text(size: 28pt, weight: "bold", it.body)
}

= {{ title }}

_by {{ author }}_

{{ content }}
EOF

# Create zip
zip -r my-theme.zip meta.json template.typ wrapper.typ.jinja
```

### Step 2: Install Locally

```bash
python app/cli.py install my-theme.zip
```

### Step 3: Upload to GitHub (Optional)

Push to a GitHub repository and create a release:

```bash
gh release create v1.0.0 my-theme.zip
```

Then anyone can install it:

```bash
python app/cli.py install-github username/my-theme
```

---

## Troubleshooting

### "Theme package must be a .zip file"
- Ensure the file has the `.zip` extension
- Verify it's a valid zip archive: `unzip -t file.zip`

### "Theme package missing required files"
- Check that your zip contains: `meta.json`, `template.typ`, `wrapper.typ.jinja`
- File names are case-sensitive

### "Invalid JSON in meta.json"
- Validate JSON: `python -m json.tool meta.json`
- Ensure no trailing commas in JSON objects

### "gh CLI not found"
- Install GitHub CLI: https://cli.github.com
- Run `gh auth login` to authenticate

### "Failed to fetch releases"
- Verify the repository exists: `gh repo view owner/repo`
- Ensure you have access to (public or authenticated) repository

---

## Architecture

### Theme Discovery

Templates are discovered from multiple sources in this order:

1. **Built-in** (`TEMPLATES_DIR` = `app/templates/`)
2. **Installed Custom** (`CUSTOM_TEMPLATES_DIR` = `app/templates_custom/`)
3. **Session-scoped** (`get_session_themes_dir(session_id)` = `/tmp/sessions/{id}/themes/`)

All sources are merged and returned by `/api/templates`.

### Theme Installation Flow

```
User uploads .zip file
    ↓
Web UI POST /api/theme/upload
    ↓
theme_package.install_theme_package()
    ↓
Validate zip structure
    ↓
Validate required files
    ↓
Prevent path traversal attacks
    ↓
Extract to session themes directory
    ↓
Return theme metadata
    ↓
Add to template selection options
```

### CLI Installation Flow

```
User runs: cli.py install-github owner/repo
    ↓
Run: gh release download --remote {repo}
    ↓
Download latest .zip asset
    ↓
theme_package.install_theme_package()
    ↓
[Same validation as above]
    ↓
Extract to custom templates directory
    ↓
Persist for future use
```

---

## Security

Theme installation includes several security measures:

- ✓ **Path Traversal Prevention** - Blocks `../` and absolute paths in zip entries
- ✓ **File Size Limits** - Max 5MB per theme zip
- ✓ **Required Files Check** - Validates essential template files exist
- ✓ **JSON Validation** - Validates meta.json is parseable
- ✓ **Session Isolation** - Uploaded themes scoped to session only
- ✓ **Unique IDs** - Auto-generated IDs prevent collisions

---

## API Reference

### GET /api/templates

Returns list of available templates (built-in + custom + session-scoped).

**Response:**
```json
[
  {
    "id": "bubble",
    "name": "Bubble Template",
    "author": "Author Name",
    "description": "A colorful template",
    "scenarios": ["letter", "article"]
  },
  ...
]
```

### POST /api/theme/upload

Upload and install a theme in the current session.

**Parameters:**
- `theme_zip` (file) - .zip file containing theme package
- `session_id` (optional) - Session ID. Auto-generated if not provided

**Response:**
```json
{
  "session_id": "uuid-here",
  "template": {
    "id": "theme-id",
    "name": "Theme Name",
    "author": "Author",
    ...
  }
}
```

**Errors:**
- `400` - Invalid zip or missing required files
- `413` - File too large (>5MB)

---

## Contributing

To contribute a theme to the built-in collection:

1. Create a theme package following the format above
2. Submit as a GitHub release in a public repository
3. Open an issue or PR to request inclusion in the converter

Built-in themes should:
- Be well-documented
- Include example output
- Be production-ready
- Support multiple scenarios or have clear use case
