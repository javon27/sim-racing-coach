"""Track resolution: a live track name maps to a corner-map file by a deterministic
slug of the (series-suffix-stripped, accent-normalized) full name — distinct layouts
get distinct files, and an unknown layout resolves to None so the caller auto-builds."""
import json
from pathlib import Path

import pytest

from simcoach.track_lookup import load_track_knowledge, resolve_track_map, track_slug

KNOWLEDGE = Path(__file__).resolve().parents[1] / "simcoach" / "knowledge"

SLUG_CASES = [
    ("Silverstone Grand Prix Circuit - ELMS", "silverstone_grand_prix_circuit"),  # series suffix dropped
    ("Silverstone National Circuit", "silverstone_national_circuit"),             # distinct from GP
    ("Circuit de la Sarthe", "circuit_de_la_sarthe"),
    ("Circuit de la Sarthe Mulsanne", "circuit_de_la_sarthe_mulsanne"),           # distinct layout
    ("Autódromo José Carlos Pace", "autodromo_jose_carlos_pace"),                 # accents normalized
    ("Circuit de Spa-Francorchamps", "circuit_de_spa_francorchamps"),             # inner hyphen kept
    ("Autodromo Nazionale Monza - WEC", "autodromo_nazionale_monza"),
]


@pytest.mark.parametrize("live,expected", SLUG_CASES)
def test_track_slug(live, expected):
    assert track_slug(live) == expected


def test_gp_and_national_resolve_to_different_maps(tmp_path):
    # Distinct layout names -> distinct slug files. Uses tmp fixtures rather than
    # shipped maps (real track JSONs are user-owned data, cleared/rebuilt in the editor).
    (tmp_path / "silverstone_grand_prix_circuit.json").write_text("{}")
    (tmp_path / "silverstone_national_circuit.json").write_text("{}")
    gp = resolve_track_map("Silverstone Grand Prix Circuit - ELMS", str(tmp_path))
    nat = resolve_track_map("Silverstone National Circuit", str(tmp_path))
    assert gp is not None and gp.stem == "silverstone_grand_prix_circuit"
    assert nat is not None and nat.stem == "silverstone_national_circuit"
    assert gp != nat


def test_series_suffix_shares_one_map():
    # ELMS vs WEC of the same track -> same slug -> same map file
    assert (track_slug("Silverstone Grand Prix Circuit - ELMS")
            == track_slug("Silverstone Grand Prix Circuit - WEC"))


def test_unknown_layout_returns_none(tmp_path):
    assert resolve_track_map("Nonexistent Raceway ZZZ", str(tmp_path)) is None


def test_load_map_data_merges_user_annotations_over_shipped_geometry(tmp_path):
    # Installed app: shipped geometry (path/edges/sectors) is authoritative; the
    # user's annotation overlay (corners/sections/rotation) layers on top.
    import json

    from simcoach.track_lookup import load_map_data

    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    slug = "autodromo_nazionale_monza"
    (bundled / f"{slug}.json").write_text(json.dumps({
        "path": [[0, 0], [1, 1]], "dist": [0, 100], "sectors": [33.0, 66.0],
        "rotation": 0.0,
        "corners": [{"number": 1, "name": "Turn 1", "apex_dist": 10.0,
                     "start_dist": 5.0, "end_dist": 15.0}],
    }))

    # No overlay yet -> shipped map verbatim.
    d = load_map_data("Autodromo Nazionale Monza", str(bundled), user_dir=str(user))
    assert d["corners"][0]["name"] == "Turn 1"

    # User renames the corner + rotates: annotations win, geometry stays shipped.
    (user / f"{slug}.json").write_text(json.dumps({
        "path": [[9, 9]], "sectors": [1.0],  # a hijacked-geometry overlay is ignored
        "rotation": 90.0,
        "corners": [{"number": 1, "name": "Rettifilo", "apex_dist": 10.0,
                     "start_dist": 5.0, "end_dist": 15.0}],
    }))
    d = load_map_data("Autodromo Nazionale Monza", str(bundled), user_dir=str(user))
    assert d["corners"][0]["name"] == "Rettifilo"   # user annotation applied
    assert d["rotation"] == 90.0                     # user annotation applied
    assert d["path"] == [[0, 0], [1, 1]]             # geometry stays authoritative
    assert d["sectors"] == [33.0, 66.0]              # geometry stays authoritative


def test_load_map_data_unshipped_track_is_wholly_user(tmp_path):
    import json

    from simcoach.track_lookup import load_map_data

    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    (user / "some_community_track.json").write_text(json.dumps({
        "path": [[0, 0]], "corners": [{"number": 1, "name": "T1", "apex_dist": 1.0,
                                       "start_dist": 0.0, "end_dist": 2.0}],
    }))
    d = load_map_data("Some Community Track", str(bundled), user_dir=str(user))
    assert d["path"] == [[0, 0]] and d["corners"][0]["name"] == "T1"
    # Neither shipped nor user -> empty.
    assert load_map_data("Nope Raceway", str(bundled), user_dir=str(user)) == {}


def test_load_map_data_reads_non_ascii_name_as_utf8(tmp_path):
    # Regression: the shipped JSON is UTF-8 on disk; reading it without an explicit
    # encoding falls back to the OS locale (cp1252 on Windows) and mojibakes accents
    # (issue #29 -- "AutÃ³dromo" instead of "Autódromo").
    from simcoach.track_lookup import load_map_data

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    slug = "autodromo_jose_carlos_pace"
    (bundled / f"{slug}.json").write_text(
        json.dumps({"name": "Autódromo José Carlos Pace"}, ensure_ascii=False),
        encoding="utf-8",
    )
    d = load_map_data("Autodromo Jose Carlos Pace", str(bundled))
    assert d["name"] == "Autódromo José Carlos Pace"


def test_load_map_data_merges_notes_overlay(tmp_path):
    from simcoach.track_lookup import load_map_data
    bundled = tmp_path / "bundled"; user = tmp_path / "user"
    bundled.mkdir(); user.mkdir()
    (bundled / "spa.json").write_text(json.dumps({
        "path": [[0, 0]], "corners": [], "notes": {"overview": "shipped", "corners": {}}}))
    (user / "spa.json").write_text(json.dumps({
        "notes": {"overview": "edited", "corners": {"1": "brake late"}}}))
    merged = load_map_data("Spa", str(bundled), str(user))
    assert merged["path"] == [[0, 0]]            # geometry from shipped
    assert merged["notes"]["overview"] == "edited"  # notes from overlay
    assert merged["notes"]["corners"]["1"] == "brake late"


def test_load_track_knowledge_reads_shipped_orphan_note():
    # Seven authored track notes ship ONLY as `knowledge/<slug>.md` — they have no
    # corner-map JSON at all, so this loader is the only way the coach reaches them.
    # Resolved from the live game name (series suffix and all) through track_slug.
    text = load_track_knowledge("Circuit de Spa-Francorchamps - WEC")
    assert "Spa-Francorchamps notes" in text
    # verbatim — the coach states these as owned fact, so nothing may be reformatted
    assert text == (KNOWLEDGE / "circuit_de_spa_francorchamps.md").read_text(encoding="utf-8")


def test_load_track_knowledge_decodes_utf8_not_cp1252(tmp_path):
    # Regression guard for issue #29: a bare Path.read_text() defaults to cp1252 on
    # Windows and mojibakes valid UTF-8 authored content.
    body = "# Sarthe\n\nMulsanne — the Porsche Curves, 250 km/h, ±3°C track temp.\n"
    (tmp_path / "circuit_de_la_sarthe.md").write_text(body, encoding="utf-8")
    assert load_track_knowledge("Circuit de la Sarthe", tmp_path) == body


def test_load_track_knowledge_missing_returns_empty(tmp_path):
    assert load_track_knowledge("No Such Circuit", tmp_path) == ""
    assert load_track_knowledge("", tmp_path) == ""


def test_every_shipped_knowledge_note_is_reachable():
    # A note's filename must be a fixed point of track_slug or the live name never
    # resolves to it and the authored content is silently unreachable.
    notes = sorted(KNOWLEDGE.glob("*.md"))
    assert len(notes) == 7
    for p in notes:
        assert track_slug(p.stem) == p.stem, p.name
        assert load_track_knowledge(p.stem).startswith("#"), p.name
