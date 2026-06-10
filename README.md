# Sunburst Disk Scanner

Install-free Python CLI for scanning a filesystem tree and writing a self-contained HTML disk usage report. It uses only the Python standard library and embeds vanilla JS/CSS directly in the generated report, so the HTML can be opened locally with `file://`.

The report renders a circular SVG sunburst diagram. The selected folder is shown in the center, child files and folders are shown as rings, and sector angles are proportional to scanned size. Click a folder sector, or a folder in the child list, to drill into it. Use `Back` and `Root` to navigate.

## Usage

Run from this project directory:

```bash
python scan.py [path] --output disk_report.html
```

If `path` is omitted, the current directory is scanned and `disk_report.html` is written in this project directory.

Examples:

```bash
python scan.py .
python scan.py "C:\ProgramData\MySQL" --output mysql_report.html
python scan.py D:\ --dated --output d_drive_report.html
```

## Smoke Check

```bash
python scan.py --smoke
```

Smoke mode creates a temporary directory tree with known files and folders, writes `_smoke_report.html` in this project directory, and validates expected scanned nodes plus basic report UI markers such as the sunburst chart and navigation controls.

## Filesystem Behavior

Symlinks and Windows reparse points are not followed by default. They are represented as `link` nodes, which avoids loops and keeps scans predictable.

Permission, missing-file, and other filesystem errors are captured on the affected node when possible. The scanner keeps going and the report surfaces the error in details/tooltips instead of failing the whole scan.

Empty or zero-size folders are valid input. The report shows an empty state when there are no child entries; when every child is zero bytes, sectors are shown with equal width so the structure is still visible.

## Report Contents

Generated reports are static and self-contained by default:

- No CDN or external assets.
- Embedded JSON scan data (all depth tiers inside the HTML for large scans).
- Embedded vanilla JavaScript and CSS.
- Open reports directly in a browser with `file://` — no local HTTP server required.
- Optional `--external-tiers` writes smaller HTML plus sidecar `.tier2.json` / `.tier3.json`
  files (requires a local HTTP server when viewing).

Tooltips include name, human-readable size, full path, type, and error details when present.

Privacy note: reports embed scanned names, sizes, and full local paths. Review the HTML before sharing it outside your machine.
