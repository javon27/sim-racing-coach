# tests/test_server_live.py
import json

import pytest
from fastapi.testclient import TestClient

import simcoach.server.app as app_mod
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.lmu.live import FieldSnapshot, SessionState, VehicleState
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


@pytest.fixture(autouse=True)
def _pin_live_track(monkeypatch):
    # /live resolves the corner map from _live_track(), which reads REAL shared
    # memory -- pin it so a running LMU on the dev machine can't leak in.
    monkeypatch.setattr("simcoach.lmu.smm.read_session_track_car",
                        lambda: ("Nowhere Raceway", "GT3"))


def _client(tmp_path, **kw):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel())
    tracks = tmp_path / "tracks"
    tracks.mkdir(exist_ok=True)
    kw.setdefault("tracks_dir", str(tracks))  # hermetic: never the shipped maps
    return TestClient(create_app(store, coach, **kw), base_url="http://127.0.0.1")


def _cmap():
    return CornerMap.from_data({"corners": [
        {"number": 1, "name": "La Source", "direction": "right",
         "start_dist": 100, "apex_dist": 150, "end_dist": 200},
        {"number": 2, "name": "Raidillon", "direction": "left",
         "start_dist": 900, "apex_dist": 950, "end_dist": 1000},
    ]})


def _snap(live):
    session = SessionState(live=live, phase="race", current_lap=9, max_laps=24,
                           track_len=4000.0, elapsed=100.0, end_time=0.0, cur_s1=None,
                           cur_s2=None, best_s1=None, best_s2=None, last_s1=None, last_s2=None,
                           last_lap=None, best_lap=None)
    cars = [VehicleState(id=2, place=9, class_position=3, driver="You", car="Corvette",
                         cls="GT3", lap_dist=500.0, speed=55.0, sector=1, laps=8, in_pits=False,
                         is_player=True, best_lap=91.9, last_lap=92.5, best_s1=29.8,
                         best_s2=61.5, gap_leader=31.5, laps_behind_leader=0,
                         gap_next=2.4, laps_behind_next=0)] if live else []
    return FieldSnapshot(session=session, cars=cars)


def test_live_pushes_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    # starlette's TestClient.websocket_connect hardcodes Host: testserver
    # regardless of base_url (unlike its HTTP methods) -- override explicitly
    # so this "good" request clears the Host guard (same fix as test_server_ws.py).
    with _client(tmp_path).websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data["session"]["live"] is True
    assert data["session"]["current_lap"] == 9
    assert data["cars"][0]["is_player"] is True
    assert data["cars"][0]["gap_next"] == 2.4


def test_live_reports_not_live_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(False))
    with _client(tmp_path).websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data["session"]["live"] is False
    assert data["cars"] == []


def test_live_rejects_spoofed_host(tmp_path):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with (
        pytest.raises(WebSocketDisconnect),
        _client(tmp_path).websocket_connect("/live", headers={"host": "evil.com"}) as ws,
    ):
        ws.receive_json()


def test_live_sends_the_players_next_corner_from_the_track_map(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    with _client(tmp_path, cmap=_cmap()).websocket_connect(
            "/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    # Player at lap_dist 500: past T1, before T2 -> T2, direction from the map.
    assert data["next_corner"] == {"label": "2", "name": "Raidillon", "direction": "left"}


def test_live_next_corner_is_none_without_a_corner_map(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    with _client(tmp_path).websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data["next_corner"] is None


def test_live_not_on_track_sends_no_next_corner(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(False))
    with _client(tmp_path, cmap=_cmap()).websocket_connect(
            "/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data["next_corner"] is None


def test_live_resolves_the_track_file_once_not_every_tick(tmp_path, monkeypatch):
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    (tracks / "nowhere_raceway.json").write_text(json.dumps({"corners": [
        {"number": 1, "name": "Hairpin", "direction": "right",
         "start_dist": 400, "apex_dist": 450, "end_dist": 520},
    ]}), encoding="utf-8")
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    client = _client(tmp_path, tracks_dir=str(tracks))
    calls = []
    real = app_mod.load_map_data
    monkeypatch.setattr(app_mod, "load_map_data",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    with client.websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        frames = [ws.receive_json() for _ in range(3)]
    # lap_dist 500 is inside Hairpin's window -> Hairpin on every tick...
    assert [f["next_corner"]["name"] for f in frames] == ["Hairpin"] * 3
    # ...but the track file was read once, not at 4 Hz.
    assert len(calls) == 1
