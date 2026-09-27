# tests/test_mapcapture_save_carries_keys.py
# #203: POST /mapcapture/save built its payload from scratch, so in dev (curating) mode a
# "Save ribbon" deleted every top-level key it did not list -- Monza lost its
# `pronunciations`. Save now starts from the existing file and replaces only the geometry
# it rebuilt. Fixture: the shipped Monza file (real key set, IPA table, notes with
# reference times) plus a top-level `reference_times` and an unknown key.
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.lmu import smm
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server import core as core_mod
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.track_lookup import TRACKS_DIR, dump_track_json, load_map_data

TRACK = "Autodromo Nazionale Monza"
SLUG = "autodromo_nazionale_monza"
EXTRA = {"reference_times": {"LMGT3": "1:48.900"}, "_future_key": {"kept": [1, "ü"]}}
# What Save rebuilds; everything else in the file is carried.
REPLACED = ("track", "name", "corners", "ribbon", "path", "path_dist", "sectors",
            "sections", "length", "rotation", "built_from")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(smm, "read_session_track_car", lambda: None)


def _shipped() -> dict:
    d = json.loads((TRACKS_DIR / f"{SLUG}.json").read_text(encoding="utf-8"))
    assert d.get("pronunciations") and d["notes"].get("reference_times")   # premise
    assert "built_from" not in d              # so a written one can only come from Save
    # A non-default rotation Save must keep, and a length the rebuilt one differs from.
    return {**d, "rotation": 37.0, "length": 5800.0, **EXTRA}


def _edge_laps(shipped):
    # Two bound laps 6 m either side of Monza's own outline, so Build reconstructs a
    # full-length ribbon and the shipped sections/sectors validate against its length.
    p = np.asarray(shipped["path"], dtype=float)
    d = np.asarray(shipped["path_dist"], dtype=float)
    t = np.gradient(p, axis=0)
    nrm = np.stack([-t[:, 1], t[:, 0]], axis=1) / np.linalg.norm(t, axis=1)[:, None]
    return [LapRecord("lmu", TRACK, "GT3", 110.0, True, 0.0,
                      {"pos_x": q[:, 0], "pos_y": np.zeros(len(q)), "pos_z": q[:, 1],
                       "lap_distance": d})
            for q in (p + 6.0 * nrm, p - 6.0 * nrm)]


def _build_save(tmp_path, monkeypatch, frozen):
    monkeypatch.setattr(core_mod.sys, "frozen", frozen, raising=False)
    shipped = _shipped()
    bundled = tmp_path / "tracks"
    bundled.mkdir()
    (bundled / f"{SLUG}.json").write_bytes(dump_track_json(shipped))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(bundled))
    c = TestClient(app, base_url="http://127.0.0.1")
    app.state.map_buffer = _edge_laps(shipped)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    r = c.post("/mapcapture/save", json={})
    assert r.status_code == 200, r.text
    return shipped, app, bundled, Path(store.data_dir) / "tracks"


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_dev_save_ribbon_keeps_every_key_it_does_not_rebuild(tmp_path, monkeypatch):
    shipped, _app, bundled, _user = _build_save(tmp_path, monkeypatch, frozen=False)
    out = _read(bundled / f"{SLUG}.json")
    carried = [k for k in shipped if k not in REPLACED and k != "notes"]
    assert set(carried) >= {"pronunciations", "reference_times", "_future_key"}
    assert {k: out.get(k) for k in carried} == {k: shipped[k] for k in carried}
    # notes ride _write_track_json's guard: the solver's corners differ, so per-corner
    # notes may move or drop, but the track-level fields always survive.
    for k in ("overview", "source", "reference_times"):
        assert out["notes"][k] == shipped["notes"][k]
    # The file's own key order, Save's new mark last: no reorder diff on a re-save.
    assert list(out) == list(shipped) + ["built_from"]


def test_dev_save_ribbon_still_replaces_what_it_rebuilt(tmp_path, monkeypatch):
    shipped, app, bundled, _user = _build_save(tmp_path, monkeypatch, frozen=False)
    out = _read(bundled / f"{SLUG}.json")
    world = app.state.last_ribbon["world"]
    assert out["path"] == world["center"] != shipped["path"]
    assert out["path_dist"] == world["dist"] != shipped["path_dist"]
    assert out["ribbon"] == {k: world[k] for k in ("left", "right", "center", "width", "bank")}
    assert out["ribbon"] != shipped["ribbon"]
    assert out["length"] == world["dist"][-1] != shipped["length"]
    assert out["built_from"] == "capture"
    keys = ("number", "label", "name", "direction", "start_dist", "apex_dist",
            "end_dist", "apexes")
    assert out["corners"] == [{k: c[k] for k in keys if k in c}
                              for c in app.state.last_ribbon["corners"]]
    # Kept by design, as before #203: the Build snapshot, the rotation and the name.
    assert out["sectors"] == shipped["sectors"] and out["sections"] == shipped["sections"]
    assert out["rotation"] == 37.0 and out["name"] == shipped["name"]


def test_installed_save_ribbon_leaves_the_bundle_and_shipped_keys_alone(tmp_path, monkeypatch):
    shipped, _app, bundled, user = _build_save(tmp_path, monkeypatch, frozen=True)
    assert (bundled / f"{SLUG}.json").read_bytes() == dump_track_json(shipped)   # untouched
    assert (user / f"{SLUG}.json").exists()                                     # the overlay
    # Non-annotation keys are the bundle's on read, whatever the overlay holds: a later
    # build shipping a new IPA table reaches this driver, it is not pinned by the copy.
    newer = {**shipped, "pronunciations": {"Ascari": "NEW"}, "_future_key": "NEW"}
    (bundled / f"{SLUG}.json").write_bytes(dump_track_json(newer))
    merged = load_map_data(TRACK, str(bundled), str(user))
    assert merged["pronunciations"] == {"Ascari": "NEW"} and merged["_future_key"] == "NEW"
    assert merged["rotation"] == 37.0      # an annotation field: the overlay's, as before


def test_installed_save_ribbon_on_an_unshipped_track_keeps_its_keys(tmp_path, monkeypatch):
    # No bundled map: the app-data file IS the whole map, so it must carry its own keys.
    monkeypatch.setattr(core_mod.sys, "frozen", True, raising=False)
    shipped = _shipped()
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    user = Path(store.data_dir) / "tracks"
    user.mkdir(parents=True)
    (user / f"{SLUG}.json").write_bytes(dump_track_json(shipped))
    (tmp_path / "tracks").mkdir()
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tmp_path / "tracks"))
    c = TestClient(app, base_url="http://127.0.0.1")
    app.state.map_buffer = _edge_laps(shipped)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    out = _read(user / f"{SLUG}.json")
    for k in ("pronunciations", "reference_times", "_future_key"):
        assert out[k] == shipped[k]
    assert not (tmp_path / "tracks" / f"{SLUG}.json").exists()
