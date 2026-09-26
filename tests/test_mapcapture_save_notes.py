# tests/test_mapcapture_save_notes.py
# POST /mapcapture/save REPLACES the track file, so it is a second writer of the
# same authored-notes overlay as PUT /tracks/{slug}/map. It used to build its
# payload from scratch with no notes key at all, so the Tracks -> Edit -> "Rebuild
# from saved capture" -> "Save ribbon" flow silently deleted every authored note
# for that track. Both writers now route through the one _carry_notes owner, so
# these assert the SAME rule as test_server_tracks.py's #37 cases.
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simcoach.analysis import geometry_clause
from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore
from tests.conftest import with_map_rev

TRACK = "Algarve International Circuit"
SLUG = "algarve_international_circuit"


def _client(tmp_path, corner_labels=("1", "2")):
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    (tracks_dir / f"{SLUG}.json").write_text(json.dumps({
        "track": SLUG,
        "path": [[float(i), float(i % 3)] for i in range(120)],
        "path_dist": [float(i) for i in range(120)],
        "corners": [_corner(i, lab) for i, lab in enumerate(corner_labels, start=1)],
    }))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks_dir))
    return app, TestClient(app, base_url="http://127.0.0.1"), tracks_dir


def _corner(number, label):
    apex = 20.0 * number
    return {"number": number, "label": label, "name": f"Turn {label}",
            "direction": "right", "start_dist": apex - 10.0, "apex_dist": apex,
            "end_dist": apex + 10.0, "apexes": [apex]}


def _ribbon(labels):
    # The shape mapcapture_build pins into app.state.last_ribbon. Geometry values
    # are irrelevant here -- only the corner set drives the note-carry decision.
    n = 120
    world = {"left": [[float(i), 5.0] for i in range(n)],
             "right": [[float(i), -5.0] for i in range(n)],
             "center": [[float(i), 0.0] for i in range(n)],
             "width": [10.0] * n, "bank": [0.0] * n,
             "dist": [float(i) for i in range(n)]}
    return {"world": world, "corners": [_corner(i, lab) for i, lab in enumerate(labels, 1)],
            "sectors": [], "sections": [], "track": TRACK}


def _put_notes(client, corners):
    return client.put(f"/tracks/{SLUG}/notes", json={
        "track": TRACK, "rev": client.get(f"/tracks/{SLUG}/notes").json()["rev"],
        "notes": {"overview": "rhythm track", "corners": corners,
                  "reference_times": {"GT3": "1:32.500"}, "source": "me"}})


def _written(tracks_dir: Path) -> dict:
    return json.loads((tracks_dir / f"{SLUG}.json").read_text())


def test_mapcapture_save_preserves_authored_notes(tmp_path):
    # The no-op case: the rebuild lands the same corner count and the same labels,
    # so EVERY authored field survives the ribbon save.
    app, client, tracks_dir = _client(tmp_path)
    assert _put_notes(client, {"1": "brake late", "2": "flat in 6th"}).status_code == 200
    app.state.last_ribbon = _ribbon(["1", "2"])

    r = client.post("/mapcapture/save", json={})

    assert r.status_code == 200, r.text
    # Assert the FILE, not the status code -- the bug returned 200 while deleting.
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"1": "brake late", "2": "flat in 6th"}
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"
    assert len(written["corners"]) == 2          # the ribbon's geometry still landed
    assert written["ribbon"]["width"]            # ...including the new surface


def test_mapcapture_save_drops_a_note_whose_nearest_corner_already_belongs_to_another(
        tmp_path, caplog):
    # Was "drops corner notes when the corner count changes", which deleted EVERY
    # per-corner note whenever the solver found one more corner anywhere on the lap.
    # What this pins: T2 (40 m) has no corner of its own after the rebuild. Its nearest
    # new corner is T1 at 20 m, which T1's exact match already holds, so T2 has no second
    # choice -- even though the rebuild's 70 m corner is within 50 m of it. T2's note goes,
    # LOUDLY and with its text; T1's stays.
    app, client, tracks_dir = _client(tmp_path)
    assert _put_notes(client, {"1": "brake late",
                               "2": "the slow left hairpin - huge patience"}).status_code == 200
    rib = _ribbon(["1", "2", "3"])
    for c, apex in zip(rib["corners"][1:], (70.0, 90.0)):
        c.update(apex_dist=apex, start_dist=apex - 10.0, end_dist=apex + 10.0, apexes=[apex])
    app.state.last_ribbon = rib

    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = client.post("/mapcapture/save", json={})

    assert r.status_code == 200, r.text
    assert "huge patience" in caplog.text       # recoverable from the log, not silent
    assert "the corners they name no longer exist" in caplog.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"1": "brake late"}   # not rebound to the new T2
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"


def test_mapcapture_save_moves_notes_onto_relabelled_corners(tmp_path, caplog):
    # Was "drops orphaned notes on relabel". Same corners, GP-style relabelling ('2' ->
    # '2A'): the corner at 40 m is the one the note was written about, so the note
    # follows it instead of being deleted (#64) -- and the move is surfaced in the log.
    app, client, tracks_dir = _client(tmp_path)
    assert _put_notes(client, {"1": "brake late", "2": "middle of the chicane"}).status_code == 200
    app.state.last_ribbon = _ribbon(["1", "2A"])

    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = client.post("/mapcapture/save", json={})

    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"1": "brake late", "2A": "middle of the chicane"}
    assert written["notes"]["overview"] == "rhythm track"
    assert 'moved 1 authored' in caplog.text and '"2": "2A"' in caplog.text


def test_mapcapture_save_of_corner_less_rebuild_keeps_every_note(tmp_path):
    # A thin 2-pass capture can yield ZERO solver corners, and this route still saves
    # (it exists to persist the reconstructed SURFACE -- see
    # test_server_reads.py::test_mapcapture_save_writes_track_json, which asserts that
    # deliberately). The empty corner set used to read as a count change and drop EVERY
    # per-corner note, in dev straight into the shipped tracks dir. Zero new corners
    # can't rebind anything, so there is nothing to protect against: keep them all.
    app, client, tracks_dir = _client(tmp_path)
    assert _put_notes(client, {"1": "brake late", "2": "flat in 6th"}).status_code == 200
    app.state.last_ribbon = _ribbon([])

    r = client.post("/mapcapture/save", json={})

    assert r.status_code == 200, r.text          # the surface still persists
    written = _written(tracks_dir)
    assert written["corners"] == []
    assert written["notes"]["corners"] == {"1": "brake late", "2": "flat in 6th"}
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"


def test_mapcapture_save_keeps_range_keyed_notes(tmp_path):
    # A per-corner note may name a RANGE ('2-4', '11-13' -- 16 of the 135 shipped keys,
    # 3,341 bytes across 5 tracks). With the corner count unchanged and every corner it
    # names still present, a ribbon save must keep it.
    labels = ["1", "2", "3", "4"]
    app, client, tracks_dir = _client(tmp_path, labels)
    assert _put_notes(client, {"2-4": "the whole esses complex, one flowing arc"}).status_code == 200
    app.state.last_ribbon = _ribbon(labels)

    assert client.post("/mapcapture/save", json={}).status_code == 200

    notes = _written(tracks_dir)["notes"]["corners"]
    assert notes["2-4"] == "the whole esses complex, one flowing arc"


def test_both_track_file_writers_agree_on_which_notes_survive(tmp_path):
    # THE TRIPWIRE for the one-owner invariant, and the reason it is written as an
    # equivalence rather than as "a range note survives".
    #
    # git merges the _carry_notes DEFINITION cleanly and both call sites cleanly --
    # only _persist_map's inline block conflicts. So a resolution that keeps main's
    # block leaves _carry_notes alive as an orphaned SECOND owner, still wired to
    # mapcapture_save, and every "Save ribbon" silently deletes the 16 range-keyed
    # notes (3,341 bytes across 5 tracks) that main's note_key_binds exists to keep.
    #
    # Asserting "'2-4' survives" cannot catch that here: this branch predates
    # note_key_binds, so such a test is red until the merge and green after -- it
    # would have to ship failing. Asserting the two writers AGREE is green under
    # either rule applied to both, and red the moment they drift apart, which is the
    # actual failure mode. The prior notes carry a RANGE key so the divergence the
    # bad resolution produces lands inside this assertion.
    labels = ["1", "2", "3", "4"]
    prior = {"1": "brake late", "2-4": "the whole esses complex, one flowing arc"}

    # Editor save (PUT .../map) -- main's block lives here.
    _, client_a, dir_a = _client(tmp_path / "editor", labels)
    assert _put_notes(client_a, prior).status_code == 200
    r = client_a.put(f"/tracks/{SLUG}/map", json=with_map_rev(client_a, f"/tracks/{SLUG}/map", {
        "corners": [{"number": i, "label": lab, "name": f"Turn {lab}",
                     "apex_dist": 20.0 * i} for i, lab in enumerate(labels, 1)]}))
    assert r.status_code == 200, r.text

    # Ribbon save (POST /mapcapture/save) -- _carry_notes is wired here.
    app_b, client_b, dir_b = _client(tmp_path / "ribbon", labels)
    assert _put_notes(client_b, prior).status_code == 200
    app_b.state.last_ribbon = _ribbon(labels)
    assert client_b.post("/mapcapture/save", json={}).status_code == 200

    editor, ribbon = _written(dir_a)["notes"], _written(dir_b)["notes"]
    # Fixture sanity: both actually wrote notes, so equality below isn't vacuous.
    assert editor["corners"].get("1") == "brake late"
    assert ribbon["corners"].get("1") == "brake late"
    # One rule, one owner: the same prior notes and the same corner set must survive
    # a track-file write identically, whichever route performed it.
    assert ribbon == editor


# --- #64 x #65: one writer, identity-keyed notes -----------------------------------------

_LABELS = ["1", "2", "3", "4"]                  # apexes 20/40/60/80 m, per _corner
_PRIOR = {"1": "the hairpin at 20 m", "3-4": "the chicane at 60-80 m"}


def _save_via(writer, app, client, labels):
    """Drive one of the FOUR writers of a track file (#65), by the route the app uses."""
    if writer == "editor":
        return client.put(f"/tracks/{SLUG}/map", json=with_map_rev(client, f"/tracks/{SLUG}/map", {"corners": [
            {"number": i, "label": lab, "name": f"Turn {lab}", "apex_dist": 20.0 * i}
            for i, lab in enumerate(labels, 1)]}))
    if writer == "ribbon":
        app.state.last_ribbon = _ribbon(labels)
        return client.post("/mapcapture/save", json={})
    if writer == "rotation":
        return client.post(f"/tracks/{SLUG}/rotation", json=with_map_rev(client, f"/tracks/{SLUG}/rotation", {"degrees": 90}))
    assert writer == "notes"
    # What the Tracks screen does: read the whole notes object, PUT it straight back.
    got = client.get(f"/tracks/{SLUG}/notes").json()
    return client.put(f"/tracks/{SLUG}/notes",
                      json={"track": TRACK, "notes": got, "rev": got["rev"]})


def test_all_four_writers_keep_the_same_notes_across_a_no_op_save(tmp_path):
    # The cross-writer equivalence. Four routes write a track file; before #65 they did
    # four different things to the notes overlay, and "Save ribbon" deleted all of it.
    # Same prior notes -- a plain key AND a range key -- same corners, every writer: the
    # survivors must be identical, and identical to what was authored.
    results = {}
    for writer in ("editor", "ribbon", "rotation", "notes"):
        app, client, tracks_dir = _client(tmp_path / writer, _LABELS)
        assert _put_notes(client, _PRIOR).status_code == 200
        r = _save_via(writer, app, client, _LABELS)
        assert r.status_code == 200, (writer, r.text)
        results[writer] = _written(tracks_dir)["notes"]
    assert results["editor"]["corners"] == _PRIOR          # not vacuously equal
    assert results["editor"]["reference_times"] == {"GT3": "1:32.500"}
    for writer, notes in results.items():
        assert notes == results["editor"], writer


def test_a_relabel_moves_the_notes_through_either_corner_writer_and_nothing_undoes_it(tmp_path):
    # THE cross case between the two fixes. #65 moved the note carry INTO the one writer,
    # keyed on "the caller passed no notes"; #64 rebinds a note by identifying corners
    # against the file ON DISK. The shape that makes one remove what the other needs is a
    # writer whose payload already holds the notes -- the cheap #65 fix ("seed `out` from
    # _track_file_data") -- because then the guard never runs and a relabel keeps '3-4'
    # verbatim, which is #64's misbind exactly. So: relabel 1-4 -> 3-6 (the corners do not
    # move) through each writer that CAN relabel, then run the two seeded-from-disk
    # writers over the result, and check what the COACH serves, not just the file.
    shifted = ["3", "4", "5", "6"]
    expected = {"3": _PRIOR["1"], "5-6": _PRIOR["3-4"]}
    for writer in ("editor", "ribbon"):
        app, client, tracks_dir = _client(tmp_path / writer, _LABELS)
        assert _put_notes(client, _PRIOR).status_code == 200
        assert _save_via(writer, app, client, shifted).status_code == 200
        assert _written(tracks_dir)["notes"]["corners"] == expected, writer
        for follow in ("rotation", "notes"):
            assert _save_via(follow, app, client, shifted).status_code == 200
            assert _written(tracks_dir)["notes"]["corners"] == expected, (writer, follow)
        ctx = CoachContext(store=None, cmap=CornerMap.from_data(_written(tracks_dir)),
                           track=TRACK, car="GT3")
        assert ctx._corner_note("3") == _PRIOR["1"], writer       # the 20 m corner, renamed
        assert ctx._corner_note("4") is None, writer              # 40 m: nobody wrote about it
        assert ctx._corner_note("5") == _PRIOR["3-4"], writer     # the chicane, renamed
        assert ctx._corner_note("6") == _PRIOR["3-4"], writer


def test_every_writer_hands_the_dropped_notes_back_to_the_client(tmp_path):
    # A drop that only reaches the log is a drop the driver never hears about. Every
    # writer's response carries `dropped_notes` (key -> text): the two that rebuild the
    # corner set report what they dropped -- here T4's note, its corner deleted -- and the
    # two that never touch corners report nothing, in the same shape.
    for writer, expected in (("editor", {"4": "T4 at 80 m"}), ("ribbon", {"4": "T4 at 80 m"}),
                             ("rotation", {}), ("notes", {})):
        app, client, tracks_dir = _client(tmp_path / writer, _LABELS)
        assert _put_notes(client, {"1": "T1 at 20 m", "4": "T4 at 80 m"}).status_code == 200
        r = _save_via(writer, app, client, _LABELS[:3])
        assert r.status_code == 200, (writer, r.text)
        assert r.json()["dropped_notes"] == expected, writer
        kept = _written(tracks_dir)["notes"]["corners"]
        assert kept.get("1") == "T1 at 20 m", writer            # a real save, not a no-op
        assert ("4" in kept) == (not expected), writer          # reported iff it is gone


def _disk(tracks_dir):
    return tracks_dir / f"{SLUG}.json"


@pytest.mark.parametrize("writer", ["ribbon", "rotation", "notes"])
def test_no_writer_commits_a_section_the_app_cannot_open(tmp_path, writer):
    # #65: only the editor's save validated `sections`; the other three wrote them as
    # handed, and CornerMap.from_data RAISES on a backwards span -- the track is then dead
    # for every client. Refused (422) and the file on disk is left byte-for-byte alone.
    app, client, tracks_dir = _client(tmp_path, _LABELS)
    assert _put_notes(client, _PRIOR).status_code == 200
    data = json.loads(_disk(tracks_dir).read_text())
    junk = [{"name": "x", "start_dist": 90.0, "end_dist": 10.0}]
    with pytest.raises(ValueError):
        CornerMap.from_data({**data, "sections": junk})       # the failure is real
    if writer == "ribbon":
        app.state.last_ribbon = {**_ribbon(_LABELS), "sections": junk}
    else:
        _disk(tracks_dir).write_text(json.dumps({**data, "sections": junk}))
    before = _disk(tracks_dir).read_bytes()
    r = _save_via(writer, app, client, _LABELS) if writer != "ribbon" else \
        client.post("/mapcapture/save", json={})
    assert r.status_code == 422, (writer, r.text)
    assert _disk(tracks_dir).read_bytes() == before


def test_a_seeded_writer_strips_unusable_geometry_and_writes_sectors_as_numbers(tmp_path):
    # #65, through the rotation writer, which rewrites the whole file from disk: a string
    # radius_m, a string banking_deg and a -1e9 width all used to go back out verbatim,
    # and `sectors` stayed strings. Unusable geometry is stripped (it is re-bakeable; a
    # string in it crashes the coach), usable geometry beside it is untouched.
    _, client, tracks_dir = _client(tmp_path, _LABELS)
    data = json.loads(_disk(tracks_dir).read_text())
    data["corners"][0].update(radius_m="banana", banking_deg="3", width_m=12.0)
    data["corners"][1]["width_m"] = -1e9
    data["sectors"] = ["30", "70"]
    _disk(tracks_dir).write_text(json.dumps(data))
    with pytest.raises((TypeError, ValueError)):
        geometry_clause(data["corners"][0], "right")          # the crash is real (banking "3")

    assert client.post(f"/tracks/{SLUG}/rotation", json=with_map_rev(client, f"/tracks/{SLUG}/rotation", {"degrees": 90})).status_code == 200

    written = json.loads(_disk(tracks_dir).read_text())
    c0, c1 = written["corners"][0], written["corners"][1]
    assert "radius_m" not in c0 and "banking_deg" not in c0
    assert c0["width_m"] == 12.0
    assert "width_m" not in c1
    assert written["sectors"] == [30.0, 70.0]
    assert [type(s) for s in written["sectors"]] == [float, float]
    for c in written["corners"]:
        geometry_clause(c, c["direction"])                    # must not raise


# No test for the "all notes dropped -> write no hollow notes block" arm of
# _carry_notes here: with the bug present this route wrote no notes key at all, so
# such a test passes either way and proves nothing. That arm is covered against the
# shared owner by test_server_tracks.py's
# test_save_track_map_has_notes_false_once_only_orphans_remained.
