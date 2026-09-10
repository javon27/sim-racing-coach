# tests/test_server_tracks.py
import json
import logging
import shutil
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import simcoach
from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _seed_track(tracks_dir: Path, slug: str = "algarve_international_circuit") -> None:
    # Minimal shipped-shape fixture -- just enough for CornerMap.from_data
    # (see simcoach/corners.py) so draft-notes/put-notes/coverage have a
    # "shipped" track to work with, sandboxed away from the real repo file.
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / f"{slug}.json").write_text(json.dumps({
        "track": slug,
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))


def _make_client_with_fake_model(tmp_path, model_reply):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(model_reply))
    tracks_dir = tmp_path / "tracks"
    _seed_track(tracks_dir)
    client = TestClient(create_app(store, coach, tracks_dir=str(tracks_dir)),
                         base_url="http://127.0.0.1")
    return client, store


def test_draft_notes_returns_draft_without_writing(tmp_path):
    client, store = _make_client_with_fake_model(tmp_path, model_reply=json.dumps({
        "overview": "rhythm track", "corners": {"1": "brake late"}, "source": "draft"}))
    tracks_dir = Path(store.data_dir) / "tracks"
    before = set(tracks_dir.glob("*.json")) if tracks_dir.exists() else set()
    r = client.post("/tracks/algarve_international_circuit/draft-notes",
                     json={"track": "Algarve International Circuit", "guides": "outside line into T1"})
    assert r.status_code == 200
    draft = r.json()["draft"]
    assert draft["overview"] == "rhythm track"
    assert draft["corners"]["1"] == "brake late"
    after = set(tracks_dir.glob("*.json")) if tracks_dir.exists() else set()
    assert before == after                     # endpoint wrote nothing


def test_draft_notes_bad_slug_400(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.post("/tracks/..%2f..%2fetc/draft-notes", json={"track": "../../etc"})
    assert r.status_code in (400, 404)


def test_draft_notes_no_map_404(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.post("/tracks/nonexistent_track_xyz/draft-notes",
                     json={"track": "Nonexistent Track XYZ"})
    assert r.status_code == 404


def test_draft_notes_bad_model_json_502(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="not json")
    r = client.post("/tracks/algarve_international_circuit/draft-notes",
                     json={"track": "Algarve International Circuit"})
    assert r.status_code == 502


def test_put_notes_persists_and_reads_back(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm", "corners": {"1": "brake late"}, "source": "me"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    # A fresh GET of the track's map data now carries the notes overlay.
    got = client.get("/tracks").json()
    row = next(t for t in got if t["slug"] == "algarve_international_circuit")
    assert row["has_notes"] is True


def test_get_tracks_lists_coverage(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    rows = client.get("/tracks").json()
    assert any(t["slug"] == "algarve_international_circuit" and t["has_map"] for t in rows)


def test_put_notes_reference_times_roundtrip(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "x", "reference_times": {"GT3": "1:32.500"}}})
    assert r.status_code == 200
    written = json.loads((tmp_path / "tracks" / "algarve_international_circuit.json").read_text())
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}


def test_get_notes_no_notes_returns_empty_shape(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/algarve_international_circuit/notes")
    assert r.status_code == 200
    assert r.json() == {"overview": "", "corners": {}, "reference_times": {}, "source": ""}


def test_get_notes_missing_track_returns_empty_shape(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/nonexistent_track_xyz/notes")
    assert r.status_code == 200
    assert r.json() == {"overview": "", "corners": {}, "reference_times": {}, "source": ""}


def test_track_map_by_slug_no_path_returns_empty(tmp_path):
    # The seed fixture has corners but no frozen path -> no canonical layout yet.
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/algarve_international_circuit/map")
    assert r.status_code == 200 and r.json() == {}


def test_track_map_by_slug_bad_slug_400(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/..%2f..%2fetc/map")
    assert r.status_code in (400, 404)


def test_track_map_by_slug_returns_layout(tmp_path):
    # A track file with a frozen path yields a built canonical map (path + corners),
    # no lap needed -- the Tracks-screen view path.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel("{}"))
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    path = [[float(i), float(i % 3)] for i in range(120)]
    (tracks_dir / "algarve_international_circuit.json").write_text(json.dumps({
        "track": "algarve_international_circuit",
        "path": path,
        "path_dist": [float(i) for i in range(120)],
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))
    client = TestClient(create_app(store, coach, tracks_dir=str(tracks_dir)),
                        base_url="http://127.0.0.1")
    r = client.get("/tracks/algarve_international_circuit/map")
    assert r.status_code == 200
    m = r.json()
    assert m.get("path") and len(m["corners"]) == 1


def _client_with_frozen_path(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel("{}"))
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / "algarve_international_circuit.json").write_text(json.dumps({
        "track": "algarve_international_circuit",
        "path": [[float(i), float(i % 3)] for i in range(120)],
        "path_dist": [float(i) for i in range(120)],
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))
    client = TestClient(create_app(store, coach, tracks_dir=str(tracks_dir)),
                        base_url="http://127.0.0.1")
    return client, tracks_dir


def test_save_track_map_by_slug_persists(tmp_path):
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    r = client.put("/tracks/algarve_international_circuit/map", json={
        "corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0},
            {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 80.0},
        ]})
    assert r.status_code == 200, r.text
    written = json.loads((tracks_dir / "algarve_international_circuit.json").read_text())
    assert len(written["corners"]) == 2
    # Direction is server-derived, never trusted from the client.
    assert all(c["direction"] in ("left", "right", "straight") for c in written["corners"])


def _written(tracks_dir):
    return json.loads((tracks_dir / "algarve_international_circuit.json").read_text())


def _put_notes(client, corners):
    return client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm track", "corners": corners,
                  "reference_times": {"GT3": "1:32.500"}, "source": "me"}})


def test_save_track_map_by_slug_preserves_authored_notes(tmp_path):
    # Issue #37: re-saving a track's layout (PUT .../map) must not silently wipe
    # the authored notes overlay -- it's expensive to author. The no-op case: same
    # corner count, same labels -> everything is carried across unchanged.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    _put_notes(client, {"1": "brake late"})
    r = client.put("/tracks/algarve_international_circuit/map", json={
        "corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0},
        ]})
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["corners"] == {"1": "brake late"}
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}


def test_save_track_map_drops_corner_notes_when_corner_count_changes(tmp_path, caplog):
    # Issue #37 (rework): a rebuild may re-derive a chicane as TWO corners and
    # renumber everything after it, so a label-keyed note would silently rebind to
    # a DIFFERENT corner -- and the coach states authored notes as owned fact.
    # A changed corner count -> drop per-corner notes; keep the fields that are
    # not corner-keyed (overview / reference_times / source).
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    _put_notes(client, {"1": "the slow left hairpin - huge patience"})
    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.put("/tracks/algarve_international_circuit/map", json={
            "corners": [
                {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 18.0},
                {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 24.0},
            ]})
    assert r.status_code == 200, r.text
    # Not silent, and the dropped text is in the log so it can be recovered.
    assert "huge patience" in caplog.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {}          # not rebound to a new T1/T2
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"


def test_save_track_map_drops_orphaned_corner_notes_on_relabel(tmp_path):
    # Issue #37 (rework): same corner count, but GP-style relabelling ('2' -> '2A'
    # /'2B') leaves note keys that no longer name any corner. Those are silently
    # unreachable -- drop them, keep the labels that still exist.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
             {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 50.0},
             {"number": 3, "label": "3", "name": "Turn 3", "apex_dist": 80.0}]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": three}).status_code == 200
    _put_notes(client, {"1": "brake late", "2": "middle of the chicane", "3": "flat in 6th"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 50.0},
        {"number": 3, "label": "2B", "name": "Turn 2B", "apex_dist": 80.0},
    ]})
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"1": "brake late"}   # '2'/'3' orphaned
    assert written["notes"]["overview"] == "rhythm track"


def _four_corners():
    return [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
            for n in (1, 2, 3, 4)]


def test_save_track_map_keeps_range_keyed_notes(tmp_path):
    # A per-corner note key may name a RANGE ('2-4') -- that is how a complex gets
    # annotated, and 16 of the 135 per-corner keys across the shipped maps look like
    # this. '2-4' is never itself a corner label, so testing the raw key against the
    # labels dropped every range-keyed note as "stale" on a plain no-op re-save,
    # even though every corner it names still existed.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    _put_notes(client, {"1": "brake late", "2-4": "the fast left-right-left"})
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    assert _written(tracks_dir)["notes"]["corners"] == {
        "1": "brake late", "2-4": "the fast left-right-left"}


def test_save_track_map_keeps_range_note_when_only_some_of_its_corners_survive(tmp_path):
    # Partial survivor. '1-3' still names corners 1 and 3 after the middle one is
    # relabelled '2A', and the coach still serves the note for those, so keeping it
    # loses nothing. It is also no more exposed to renumbering than a plain '1' key,
    # which the same rule already keeps -- so a range is judged the same way a single
    # label is: keep it while it names a corner that exists.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
             for n in (1, 2, 3)]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": three}).status_code == 200
    _put_notes(client, {"1-3": "the downhill complex"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 40.0},
        {"number": 3, "label": "3", "name": "Turn 3", "apex_dist": 60.0},
    ]})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"1-3": "the downhill complex"}


def test_save_track_map_drops_range_note_when_no_corner_it_names_survives(tmp_path):
    # GUARD (passes before the fix too -- the old code dropped every range key
    # unconditionally, so this case cannot be made red). Pins the other side of the
    # rule: a range naming nothing that still exists is unreachable, so it goes.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
             for n in (1, 2, 3)]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": three}).status_code == 200
    _put_notes(client, {"2-3": "the chicane"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1A", "name": "Turn 1A", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 40.0},
        {"number": 3, "label": "3A", "name": "Turn 3A", "apex_dist": 60.0},
    ]})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {}


def test_save_track_map_drops_range_keyed_notes_when_corner_count_changes(tmp_path, caplog):
    # GUARD (passes before the fix too). #37's renumbering-safety arm must treat a
    # range key no differently: a changed corner count drops per-corner notes
    # wholesale, because every label downstream of a re-split complex shifts.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    _put_notes(client, {"2-4": "the fast left-right-left"})
    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.put("/tracks/algarve_international_circuit/map",
                       json={"corners": four[:3]})
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {}
    assert "the fast left-right-left" in caplog.text   # recoverable from the log
    assert "corner count changed" in caplog.text
    assert written["notes"]["overview"] == "rhythm track"


def test_save_track_map_accepts_corner_labels_int_cannot_parse(tmp_path):
    # A corner label is a FREE-TEXT box in the map editor (TrackMapEditor.svelte) and
    # the endpoint only truncates it, so typing '²' U+00B2 is a pure-UI action. '²',
    # '₂' U+2082 and '⑥' U+2465 all satisfy str.isdigit() and RAISE on int(), so the
    # note guard's range check turned one into an HTTP 500 on save. Worse, whether you
    # got the 500 or a 200 that persisted the label depended on `set` iteration order
    # reaching an ASCII label first -- the label then poisoned every later read.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    odd = [{"number": n, "label": lab, "name": f"Turn {lab}", "apex_dist": 20.0 * n}
           for n, lab in ((1, "1"), (2, "2"), (3, "²"), (4, "₂"), (5, "⑥"))]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": odd}).status_code == 200
    # A NON-binding range scans every label, so it always reaches the odd ones: the
    # deterministic 500. ('1-3' below instead short-circuits on ASCII '1' and saved a
    # 200, which is the worse half of the same bug -- which you got depended on `set`
    # iteration order.)
    _put_notes(client, {"90-95": "nothing here"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": odd})
    assert r.status_code == 200, r.text
    _put_notes(client, {"1-3": "the opening complex", "²": "the squared one"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": odd})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {
        "1-3": "the opening complex", "²": "the squared one"}
    # And the coach can still read every one of those labels back off the saved map.
    cmap = CornerMap.load(str(tracks_dir / "algarve_international_circuit.json"))
    ctx = CoachContext(store=None, cmap=cmap, track="Algarve", car="GT3")
    assert ctx._corner_note("²") == "the squared one"
    assert ctx._corner_note("2") == "the opening complex"
    assert ctx._corner_note("⑥") is None
    assert "the squared one" in ctx.get_track_notes(corner="²")


def test_save_track_map_survives_an_overlong_note_key_on_disk(tmp_path):
    # CPython caps int()/str() conversion at 4300 digits, so a 4301-digit note key made
    # every map save return 500 PERMANENTLY, with GET .../notes still 200 so the editor
    # looked fine. The key is reached before any comparison, so it also killed the
    # coach's read of corners that have nothing to do with it.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    poison = "1-" + "9" * 5000          # well past the 4300-digit int() threshold
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    # ON DISK, behind the endpoint: `PUT /notes` rejects a key this long now (see
    # test_put_notes_rejects_unbounded_input), but a file written before that cap
    # existed -- which is how this got reported -- still has to be survivable.
    data = _written(tracks_dir)
    data["notes"]["corners"][poison] = "nonsense"
    (tracks_dir / "algarve_international_circuit.json").write_text(json.dumps(data))
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": four})
    assert r.status_code == 200, r.text
    # The poison key names nothing, so it is dropped; the real note is kept.
    assert _written(tracks_dir)["notes"]["corners"] == {"1": "brake late"}
    cmap = CornerMap.load(str(tracks_dir / "algarve_international_circuit.json"))
    ctx = CoachContext(store=None, cmap=cmap, track="Algarve", car="GT3")
    assert ctx._corner_note("1") == "brake late"    # ordinary labels still resolve
    assert ctx._corner_note("99") is None


def test_self_save_of_shipped_imola_keeps_every_authored_note(tmp_path):
    # The sharpest check available: hand the editor Imola's OWN shipped map straight
    # back. Imola's notes are range-keyed ('2-4', '5-6', '11-13', '14-15', '17-18'),
    # and this self-save silently deleted all five -- 1000 characters of authored
    # prose -- on every map edit made through the app.
    slug = "autodromo_enzo_e_dino_ferrari"
    src = Path(simcoach.__file__).parent / "tracks" / f"{slug}.json"
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir()
    shutil.copy(src, tracks_dir / f"{slug}.json")   # a temp copy; never the repo file
    before = json.loads(src.read_text(encoding="utf-8"))
    assert any("-" in k for k in before["notes"]["corners"]), "fixture lost its ranges"
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks_dir)),
                        base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{slug}/map", json={
        "corners": before["corners"], "sectors": before.get("sectors") or [],
        "sections": before.get("sections") or [], "name": before.get("name")})
    assert r.status_code == 200, r.text
    after = json.loads((tracks_dir / f"{slug}.json").read_text(encoding="utf-8"))
    assert after["notes"] == before["notes"]        # every authored note, verbatim
    # Otherwise a no-op. Not byte-identical only because a self-save re-bakes
    # radius_m on 7 corners to different values than the shipped file carries --
    # a separate, pre-existing geometry drift, unrelated to notes.
    assert sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k)) == ["corners"]
    assert {f for b, a in zip(before["corners"], after["corners"])
            for f in set(b) | set(a) if b.get(f) != a.get(f)} == {"radius_m"}


def test_save_track_map_has_notes_false_once_only_orphans_remained(tmp_path):
    # The Tracks screen must not keep claiming a track has notes when every
    # authored per-corner note was dropped as unbindable.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "", "corners": {"1": "brake late"}}})
    assert client.get("/tracks").json()[0]["has_notes"] is True
    assert client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 18.0},
        {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 24.0},
    ]}).status_code == 200
    assert client.get("/tracks").json()[0]["has_notes"] is False
    assert "notes" not in _written(tracks_dir)   # nothing authored left to keep


def test_save_track_map_by_slug_rejects_empty_corners(tmp_path):
    client, _ = _client_with_frozen_path(tmp_path)
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": []})
    assert r.status_code == 422


def test_track_rotation_by_slug_persists(tmp_path):
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    r = client.post("/tracks/algarve_international_circuit/rotation", json={"degrees": 90})
    assert r.status_code == 200
    written = json.loads((tracks_dir / "algarve_international_circuit.json").read_text())
    assert written["rotation"] == 90.0


def test_put_then_get_notes_roundtrip(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm track", "corners": {"1": "brake late"},
                   "reference_times": {"GT3": "1:32.500"}, "source": "me"}})
    r = client.get("/tracks/algarve_international_circuit/notes")
    assert r.status_code == 200
    assert r.json() == {"overview": "rhythm track", "corners": {"1": "brake late"},
                         "reference_times": {"GT3": "1:32.500"}, "source": "me"}


def test_save_track_map_bakes_geometry_and_digest_uses_it(tmp_path):
    slug = "algarve_international_circuit"
    n = 120
    R = 60.0
    t = np.linspace(0.0, np.pi, n)                       # semicircle -> radius ~R
    path = [[float(R * np.cos(a)), float(R * np.sin(a))] for a in t]
    dist = [float(R * a) for a in t]                     # arc length
    width = [12.0] * n
    bank = [float(np.radians(2.0))] * n
    s, apex, e = dist[n // 4], dist[n // 2], dist[3 * n // 4]

    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / f"{slug}.json").write_text(json.dumps({
        "track": slug, "path": path, "path_dist": dist, "length": dist[-1],
        "ribbon": {"left": path, "right": path, "center": path, "width": width, "bank": bank},
        "corners": [{"number": 1, "label": "1", "name": "Turn 1", "direction": "left",
                     "start_dist": s, "apex_dist": apex, "end_dist": e, "apexes": [apex]}],
    }))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel('{"overview": "o", "corners": {}, "source": ""}')
    app = create_app(store, Coach(fake), tracks_dir=str(tracks_dir))
    c = TestClient(app, base_url="http://127.0.0.1")

    r = c.put(f"/tracks/{slug}/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1",
         "apex_dist": apex, "apexes": [apex], "start_dist": s, "end_dist": e}]})
    assert r.status_code == 200, r.text
    cc = json.loads((tracks_dir / f"{slug}.json").read_text())["corners"][0]
    assert abs(cc["radius_m"] - R) < 8.0                 # radius from path curvature
    assert cc["width_m"] == 12.0                         # from the ribbon
    assert abs(cc["banking_deg"] - 2.0) < 0.2            # radians -> degrees

    d = c.post(f"/tracks/{slug}/draft-notes", json={"track": slug})
    assert d.status_code == 200
    prompt = fake.last_messages[0]["content"]            # the digest sent to the model
    assert "radius ~" in prompt and "width 12m" in prompt


def test_put_notes_rejects_unbounded_input(tmp_path):
    # `PUT /tracks/{slug}/notes` validated NOTHING: no key count, no key length, no text
    # length, so a 2,000,000-character note body stored verbatim into the track file and
    # was carried across every later map save. The adjacent corner NAME has been capped
    # at 80 chars since this path was written -- this was the one endpoint on it with no
    # bounds at all. Over-limit input is REJECTED, never truncated: a silently shortened
    # note is the class of bug this whole area just got fixed for.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    keep = _written(tracks_dir)["notes"]

    over_limit = (
        {str(n): "x" for n in range(101)},     # 101 keys  (cap: 100, the corner cap)
        {"k" * 81: "x"},                       # 81-char key (cap: 80, the name cap)
        {"1": "x" * 2001},                     # 2001-char note (cap: 2000, ~300 words)
    )
    for corners in over_limit:
        r = _put_notes(client, corners)
        assert r.status_code == 422, r.text
        assert _written(tracks_dir)["notes"] == keep    # nothing persisted

    # overview/source are text on the same body, bounded the same way.
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "o" * 2001, "corners": {}}})
    assert r.status_code == 422, r.text
    assert _written(tracks_dir)["notes"] == keep

    # At the key-count, key-length and per-note limits SIMULTANEOUSLY still succeeds -- the
    # caps sit well clear of authored prose (the drafting prompt's budgets are 120 words of
    # overview, 50 words per corner). Not every cap at once, which is unsatisfiable by
    # design: the document cap below makes 100 maxed notes mutually exclusive with each
    # other. One maxed note on the longest allowed key, and 99 short ones.
    at_limit = {**{str(n): "y" for n in range(1, 100)}, "k" * 80: "z" * 2000}
    assert len(at_limit) == 100
    assert _put_notes(client, at_limit).status_code == 200
    assert _written(tracks_dir)["notes"]["corners"] == at_limit


def test_put_notes_refuses_an_oversized_notes_document(tmp_path):
    # Every field can pass its own cap and the DOCUMENT still be enormous: 100 corner keys
    # plus 100 reference times, each at the 2000-char field cap, put a 5 MB body on disk --
    # 2.5x the single-field abuse the per-field caps were added to stop, just spread across
    # 200 keys -- and every later map save re-read and re-wrote it.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    keep = _written(tracks_dir)["notes"]
    fat = {str(n): "x" * 2000 for n in range(1, 41)}   # 40 x 2000 > the 64,357-byte cap
    r = _put_notes(client, fat)
    assert r.status_code == 422, r.text
    assert _written(tracks_dir)["notes"] == keep       # authored notes untouched

    # The cap counts the ESCAPED bytes _write_track_json writes, not code points: an
    # astral char escapes to 12 bytes ("😀"), so 31 all-emoji notes measured
    # 62,367 code points, passed a code-point cap, and wrote 744,367 bytes to disk.
    emoji = {str(n): "\U0001f600" * 2000 for n in range(1, 32)}
    assert all(len(v) <= 2000 for v in emoji.values())   # every field within its own cap
    r = _put_notes(client, emoji)
    assert r.status_code == 422, r.text
    assert _written(tracks_dir)["notes"] == keep

    # And the ceiling the constant is derived from is genuinely accepted: 30 notes at the
    # full per-note cap plus overview and source at theirs. A round 64,000 would refuse it.
    ceiling = {str(n): "x" * 2000 for n in range(1, 31)}
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "o" * 2000, "corners": ceiling,
                  "reference_times": {}, "source": "s" * 2000}})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == ceiling


def test_put_notes_refuses_a_non_dict_corners_map(tmp_path):
    # `corners` as a list used to raise AttributeError -- a 500 that wrote NOTHING. The
    # first cut of the caps above coerced a non-dict to empty instead, which returned 200
    # and wiped the authored notes already on disk: a crash that preserves data beats a
    # success that destroys it. This write REPLACES what is on disk, so it must refuse.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late", "2": "turn in early"}).status_code == 200
    keep = _written(tracks_dir)["notes"]
    assert len(keep["corners"]) == 2
    for bad in ([["1", "brake late"]], "1: brake late", 7):
        r = _put_notes(client, bad)
        assert r.status_code == 422, r.text
        assert _written(tracks_dir)["notes"] == keep    # authored notes untouched


def test_dropped_note_log_is_bounded(tmp_path, caplog):
    # The drop-path warning json.dumps'd every dropped note IN FULL: a probe that stored
    # a large notes object made one warning() call write 4.6 MB. A log sink is not a
    # place unbounded user input gets to land, so it logs a bounded sample now -- the
    # exact count, plus enough of the keys/prose to re-author from.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    # THE most PUT /notes accepts at ~2000 chars a note: 31 of them, ~62 KB, just inside the
    # 64,357-byte document cap (32 is refused). That is the ceiling on how much this one log
    # call can ever be asked to write from this path -- and the bound is structural anyway,
    # a fixed 20 keys x 200 chars, so no accepted payload of any size or encoding moves it.
    assert _put_notes(client, {str(n): f"note {n} " + "x" * 1980
                               for n in range(1, 32)}).status_code == 200
    # The warning now comes from simcoach.track_lookup: the bound lives in
    # `guard_corner_notes`, which the promote script shares with this save path.
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = client.put("/tracks/algarve_international_circuit/map",
                       json={"corners": four[:3]})      # count change -> drop them all
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {}
    (rec,) = [x for x in caplog.records if "dropped" in x.getMessage()]
    msg = rec.getMessage()
    assert len(msg) < 10_000, f"warning record is {len(msg)} chars"   # was the whole ~62 KB
    assert "dropped 31 authored" in msg           # the exact count still reported
    assert '"1": "note 1 ' in msg                 # and a sample is still recoverable
