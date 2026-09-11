"""Track resolution: a live track name maps to a corner-map file by a deterministic
slug of the (series-suffix-stripped, accent-normalized) full name — distinct layouts
get distinct files, and an unknown layout resolves to None so the caller auto-builds."""
import json
import logging
from pathlib import Path

import pytest

from simcoach.track_lookup import (
    guard_corner_notes,
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
                    "CORNER_NUM.test(label) && String(n) === label"):  # canonical labels
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
    # PARTIAL. The cap is 100 and the largest shipped map has 30 corners, so '1-30'
    # still blankets an entire real circuit and still binds. What the cap removes is
    # the key that blankets any map regardless of size, plus an unbounded probe loop.
    # Bounding the blast radius properly means a cap near the real complex width (the
    # widest shipped range is 3), which would also start dropping a sector-wide note
    # someone may have authored -- a call for the #64 work, not a side effect here.
    assert note_key_binds("1-100", {"50"})          # span 100: still a complex
    assert not note_key_binds("1-101", {"50"})      # wider: names nothing
    assert not note_key_binds("1-999", {"7"})       # the blanket key
    assert note_key_binds("1-999", {"1-999"})       # still its own label


def test_note_key_binds_is_strict_where_the_ui_is_now_strict_too():
    # These are the cases where the two readers used to disagree, all three classes of
    # them: a label `Number()` coerced to a covered integer ('07' is 7, a string probe
    # matches only '7'), a range endpoint of 4+ digits, and a span over _MAX_RANGE_SPAN.
    # The UI bound all of them and this guard bound none, which is the DATA-LOSS
    # direction -- the Tracks screen collapsed the note under a range row, telling the
    # author it was bound, while the next map save dropped it with only a log line.
    #
    # Closed in both directions now: TracksScreen was narrowed to match (its grammar is
    # pinned in the test above), and a node comparison over 360 key x label pairs shows
    # zero disagreements. Kept as the Python half of that invariant -- the JS half is
    # pinned by source literals, and this is what fails if the bound moves here.
    for label in ("07", "007", "0007", " 7 ", "7.0", "+7", "0x7", "1e1"):
        assert not note_key_binds("5-9", {label})   # non-canonical: only '7' is 7
    assert not note_key_binds("0-0", {""})          # an empty label is not 0
    assert not note_key_binds("1-1000", {"7"})      # 4-digit endpoint: not a range
    assert not note_key_binds("1-101", {"50"})      # over the span cap: not a range
    assert note_key_binds("5-9", {"7"})             # the canonical form still binds


def test_note_key_binds_never_parses_a_label():
    # STRUCTURAL, not guarded -- the point of the design, so don't "simplify" the probe
    # back into a label parse (tried in 019dc26, reverted in b181604). A corner label is
    # free text from the editor's label box. '²' U+00B2 and friends satisfy str.isdigit()
    # but RAISE on int(), so parsing the label side turned a typed superscript into an
    # HTTP 500 on save -- or, when `any` short-circuited on an earlier label first, a 200
    # that persisted the label and then broke every read of it. A guarded parse passes
    # this test too, which is exactly why the comment above the code says not to.
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
                  {"²", "7"}, {"²", "3A"}, {"100", "101"}, {"0"}, {""}]
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


def _corners(*labels, apex=10.0, step=10.0):
    """Corner dicts as a track JSON carries them: a label and an apex distance, the
    apex being what identifies a corner across a save."""
    return [{"number": i, "label": lab, "name": f"Turn {lab}",
             "apex_dist": apex + step * i}
            for i, lab in enumerate(labels)]


def _kept(old_notes_corners, old_labels, new_labels, **kw):
    notes = guard_corner_notes("algarve", {"corners": old_notes_corners},
                               _corners(*old_labels), _corners(*new_labels, **kw))
    return (notes or {}).get("corners", {})


def test_a_relabel_moves_a_note_onto_the_corner_it_was_written_about():
    # #64, the reported repro: shift every label by two (1,2,3,4 -> 3,4,5,6), which the
    # editor's free-text label box does in four keystrokes and which leaves the CORNER
    # COUNT unchanged. Keying on the count kept '3-4' verbatim, so a note about the last
    # chicane was then served as `Authored note:` -- owned fact, full confidence -- about
    # the first two corners of the lap. The corners did not move, so each is identified
    # by its apex and the key follows it.
    assert _kept({"3-4": "THE LAST CHICANE"}, "1234", "3456") == {"5-6": "THE LAST CHICANE"}
    assert _kept({"4": "THE LAST CORNER"}, "1234", "3456") == {"6": "THE LAST CORNER"}
    # The blanket key from the same report. It was authored about every corner on the
    # track, so naming them all is not a misbind and it is kept verbatim -- but it is now
    # kept because every corner it names is one it was written about, not because the
    # count happened to match. '1-999' is over the span cap and so names only itself,
    # which is why the reachable blanket key is this one.
    assert _kept({"1-99": "ALWAYS LIFT HERE"}, "1234", "3456") == {"1-99": "ALWAYS LIFT HERE"}
    assert _kept({"1-999": "ALWAYS LIFT HERE"}, "1234", "3456") == {}


def test_a_gp_style_relabel_keeps_the_note_instead_of_deleting_it():
    # The other half of #64: the old rule ALSO deleted. '2' -> '2A' made the key name no
    # corner, so hand-authored prose went in the log and out of the file. The corner is
    # the same corner; the note moves onto its new designation.
    assert _kept({"1": "brake late", "2": "middle of the chicane"},
                 ["1", "2"], ["1", "2A"]) == {"1": "brake late", "2A": "middle of the chicane"}
    # A range whose corners are all renamed cannot be expressed as one key, so it narrows
    # onto its first corner rather than being deleted.
    assert _kept({"2-3": "the chicane"},
                 ["1", "2", "3"], ["1A", "2A", "3A"]) == {"2A": "the chicane"}


def _at(*pairs):
    """Corners at explicit (label, apex) -- an insert or a delete moves the LABELS of
    the corners around it while leaving their apexes exactly where they were, which a
    positional fixture cannot express."""
    return [{"number": i, "label": lab, "name": f"Turn {lab}", "apex_dist": apex}
            for i, (lab, apex) in enumerate(pairs, 1)]


def test_an_insert_renumbers_the_lap_and_the_notes_follow_their_corners():
    # Corner count was the old test for "are these the same corners", so adding a corner
    # ANYWHERE deleted every per-corner note on the track -- which is how 135 authored
    # notes came within one save of being wiped. Here a new corner is inserted at 25 m
    # and takes the designation '3'; the corners at 30 m and 40 m become '4' and '5'.
    notes = guard_corner_notes(
        "algarve", {"corners": {"1": "hairpin", "3-4": "the esses"}},
        _at(("1", 10.0), ("2", 20.0), ("3", 30.0), ("4", 40.0)),
        _at(("1", 10.0), ("2", 20.0), ("3", 25.0), ("4", 30.0), ("5", 40.0)))
    assert notes["corners"] == {"1": "hairpin", "4-5": "the esses"}
    # The inserted corner inherits NOTHING -- the note that used to be keyed '3' is now
    # keyed '4', and '3' is the new corner nobody has written about yet.
    assert "3" not in notes["corners"]


def test_a_deleted_corner_drops_its_own_note_and_only_its_own():
    # The corner at 20 m is gone and the lap renumbers; its note cannot be bound to
    # anything and is dropped (with its text, in the log). '3-4' is KEPT as authored:
    # everything it still names -- corner '3', which is the 40 m corner it was written
    # about -- is a corner it was written about, so rewriting it would churn the author's
    # key for nothing. What it must never do is name the 20 m corner's replacement.
    notes = guard_corner_notes(
        "algarve", {"corners": {"2": "gone", "3-4": "the esses"}},
        _at(("1", 10.0), ("2", 20.0), ("3", 30.0), ("4", 40.0)),
        _at(("1", 10.0), ("2", 30.0), ("3", 40.0)))
    assert notes["corners"] == {"3-4": "the esses"}
    # '2' is a LIVE label after the delete (the 30 m corner), so a key-survives rule would
    # have kept "gone" and served it about the wrong corner. Nothing is bound to it.
    assert "2" not in notes["corners"]


def test_dragging_an_apex_keeps_the_note_on_that_corner():
    # The curator nudges a corner's apex in the editor. No apex matches any more, so
    # identity falls through to lap POSITION -- which is why the fallback exists: an
    # authored note must not evaporate because someone moved a marker a metre. It is a
    # position match, not a label match, so a relabel cannot ride on it (the test above
    # shows a same-count relabel still moves the key).
    notes = guard_corner_notes(
        "algarve", {"corners": {"2": "late apex"}},
        _at(("1", 10.0), ("2", 20.0), ("3", 30.0)),
        _at(("1", 11.0), ("2", 22.0), ("3", 33.0)))
    assert notes["corners"] == {"2": "late apex"}


def test_notes_are_carried_verbatim_when_either_side_has_no_corners():
    # Zero NEW corners cannot rebind anything, and a corner-less ribbon save is a real
    # flow (it persists the reconstructed surface). Zero OLD corners means there is
    # nothing to identify the notes against. Both used to delete every per-corner note --
    # the first even when the count matched, because an empty label set binds nothing.
    prior = {"1": "brake late", "2-4": "the esses"}
    assert _kept(prior, "1234", []) == prior
    assert _kept(prior, [], "1234") == prior
    assert _kept(prior, [], []) == prior


def test_dropped_note_log_survives_and_bounds_non_str_note_values(caplog):
    # A note VALUE is not known to be a str here. `PUT /notes` coerces with str(v); the
    # promote script does not, and this function stringifies only keys -- which is the
    # flip side of the bound living here to cover the promote path: less sanitised input,
    # so the bound must assume less about it. A map save also re-reads `notes` off disk,
    # so a legacy or hand-edited file reaches the same line.
    #
    # `v[:200]` raised on int/float/bool/None (a 500 on the map-save path) and KeyError on
    # a dict, and on a LIST it sliced 200 ELEMENTS, inverting the bound into an amplifier:
    # 25 keys of 1000 x 10,000-char lists logged 40 MB in ONE warning() call, 8.7x the
    # 4.6 MB the bound exists to prevent.
    values = {
        "1": ["x" * 10_000] * 1000,     # the amplifier: 200 ELEMENTS, not 200 chars
        "2": 7,                         # the crash: 'int' object is not subscriptable
        "3": 1.5,
        "4": True,
        "5": None,
        "6": {"nested": "y" * 10_000},  # KeyError: slice(None, 200, None)
        "7": "z" * 10_000,              # the str case, which always worked
    }
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        # Every corner the notes were written for is gone (different count, no shared
        # apex), so none of them can be rebound -> every note is dropped and logged.
        kept = guard_corner_notes("algarve", {"corners": values},
                                  _corners(*values), _corners("99", apex=999.0))
    assert kept is None          # every note dropped, nothing else authored -> hollow
    (rec,) = [r for r in caplog.records if "dropped" in r.getMessage()]
    msg = rec.getMessage()
    assert "dropped 7 authored" in msg                    # exact count, every type survived
    assert len(msg) < 2_000, f"log is {len(msg)} chars"   # 20 x 200 + preamble is the ceiling
    assert "x" * 201 not in msg and "z" * 201 not in msg  # no value contributes over 200


def test_dropped_note_log_bounds_a_list_note_value(caplog):
    # SEPARATE from the all-types test above, on purpose: there the int raises before the
    # list is ever formatted, so that test red-verifies the CRASH and can never observe
    # the amplifier. This one has no scalar to crash on, so it fails on SIZE.
    #
    # `v[:200]` on a list slices 200 ELEMENTS, not 200 characters. One such value among
    # ordinary string notes logged 2,000,982 chars (25 of them: 40,016,322). A partial fix
    # that stringified non-str SCALARS and left lists unsliced would pass every other test
    # in this file -- this is the one that catches it.
    values = {"1": "brake late", "2": ["x" * 10_000] * 200, "3": "flat in 6th"}
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        guard_corner_notes("algarve", {"corners": values},
                           _corners(*values), _corners("99", apex=999.0))
    (rec,) = [r for r in caplog.records if "dropped" in r.getMessage()]
    msg = rec.getMessage()
    assert len(msg) < 1_000, f"log is {len(msg)} chars"   # ~384 bounded, 2,000,982 was not
    assert "dropped 3 authored" in msg
