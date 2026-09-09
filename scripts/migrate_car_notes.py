"""One-time migration: fold each per-car simcoach/knowledge/<car>.md into a new
simcoach/cars/<slug>.json as a keyed `notes` object (overview / handling /
setup_concepts / driver_aids / sources). The <slug> (== the .md stem) is the slug of
the resolved car model name, so the coach's car_notes loader finds it unchanged.

Only the known CAR notes are migrated (track notes live in tracks/*.json already).
Provenance tags ([LMU]/[general-sim]/[needs-refinement]) are preserved verbatim.
`setup_concepts` is left empty (hand-authored later; the shared _lmu_setup_concepts
doc covers the general case). Sections that don't map to a known key are reported for
hand-finishing rather than dropped silently. Run once; the JSON output is committed."""
from __future__ import annotations

import json
import re
from pathlib import Path

# The real car notes to migrate (+ test_car for the test suite). Explicit so a track
# .md in knowledge/ is never mistaken for a car note.
CAR_STEMS = ("ferrari_296_lmgt3", "porsche_911_gt3_r_lmgt3", "test_car")


def _classify(heading: str) -> str | None:
    """Map a '## heading' to a notes key, or None if it doesn't fit a known field."""
    h = heading.lower()
    if "handling" in h:
        return "handling"
    if "driver aid" in h:
        return "driver_aids"
    if "sources" in h:
        return "sources"
    return None


def parse_md(md_text: str) -> tuple[dict, list[str]]:
    """(notes dict, list of unmapped section headings). Preamble (after the H1) ->
    overview; known sections -> their key verbatim; Sources -> a bullet list."""
    sections: dict[str, list[str]] = {"__preamble__": []}
    unmapped: list[str] = []
    cur = "__preamble__"
    for ln in md_text.splitlines():
        if ln.startswith("# ") and cur == "__preamble__" and not sections["__preamble__"]:
            continue  # drop the H1 title
        if ln.startswith("## "):
            heading = ln[3:].strip()
            key = _classify(heading)
            if key is None:
                unmapped.append(heading)
                cur = f"__skip__{heading}"
            else:
                cur = key
            sections.setdefault(cur, [])
            continue
        sections.setdefault(cur, []).append(ln)

    def _text(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    def _bullets(key: str) -> list[str]:
        out = []
        for ln in sections.get(key, []):
            s = ln.strip()
            if s.startswith("- "):
                out.append(s[2:].strip())
        return out

    notes = {
        "overview": _text("__preamble__"),
        "handling": _text("handling"),
        "setup_concepts": "",
        "driver_aids": _text("driver_aids"),
        "sources": _bullets("sources"),
    }
    return notes, unmapped


def _display_name(md_text: str, stem: str) -> str:
    m = re.match(r"#\s+(.+)", md_text.lstrip())
    if not m:
        return stem
    return re.sub(r"\s*tuning notes.*$", "", m.group(1)).strip() or stem


def migrate(knowledge_dir: Path, cars_dir: Path) -> None:
    cars_dir.mkdir(parents=True, exist_ok=True)
    for stem in CAR_STEMS:
        md = knowledge_dir / f"{stem}.md"
        if not md.exists():
            print(f"SKIP {stem}: no {md.name}")
            continue
        text = md.read_text(encoding="utf-8")
        notes, unmapped = parse_md(text)
        out = cars_dir / f"{stem}.json"
        out.write_text(
            json.dumps({"name": _display_name(text, stem), "notes": notes}, indent=2),
            encoding="utf-8",
        )
        msg = f"migrated {md.name} -> {out.name}"
        if unmapped:
            msg += f"  HAND-FINISH unmapped sections: {', '.join(unmapped)}"
        print(msg)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    migrate(root / "simcoach" / "knowledge", root / "simcoach" / "cars")
