"""Track resolution: a live track name maps to a corner-map file by a deterministic
slug of the (series-suffix-stripped, accent-normalized) full name — distinct layouts
get distinct files, and an unknown layout resolves to None so the caller auto-builds."""
import json
from pathlib import Path

import pytest

from simcoach.track_lookup import (
    load_track_knowledge,
    note_key_binds,
    resolve_track_map,
    track_slug,
)

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


# --- per-corner note-key grammar -------------------------------------------------
# Two shapes across all 12 shipped maps: a single label ('7', 119 keys) and an
# inclusive numeric range ('2-4', '11-13', 16 keys). Nothing else occurs.

def test_note_key_binds_single_label_is_plain_membership():
    assert note_key_binds("7", {"6", "7", "8"})
    assert not note_key_binds("7", {"6", "8"})


def test_note_key_binds_range_covers_every_corner_it_spans():
    # Endpoints AND the corners between them -- '11-13' names 12, and that is how
    # the coach's read path resolves it, so the save guard must agree.
    assert note_key_binds("11-13", {"12"})
    assert note_key_binds("11-13", {"11"})
    assert note_key_binds("11-13", {"13"})
    assert not note_key_binds("11-13", {"10", "14"})


def test_note_key_binds_range_survives_partially():
    assert note_key_binds("2-4", {"1", "2", "3A", "4A"})       # 2 is still there
    assert not note_key_binds("2-4", {"1", "2A", "3A", "4A"})  # names nothing left


def test_note_key_binds_unparseable_key_names_only_itself():
    # A GP-style '2A-2B' or a comma list is not a shape the coach's read path can
    # resolve, so it binds only on an exact label match -- keeping it on the
    # strength of its parts would preserve data nothing can ever serve.
    assert not note_key_binds("2A-2B", {"2A", "2B"})
    assert not note_key_binds("1,2", {"1", "2"})
    assert note_key_binds("2A-2B", {"2A-2B"})


def test_note_key_binds_absurd_range_is_bounded_not_resolved():
    # Authored keys are free text (`PUT /notes` validates nothing), and the groups are
    # fed to int(). A run of digits past CPython's 4300-digit int() limit made the
    # parse RAISE, which is a 500 on every later map save and a dead coach tool -- for
    # a key already persisted to disk. So the grammar bounds the groups at three
    # digits, the width of a real corner number, and anything longer is not a range:
    # it names only itself, like '2A-2B'.
    assert not note_key_binds("1-999999999", {"7"})
    assert not note_key_binds("1-" + "9" * 5000, {"7"})
    assert note_key_binds("1-999999999", {"1-999999999"})   # still its own label
    assert note_key_binds("100-102", {"101"})                # three digits is in grammar


def test_note_key_binds_descending_range_is_not_a_range():
    # A range is ASCENDING. fbdcb6d briefly sorted() the endpoints, which silently
    # widened the read path (main resolved '13-11' to nothing) and disagreed with the
    # UI, where `rangeOf` never normalises -- so the guard would keep a note that the
    # Tracks screen shows the user as matching no corner. Zero shipped keys are
    # inverted, so strict costs nothing and all three readers agree.
    assert not note_key_binds("13-11", {"12"})
    assert not note_key_binds("13-11", {"11"})
    assert not note_key_binds("13-11", {"13"})
    assert note_key_binds("13-11", {"13-11"})   # still its own label


def test_note_key_binds_matches_the_ui_grammar():
    # This grammar has a THIRD reader: TracksScreen.svelte's `rangeOf` decides which
    # notes collapse under a range row and which are flagged to the user as orphans.
    # A key the Python guard keeps -- and the coach then states as owned fact -- must
    # not be one the UI calls an orphan, so pin the JS source that has to agree. A
    # cross-language run would need node plus a Svelte parse; reading the literal is
    # the cheap version and still fails if someone edits it.
    from simcoach.track_lookup import _MAX_CORNER_DIGITS, _MAX_RANGE_SPAN
    svelte = (Path(__file__).resolve().parents[1]
              / "frontend" / "src" / "lib" / "TracksScreen.svelte").read_text(encoding="utf-8")
    for literal in (f"const MAX_CORNER_DIGITS = {_MAX_CORNER_DIGITS};",
                    f"const MAX_RANGE_SPAN = {_MAX_RANGE_SPAN};",
                    "String.raw`\\d{1,${MAX_CORNER_DIGITS}}`",      # the shared number
                    "hi < lo || hi - lo >= MAX_RANGE_SPAN ? null",  # ascending + capped
                    "const cover = CORNER_NUM.test(label)"):   # not Number.isInteger(n)
        assert literal in svelte, f"TracksScreen grammar changed, re-check agreement: {literal}"
    # Agreement on the keys that distinguish the two candidate grammars. Python is the
    # lenient one by default (`\d` is Unicode, and it could normalise direction), so
    # every row here is a case where it had to be held DOWN to the UI.
    ui_is_range = {"11-13": True, "2-4": True, "1-2": True,   # plain ascending: both
                   "13-11": False,        # UI never normalises -> neither do we
                   "١-٣": False,         # Number('١') is NaN; `\d` would have matched
                   "１-３": False,    # fullwidth: same trap
                   "²-³": False,        # isdigit(), not int()-able, not Number()-able
                   "2A-2B": False, "1,2": False, "7": False, "": False}
    for key, is_range in ui_is_range.items():
        # A range binds a label it spans; a non-range can only bind its own exact self.
        assert note_key_binds(key, {"1", "2", "3", "11", "12", "13"}) == is_range, key


def test_note_key_binds_wide_range_names_nothing():
    # A span wider than _MAX_RANGE_SPAN is not a complex. This bounds how many corners
    # ONE authored key can speak for: '1-999' bound every numeric corner on the track,
    # survived every same-count map save, and was served to the driver as an
    # `Authored note:` owned fact for each of them.
    #
    # PARTIAL, deliberately left that way. The cap is 100 and the largest shipped map
    # has 30 corners, so '1-30' still blankets an entire real circuit and still binds.
    # All the cap removes is the range that blankets a map of ANY size. Bounding the
    # blast radius is #64's job -- the real defect is that the guard keys on corner
    # COUNT, so a same-count relabel can rebind a note at all -- and tightening this
    # toward the real complex width (widest shipped range is 3) would start silently
    # dropping a plausible sector-wide note, which is the bug this branch exists to fix.
    assert note_key_binds("1-100", {"50"})          # span 100: still a complex
    assert not note_key_binds("1-101", {"50"})      # wider: names nothing
    assert not note_key_binds("1-999", {"7"})       # the blanket key
    assert note_key_binds("1-999", {"1-999"})       # still its own label


def test_note_key_binds_where_the_ui_used_to_disagree():
    # The cases the two readers used to split on. Each was the DATA-LOSS direction: the
    # Tracks screen collapsed the note under a range row, telling the author it was
    # bound to those corners, while the next map save dropped it with only a log line --
    # the same failure this branch exists to fix, so the grammars had to meet.
    #
    # They now agree on all of it: a node comparison running TracksScreen's own logic
    # against this function over 399 key x label pairs finds zero disagreements. This is
    # the Python half of that invariant; the JS half is pinned by source literals in the
    # test above, so moving the bound on either side breaks one of the two.
    for label in (" 7 ", "7.0", "+7", "0x7", "1e1", "0007", ""):
        assert not note_key_binds("5-9", {label})   # int() rejects; Number() coerced
    assert not note_key_binds("0-0", {""})          # an empty label is not 0
    assert not note_key_binds("1-1000", {"7"})      # 4-digit endpoint: not a range
    assert not note_key_binds("1-101", {"50"})      # over the span cap: not a range
    assert note_key_binds("5-9", {"7"})
    assert note_key_binds("5-9", {"07"})            # a leading zero binds on BOTH sides


def test_note_key_binds_ignores_labels_int_cannot_parse():
    # A corner label is free text from the editor's label box. '²' U+00B2 and friends
    # satisfy str.isdigit() but RAISE on int(), so parsing the label side turned a
    # typed superscript into an HTTP 500 on save -- or, when `any` short-circuited on
    # an earlier label first, a 200 that persisted the label and then broke every read
    # of it. The grammar never parses a label now; it compares as strings.
    for odd in ("²", "₂", "⑥"):        # ² ₂ ⑥ -- isdigit(), not int()-able
        assert odd.isdigit(), f"{odd!r} no longer exercises the isdigit()/int() gap"
        assert not note_key_binds("1-3", {odd})       # no raise, no spurious bind
        assert not note_key_binds("5-6", {"1", "2", odd})
        assert note_key_binds(odd, {odd})             # an exact label still binds
    # Arabic-Indic digits parse under int() but are not corner labels: ASCII only.
    assert not note_key_binds("1-3", {"١"})      # '١' == 1 under int()
    assert not note_key_binds("١-٣", {"2"})  # '١-٣' is not a range key


def test_note_key_binds_is_total_over_hostile_input():
    # The contract both callers rely on: a bool for every (key, labels) pair, never a
    # raise. Each is used as a predicate inside a comprehension, so one exception is a
    # 500 on a map save or a dead LLM-callable tool -- and the keys and labels both
    # come from unvalidated user input. This is the test that stops the whole class.
    labels = [set(), {"7"}, {"1", "2", "3"}, {"8A", "2A-2B"},
              {"²", "₂", "⑥"}, {"١"}, {""}, {" 7 "}, {"07"},
              {str(n) for n in range(1, 101)}]
    keys = ["7", "", " 7 ", "7 - 9", "-7", "7-", "7--9", "13-11", "7_0", "0x7", "0",
            "0-0", "1,2", "2A-2B", "١-٣", "１-３", "+1-3", "1-+3",
            "1.0-3", "1-3-5", "999-1", "1-" + "9" * 5000, "9" * 4301, "\n1-3",
            "1-3\n", "²-³", None, 7, 7.5, True, ("1-3",)]
    for key in keys:
        for labs in labels:
            out = note_key_binds(key, labs)   # must not raise
            assert out is True or out is False, f"{key!r}/{labs!r} -> {out!r}"


def test_note_key_binds_distributes_over_labels():
    # The equivalence the save guard's correctness argument rests on: asking about a
    # whole label set is the same as asking about each label alone. The coach's read
    # path only ever passes one label, the save guard passes the whole map -- if these
    # could disagree the guard would delete notes the coach still serves.
    keys = ["7", "11-13", "13-11", "1-100", "1-101", "2A-2B", "1-999999999", "0", ""]
    label_sets = [set(), {"7"}, {"12"}, {"11", "12", "13"}, {"8A", "12"},
                  {"²", "7"}, {"100", "101"}, {"0"}, {""}]
    for key in keys:
        for labs in label_sets:
            assert note_key_binds(key, labs) == any(note_key_binds(key, {lab}) for lab in labs), \
                f"{key!r} / {labs!r}"


def test_shipped_note_keys_all_bind_to_their_own_map():
    # Every authored per-corner note in the shipped set names a corner that exists.
    # A failure here means a shipped map's notes are already unreachable.
    tracks = Path(__file__).resolve().parents[1] / "simcoach" / "tracks"
    seen = 0
    for p in sorted(tracks.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        labels = {str(c.get("label") or c.get("number")) for c in (data.get("corners") or [])}
        for key in ((data.get("notes") or {}).get("corners") or {}):
            assert note_key_binds(str(key), labels), f"{p.name}: {key!r} names no corner"
            seen += 1
    assert seen > 100, "expected the shipped maps to carry authored per-corner notes"
