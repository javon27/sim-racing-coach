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
