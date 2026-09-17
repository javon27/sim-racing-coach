# tests/test_server_live.py
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.lmu.live import FieldSnapshot, SessionState, VehicleState
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel())
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1")


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
    with pytest.raises(WebSocketDisconnect):
        with _client(tmp_path).websocket_connect("/live", headers={"host": "evil.com"}) as ws:
            ws.receive_json()
