"""#188: the setup each lap was driven on -- recorded off the capture thread at lap
commit, one row per change within a session, read back per lap / per session fastest.

Fixtures are real stored rows (session_setups on the rig, 2026-09-26): a Monza LMGT3
setup, and the blank garage payload LMU served at Sarthe (170 isFreeSetting keys, every
stringValue '')."""
import copy
import dataclasses
import json
import sqlite3
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.model.fake import FakeModel
from simcoach.server.app import _capture_loop, create_app
from simcoach.store import SCHEMA_VERSION, LapStore

_FIX = Path(__file__).parent / "fixtures"
REAL = json.loads((_FIX / "lmu_setup_monza.json").read_text())
BLANK = json.loads((_FIX / "lmu_setup_blank_sarthe.json").read_text())


def _setup(brake=None, **meta):
    """REAL with the brake balance step set to `brake` (a real setup change) and/or
    per-entry churn that is NOT a change: `wear` rewrites the compound's stringValue,
    `saves` bumps numChangesValue/lastSavedStringValue/diffComparisonValue."""
    s = copy.deepcopy(REAL)
    if brake is not None:
        s["VM_BRAKE_BALANCE"].update(value=brake, stringValue=f"{brake}.0:x")
    if "wear" in meta:
        s["WM_COMPOUND-W_FL"]["stringValue"] = f"{meta['wear']}% Medium"
    if "saves" in meta:
        for v in s.values():
            v.update(numChangesValue=meta["saves"], lastSavedStringValue="saved",
                     diffComparisonValue=meta["saves"])
    return s


class _Rest:
    """LMU REST double: each lap's read returns the next of `setups` (the last one
    repeats); the liveness probe (probe_timeout set) doesn't consume one. The payload
    gains a non-free entry, which the isFreeSetting filter must drop."""
    def __init__(self, *setups):
        self.setups = list(setups)

    def get_setup(self, probe_timeout=None):
        if probe_timeout is None and len(self.setups) > 1:
            s = self.setups.pop(0)
        else:
            s = self.setups[0]
        return {**s, "VM_LOCKED": {"stringValue": "N/A", "isFreeSetting": False, "value": 0}}


class _Src:
    live_lap = None

    def __init__(self, laps):
        self._laps = laps

    def laps(self):
        yield from self._laps


def _laps(synthetic_lap, n, sid="s1"):
    return [dataclasses.replace(synthetic_lap, session_id=sid, timestamp=1000.0 + i * 100,
                                lap_time=100.0 + i, lap_no=i + 1) for i in range(n)]


def _run(tmp_path, synthetic_lap, rest, n=2, laps=None):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), rest=rest)
    _capture_loop(_Src(laps or _laps(synthetic_lap, n)), store, app.state)
    app.state.setup_pool.shutdown(wait=True)
    rows = store.conn.execute("SELECT setup_id FROM laps ORDER BY id").fetchall()
    return store, app, [r[0] for r in rows]


def _nrows(store):
    return store.conn.execute("SELECT COUNT(*) FROM setups").fetchone()[0]


def test_two_laps_on_one_setup_store_one_row_both_linked(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap, _Rest(_setup()))
    assert ids[0] is not None and ids == [ids[0], ids[0]]
    assert _nrows(store) == 1
    # Stored whole, through the same isFreeSetting filter as POST /setup/capture.
    assert store.lap_setup(1)["setup"] == REAL


def test_metadata_and_wear_churn_is_not_a_setup_change(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap,
                         _Rest(_setup(), _setup(wear=96), _setup(saves=3)), n=3)
    assert _nrows(store) == 1 and len(set(ids)) == 1


def test_a_value_change_between_laps_stores_two_rows(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap, _Rest(_setup(), _setup(brake=30)), n=3)
    assert _nrows(store) == 2
    assert ids[0] != ids[1] and ids[1] == ids[2]
    assert store.lap_setup(2)["setup"]["VM_BRAKE_BALANCE"]["value"] == 30


def test_a_to_b_to_a_stores_each_change(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap,
                         _Rest(_setup(), _setup(brake=30), _setup()), n=3)
    assert _nrows(store) == 3 and len(set(ids)) == 3   # a change vs the LAST setup
    assert store.lap_setup(3)["setup"] == store.lap_setup(1)["setup"] == REAL


def test_dedupe_is_per_session(tmp_path, synthetic_lap):
    laps = _laps(synthetic_lap, 2) + [dataclasses.replace(lap, session_id="s2",
                                                          timestamp=lap.timestamp + 10_000)
                                      for lap in _laps(synthetic_lap, 2)]
    store, _, ids = _run(tmp_path, synthetic_lap, _Rest(_setup()), laps=laps)
    # One row per session: each session's first lap starts its own setup history.
    assert _nrows(store) == 2 and ids[0] == ids[1] and ids[2] == ids[3] != ids[0]


def test_blank_garage_payload_is_never_stored(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap, _Rest(BLANK))
    assert ids == [None, None] and _nrows(store) == 0


def test_setup_capture_skips_a_blank_garage_payload(tmp_path, synthetic_lap, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": synthetic_lap.track, "car": synthetic_lap.car,
                                 "state": "track"})
    monkeypatch.setattr(smm, "read_session_track_car", lambda: None)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    synthetic_lap.session_id = "s1"
    rest = _Rest(BLANK)
    app = create_app(store, Coach(FakeModel()), rest=rest)
    app.state.latest = {"record": synthetic_lap, "lap_id": 1}
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.post("/setup/capture", json={}).json() == {"saved": False}
    assert store.last_setup(synthetic_lap.track, synthetic_lap.car) is None
    rest.setups = [REAL]   # control: a real setup through the same path is saved
    assert c.post("/setup/capture", json={}).json() == {"saved": True}


def test_rest_down_at_commit_saves_the_lap_without_a_setup(tmp_path, synthetic_lap):
    class _Down:
        def get_setup(self):
            raise ConnectionError("LMU closed")
    store, _, ids = _run(tmp_path, synthetic_lap, _Down())
    assert ids == [None, None]
    assert store.lap_setup(1)["setup"] is None


def test_a_hung_rest_never_blocks_capture(tmp_path, synthetic_lap):
    release, calls = threading.Event(), []

    class _Hung:
        def get_setup(self, probe_timeout=None):
            calls.append(probe_timeout)
            if len(calls) > 1:   # the probe answers; the lap's own read hangs
                release.wait(10)
            return REAL
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), rest=_Hung())
    t0 = time.monotonic()
    _capture_loop(_Src(_laps(synthetic_lap, 2)), store, app.state)   # returns while hung
    assert time.monotonic() - t0 < 5 and not release.is_set()
    assert store.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 2
    release.set()
    app.state.setup_pool.shutdown(wait=True)
    assert store.conn.execute("SELECT COUNT(*) FROM laps WHERE setup_id IS NOT NULL"
                              ).fetchone()[0] == 2


def test_lap_setup_endpoint_returns_that_laps_setup(tmp_path, synthetic_lap):
    store, app, _ = _run(tmp_path, synthetic_lap, _Rest(_setup(), _setup(brake=30)))
    c = TestClient(app, base_url="http://127.0.0.1")
    first = c.get("/laps/1/setup").json()
    assert first["lap_id"] == 1 and first["lap_time"] == 100.0 and first["lap_no"] == 1
    assert first["setup"] == REAL
    assert c.get("/laps/2/setup").json()["setup"]["VM_BRAKE_BALANCE"]["value"] == 30
    store.conn.execute("UPDATE laps SET setup_id = NULL WHERE id = 2")
    assert c.get("/laps/2/setup").json()["setup"] is None   # "no setup recorded"
    assert c.get("/laps/99/setup").status_code == 404


def _stored(store, synthetic_lap, sid, rows):
    """Save (lap_time, valid, brake|None) laps in `sid`; brake None = no setup recorded.
    Returns the resolved session id."""
    out = None
    for i, (t, valid, brake) in enumerate(rows):
        rec = dataclasses.replace(synthetic_lap, session_id=sid, lap_time=t, valid=valid,
                                  timestamp=synthetic_lap.timestamp + i * 100, lap_no=i + 1)
        lap_id = store.save(rec)
        out = rec.session_id
        if brake is not None:
            store.link_lap_setup(lap_id, _setup(brake=brake))
    return out


def test_session_setup_is_the_fastest_valid_laps(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    sid = _stored(store, synthetic_lap, "s1", [
        (101.0, True, 11),
        (99.0, True, 12),      # fastest valid lap, with a setup
        (90.0, False, 13),     # faster but invalid
        (100.0, True, 14),
    ])
    c = TestClient(create_app(store, Coach(FakeModel())), base_url="http://127.0.0.1")
    got = c.get(f"/sessions/{sid}/setup").json()
    assert (got["lap_time"], got["lap_no"], got["fastest"]) == (99.0, 2, True)
    assert got["setup"]["VM_BRAKE_BALANCE"]["value"] == 12
    assert c.get("/sessions/nope/setup").json() == {}


def test_session_setup_flags_a_faster_lap_with_no_setup(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    sid = _stored(store, synthetic_lap, "s1", [(99.0, True, 12), (98.0, True, None)])
    got = store.session_fastest_setup(sid)
    assert got["lap_time"] == 99.0 and got["fastest"] is False


def test_get_last_setup_names_the_previous_sessions_fastest_lap(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    track, car = synthetic_lap.track, synthetic_lap.car
    # An OLDER legacy autosave must not win over a per-lap setup.
    store.save_session_setup("old", track, car, _setup(brake=9))
    store.conn.execute("UPDATE session_setups SET updated_ts = 1")
    _stored(store, synthetic_lap, "prev", [(101.0, True, 3), (99.5, True, 4), (100.0, True, 5)])
    cur = _stored(store, dataclasses.replace(synthetic_lap, timestamp=synthetic_lap.timestamp
                                             + 10_000), "cur", [(95.0, True, 8)])
    out = CoachContext(store, track=track, car=car, session_id=cur).get_last_setup()
    assert "on the fastest lap (lap 2, 1:39.500) of your previous session" in out
    assert "Brake Balance: 4.0:x" in out
    assert "8.0:x" not in out and "9.0:x" not in out


def test_get_last_setup_says_when_the_true_fastest_lap_has_no_setup(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _stored(store, synthetic_lap, "prev", [(99.5, True, 4), (99.0, True, None)])
    out = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="cur").get_last_setup()
    assert "fastest lap with a recorded setup (lap 1, 1:39.500)" in out


def test_get_last_setup_prefers_a_more_recent_legacy_session(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _stored(store, synthetic_lap, "prev", [(99.5, True, 4)])
    # A later session with only the legacy autosave IS the previous session.
    store.save_session_setup("later", synthetic_lap.track, synthetic_lap.car, _setup(brake=7))
    out = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="cur").get_last_setup()
    assert "autosave" in out.lower() and "7.0:x" in out and "4.0:x" not in out


def test_get_last_setup_falls_back_to_the_legacy_autosave_named_as_such(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    store.save_session_setup("old", "spa", "gt3", _setup(brake=6))
    out = CoachContext(store, track="spa", car="gt3").get_last_setup()
    assert "6.0:x" in out and "autosave" in out.lower()


def test_v25_db_gains_laps_setup_id(tmp_path):
    db = str(tmp_path / "c.sqlite")
    LapStore(db, str(tmp_path / "data")).conn.close()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE laps DROP COLUMN setup_id")
    conn.execute("DROP TABLE setups")
    conn.execute("PRAGMA user_version = 25")
    conn.commit()
    conn.close()
    store = LapStore(db, str(tmp_path / "data"))
    assert "setup_id" in [r[1] for r in store.conn.execute("PRAGMA table_info(laps)")]
    assert _nrows(store) == 0
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
