"""One-time migration: fold each simcoach/knowledge/<track>.md into the matching
simcoach/tracks/<track>.json as a `notes` object (overview + per-corner keyed by
label + source). Corner bullets follow `**T<label> <Name>:** <text>`. Run once;
the generated JSON is committed. Files that don't fit the pattern are reported for
hand-finishing rather than guessed."""
from __future__ import annotations

import json
import re
from pathlib import Path

_CORNER_HEAD_RE = re.compile(r"^\s*-\s*\*\*(T[0-9][^:]*):\*\*\s*(.+)$")
_LABEL_RE = re.compile(r"T([0-9]+(?:-[0-9]+)?[A-Za-z]?)\b")
_SOURCE_RE = re.compile(r"^Source:\s*(.+)$", re.MULTILINE)


def parse_md(md_text: str) -> dict:
    lines = md_text.splitlines()
    corners: dict[str, str] = {}
    overview_parts: list[str] = []
    in_corners = False
    cur_labels: list[str] = []
    for ln in lines:
        if ln.startswith("#"):                       # heading
            # Exact match: other "## Corner*" headings (e.g. "## Corner-map
            # completeness") are prose, not the per-corner bullet list.
            in_corners = ln.lower().startswith("## corner-by-corner")
            cur_labels = []
            continue
        m = _CORNER_HEAD_RE.match(ln)
        if m:
            header, text = m.group(1), m.group(2).strip()
            # A bullet can name several corners at once (e.g. "T13 .. / T14
            # .. / T15 ..:"); give each label the same (shared, not invented)
            # text rather than collapsing them under the first label only.
            cur_labels = _LABEL_RE.findall(header)
            for label in cur_labels:
                corners[label] = text
            continue
        if in_corners and cur_labels and ln.strip() and not ln.startswith("-"):
            for label in cur_labels:
                corners[label] += " " + ln.strip()   # wrapped continuation line
            continue
        if not in_corners and ln.strip() and not ln.startswith("Source:"):
            overview_parts.append(ln.strip())
    src = _SOURCE_RE.search(md_text)
    overview = " ".join(p for p in overview_parts if not p.startswith("#")).strip()
    return {"overview": overview, "corners": corners,
            "source": src.group(1).strip() if src else ""}


def migrate(knowledge_dir: Path, tracks_dir: Path) -> None:
    unmatched = []
    for md in sorted(knowledge_dir.glob("*.md")):
        tj = tracks_dir / f"{md.stem}.json"
        if not tj.exists():                          # car notes / no matching track map
            continue
        notes = parse_md(md.read_text(encoding="utf-8"))
        if not notes["corners"] and not notes["overview"]:
            unmatched.append(md.name); continue
        data = json.loads(tj.read_text(encoding="utf-8"))
        data["notes"] = notes
        tj.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"migrated {md.name} -> {tj.name} ({len(notes['corners'])} corners)")
    if unmatched:
        print("HAND-FINISH (did not fit the pattern):", ", ".join(unmatched))


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    migrate(root / "simcoach" / "knowledge", root / "simcoach" / "tracks")
