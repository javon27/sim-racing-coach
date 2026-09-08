# tests/test_mapcapture_savecloud.py
# Save the raw mapping point cloud WITHOUT rebuilding geometry -- lets a track
# that already has a corner map (e.g. Laguna) gain a capture for later
# cross-section extraction. See the per-corner geometry spec.
import numpy as np
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _app(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(), "simcoach/knowledge"), tracks_dir=str(tmp_path / "tracks"))
    return app, store


def _tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def _edge(track, zsign, dist_max, *, elevation=True):
    ch = {"pos_x": np.array([0.0, 50.0, 100.0]),
          "pos_z": np.array([zsign * 5.0] * 3),
          "lap_distance": np.array([0.0, dist_max / 2, dist_max])}
    if elevation:
        ch["pos_y"] = np.array([0.0, 2.0, 4.0])
    return LapRecord("lmu", track, "GT3", 90.0, True, 0.0, ch)


def test_save_cloud_writes_capture_without_touching_map(tmp_path):
    app, store = _app(tmp_path)
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0), _edge("MapTrack", -1, 100.0)]
    app.state.mapping = True
    tj = tmp_path / "tracks" / "maptrack.json"
    before = tj.exists()
    r = _tc(app).post("/mapcapture/save-cloud", json={})
    assert r.status_code == 200 and r.json()["saved"] is True
    cap = tmp_path / "data" / "captures" / "maptrack.npz"
    assert cap.exists()
    with np.load(str(cap)) as z:
        assert any(k.endswith("pos_y") for k in z.files)   # elevation captured
    assert tj.exists() == before                            # geometry untouched
    assert app.state.mapping is False and app.state.map_buffer == []  # session ended


def test_save_cloud_needs_two_passes(tmp_path):
    app, store = _app(tmp_path)
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0)]
    assert _tc(app).post("/mapcapture/save-cloud", json={}).status_code == 422


def test_save_cloud_requires_elevation(tmp_path):
    # No pos_y -> rejected; capturing elevation is the whole point.
    app, store = _app(tmp_path)
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0, elevation=False),
                            _edge("MapTrack", -1, 100.0, elevation=False)]
    assert _tc(app).post("/mapcapture/save-cloud", json={}).status_code == 422
