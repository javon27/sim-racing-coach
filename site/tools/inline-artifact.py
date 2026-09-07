"""Build a single self-contained HTML file from index.html for publishing as a
Claude Artifact: inlines every local asset (src/href="assets/...") as a data: URI
and strips the doctype/html/head/body wrapper the Artifact skeleton supplies.

Usage:  python tools/inline-artifact.py [out.html]      # default: tools/dist/nestor-artifact.html
"""
import base64, re, sys
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent          # .../site
html = (SITE / "index.html").read_text(encoding="utf-8")

MIME = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}

def data_uri(rel: str) -> str:
    p = SITE / rel
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{MIME[p.suffix.lower()]};base64,{b64}"

html = re.sub(r'(src|href)="(assets/[^"]+)"',
              lambda m: f'{m.group(1)}="{data_uri(m.group(2))}"', html)

for tag in ("<!doctype html>", '<html lang="en">', "</html>",
            "<head>", "</head>", "<body>", "</body>"):
    html = html.replace(tag, "")

out = Path(sys.argv[1]) if len(sys.argv) > 1 else SITE / "tools" / "dist" / "nestor-artifact.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(html.strip() + "\n", encoding="utf-8")
leftover = re.findall(r'(?:src|href)="assets/[^"]+"', out.read_text(encoding="utf-8"))
print("wrote", out, out.stat().st_size, "bytes; leftover local refs:", leftover)
