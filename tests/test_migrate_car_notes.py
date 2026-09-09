"""The car-notes migration parser: keyed sections, preserved provenance, flagged
unmapped sections. Uses inline markdown (the source .md files were migrated + removed)."""
from scripts.migrate_car_notes import parse_md

_FERRARI_LIKE = """# Ferrari 296 GT3 tuning notes

Baseline knowledge only. [general-sim] tag marks cross-sim knowledge.

## Handling characteristics
- [general-sim] Mid-engine, well-balanced baseline.
- [LMU] High-speed rear snap if balance shifted forward. [needs-refinement]

## Driver aids (LMGT3 in LMU)
- [LMU] LMGT3 is the only LMU class with native ABS.

## Sources
- Coach Dave Academy LMU 296 guide: https://example.com/296
- OverTake LMGT3 ABS guide: https://example.com/abs
"""


def test_parse_md_round_trips_keyed_sections():
    notes, unmapped = parse_md(_FERRARI_LIKE)
    assert notes["overview"].startswith("Baseline knowledge only.")
    assert "well-balanced baseline" in notes["handling"]
    assert "native ABS" in notes["driver_aids"]
    assert notes["setup_concepts"] == ""            # left empty on migration
    assert notes["sources"] == [
        "Coach Dave Academy LMU 296 guide: https://example.com/296",
        "OverTake LMGT3 ABS guide: https://example.com/abs",
    ]
    assert unmapped == []


def test_parse_md_preserves_provenance_tags():
    notes, _ = parse_md(_FERRARI_LIKE)
    assert "[LMU]" in notes["handling"]
    assert "[needs-refinement]" in notes["handling"]
    assert "[general-sim]" in notes["overview"]


def test_parse_md_flags_unmapped_sections():
    md = _FERRARI_LIKE + "\n## Specs\n- flat-6, ~550hp\n\n## Driver-fit note\n- likes oversteer\n"
    notes, unmapped = parse_md(md)
    assert unmapped == ["Specs", "Driver-fit note"]     # reported, not silently dropped
    # Known sections still parse correctly alongside the unmapped ones.
    assert "native ABS" in notes["driver_aids"]


def test_parse_md_no_sections_is_all_overview():
    notes, unmapped = parse_md("# test_car tuning notes\n\n- Stock setup is stable.\n")
    assert "Stock setup is stable." in notes["overview"]
    assert notes["handling"] == "" and notes["sources"] == []
    assert unmapped == []
