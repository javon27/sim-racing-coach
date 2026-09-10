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

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore

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
        "track": TRACK,
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


def test_mapcapture_save_drops_corner_notes_when_corner_count_changes(tmp_path, caplog):
    # Matches _persist_map exactly: a re-split chicane changes the corner count, so a
    # label-keyed note would rebind to a DIFFERENT corner -- and the coach states
    # authored notes as owned fact. Drop the corner-keyed ones, LOUDLY; keep the rest.
    app, client, tracks_dir = _client(tmp_path)
    assert _put_notes(client, {"1": "the slow left hairpin - huge patience"}).status_code == 200
    app.state.last_ribbon = _ribbon(["1", "2", "3"])

    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.post("/mapcapture/save", json={})

    assert r.status_code == 200, r.text
    assert "huge patience" in caplog.text       # recoverable from the log, not silent
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {}    # not rebound to the new T1/T2/T3
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"


def test_mapcapture_save_drops_orphaned_corner_notes_on_relabel(tmp_path, caplog):
    # Same corner count, but GP-style relabelling ('2' -> '2A') leaves a key that
    # names no corner. Unreachable to the coach -> drop it, keep the ones that bind.
    app, client, tracks_dir = _client(tmp_path)
    assert _put_notes(client, {"1": "brake late", "2": "middle of the chicane"}).status_code == 200
    app.state.last_ribbon = _ribbon(["1", "2A"])

    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.post("/mapcapture/save", json={})

    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"1": "brake late"}
    assert written["notes"]["overview"] == "rhythm track"
    # main's wording, not "labels no longer exist": under the range-aware predicate a
    # dropped '2-4' was never a label, so the old text misdescribed exactly the drops
    # that predicate exists to handle. Asserted so it can't be silently reverted.
    assert "the corners they name no longer exist" in caplog.text


@pytest.mark.xfail(strict=True, reason=(
    "POST-MERGE CONTRACT, not a flake: this branch forked before note_key_binds, so "
    "_carry_notes still tests the raw key against the labels and a range key cannot "
    "survive here -- the production code on this base CANNOT satisfy this test. It is "
    "strict on purpose: the moment app.py:702 becomes note_key_binds(k, labels) this "
    "XPASSes and strict turns that into a FAILURE, forcing whoever merges to delete "
    "this marker and leave a live permanent assertion. Do that rather than relaxing it. "
    "NOTE this does NOT catch the orphaned-_carry_notes resolution (it stays xfailed "
    "there) -- test_both_track_file_writers_agree_on_which_notes_survive is that "
    "tripwire. See the MERGE NOTE at app.py:694."))
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
    r = client_a.put(f"/tracks/{SLUG}/map", json={
        "corners": [{"number": i, "label": lab, "name": f"Turn {lab}",
                     "apex_dist": 20.0 * i} for i, lab in enumerate(labels, 1)]})
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

# No test for the "all notes dropped -> write no hollow notes block" arm of
# _carry_notes here: with the bug present this route wrote no notes key at all, so
# such a test passes either way and proves nothing. That arm is covered against the
# shared owner by test_server_tracks.py's
# test_save_track_map_has_notes_false_once_only_orphans_remained.
