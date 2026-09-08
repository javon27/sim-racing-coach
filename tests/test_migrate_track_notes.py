from scripts.migrate_track_notes import parse_md

ALGARVE_HEAD = """# Autodromo Internacional do Algarve (Portimao) notes

4.653 km, 15 turns, WEC layout. Confidence/rhythm track.

## Corner-by-corner
- **T1 Primeira Curva:** Heavy downhill braking, brake earlier than it looks.
- **T10-11 Portimao Curve:** Double-apex right-hander, brake earlier than it looks.

## Common mistakes
Braking too late into downhill zones.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
"""


def test_parse_md_extracts_overview_corners_source():
    out = parse_md(ALGARVE_HEAD)
    assert "4.653 km" in out["overview"]
    assert out["corners"]["1"].startswith("Heavy downhill")
    assert out["corners"]["10-11"].startswith("Double-apex")
    assert "LMU wiki" in out["source"]


def test_parse_md_keys_by_label_not_name():
    out = parse_md(ALGARVE_HEAD)
    assert set(out["corners"]) == {"1", "10-11"}   # labels, not "Primeira Curva"


OTHER_CORNER_HEADING = """# Some Track notes

Overview line.

## Corner-by-corner
- **T1 Turn One:** Brake hard.

## Corner-map completeness
`tracks/some_track.json` covers 1 of 2 corners. T2 omitted, direction unknown.

Source: LMU wiki, summarized 2026-08-29.
"""


def test_other_corner_heading_falls_through_to_overview():
    """A '## Corner-map...' heading isn't the corner-by-corner section — its
    body must land in overview, not vanish."""
    out = parse_md(OTHER_CORNER_HEADING)
    assert "covers 1 of 2 corners" in out["overview"]
    assert set(out["corners"]) == {"1"}


MULTI_LABEL_BULLET = """# Some Track notes

Overview line.

## Corner-by-corner
- **T13 Left bend / T14 Middle kink / T15 Final kink:** All slight kinks,
  minimal steering.

Source: LMU wiki, summarized 2026-08-29.
"""


def test_multi_label_bullet_creates_an_entry_per_label():
    out = parse_md(MULTI_LABEL_BULLET)
    assert set(out["corners"]) == {"13", "14", "15"}
    for label in ("13", "14", "15"):
        assert out["corners"][label].startswith("All slight kinks")


BACKTICK_CONTINUATION = """# Some Track notes

Not enough data for `some_other_circuit` — skipped.
`another_circuit` too.

Source: LMU wiki, summarized 2026-08-29.
"""


def test_backtick_leading_line_kept_in_overview():
    out = parse_md(BACKTICK_CONTINUATION)
    assert "another_circuit" in out["overview"]
