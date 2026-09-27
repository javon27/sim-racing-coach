"""#188: the setup each lap was driven on -- recorded off the capture thread at lap
commit, one row per change within a session, read back per lap / per session fastest."""
import dataclasses
import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.model.fake import FakeModel
from simcoach.server.app import _capture_loop, create_app
from simcoach.store import LapStore


def _setup(wing):
    return {"VM_WING_REAR": {"stringValue": str(wing), "isFreeSetting": True},
            "VM_LOCKED": {"stringValue": "N/A", "isFreeSetting": False}}


class _Rest:
    """LMU REST double: each lap's read returns the next of `setups` (the last one
    repeats); the liveness probe (probe_timeout set) doesn't consume one."""
    def __init__(self, *setups):
        self.setups = list(setups)

    def get_setup(self, probe_timeout=None):
        if probe_timeout is None and len(self.setups) > 1:
            return self.setups.pop(0)
        return self.setups[0]


class _Src:
    live_lap = None

    def __init__(self, laps):
        self._laps = laps

    def laps(self):
        yield from self._laps


def _laps(synthetic_lap, n, sid="s1"):
    return [dataclasses.replace(synthetic_lap, session_id=sid, timestamp=1000.0 + i * 100,
                                lap_time=100.0 + i) for i in range(n)]


def _run(tmp_path, synthetic_lap, rest, n=2):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), rest=rest)
    _capture_loop(_Src(_laps(synthetic_lap, n)), store, app.state)
    app.state.setup_pool.shutdown(wait=True)
    rows = store.conn.execute("SELECT setup_id FROM laps ORDER BY id").fetchall()
    return store, app, [r[0] for r in rows]


def test_two_laps_on_one_setup_store_one_row_both_linked(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap, _Rest(_setup(5)))
    assert ids[0] is not None and ids == [ids[0], ids[0]]
    assert store.conn.execute("SELECT COUNT(*) FROM setups").fetchone()[0] == 1
    # Stored through the same isFreeSetting filter as POST /setup/capture.
    assert store.lap_setup(1)["setup"] == {"VM_WING_REAR": _setup(5)["VM_WING_REAR"]}


def test_a_setup_change_between_laps_stores_two_rows(tmp_path, synthetic_lap):
    store, _, ids = _run(tmp_path, synthetic_lap, _Rest(_setup(5), _setup(7), _setup(7)), n=3)
    assert store.conn.execute("SELECT COUNT(*) FROM setups").fetchone()[0] == 2
    assert ids[0] != ids[1] and ids[1] == ids[2]
    assert store.lap_setup(2)["setup"]["VM_WING_REAR"]["stringValue"] == "7"


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
            return _setup(5)
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
    store, app, _ = _run(tmp_path, synthetic_lap, _Rest(_setup(5), _setup(7)))
    c = TestClient(app, base_url="http://127.0.0.1")
    first = c.get("/laps/1/setup").json()
    assert first["lap_id"] == 1 and first["lap_time"] == 100.0
    assert first["setup"]["VM_WING_REAR"]["stringValue"] == "5"
    assert c.get("/laps/2/setup").json()["setup"]["VM_WING_REAR"]["stringValue"] == "7"
    store.conn.execute("UPDATE laps SET setup_id = NULL WHERE id = 2")
    assert c.get("/laps/2/setup").json()["setup"] is None   # "no setup recorded"
    assert c.get("/laps/99/setup").status_code == 404


def _stored(store, synthetic_lap, sid, rows):
    """Save (lap_time, valid, wing|None) laps in `sid`; returns the resolved session id."""
    out = None
    for i, (t, valid, wing) in enumerate(rows):
        rec = dataclasses.replace(synthetic_lap, session_id=sid, lap_time=t, valid=valid,
                                  timestamp=synthetic_lap.timestamp + i * 100)
        lap_id = store.save(rec)
        out = rec.session_id
        if wing is not None:
            store.link_lap_setup(lap_id, _setup(wing))
    return out


def test_session_setup_is_the_fastest_valid_laps(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    sid = _stored(store, synthetic_lap, "s1", [
        (101.0, True, 1),
        (99.0, True, 2),       # fastest valid lap with a setup
        (90.0, False, 3),      # faster but invalid
        (98.0, True, None),    # faster but no setup recorded
        (100.0, True, 4),
    ])
    c = TestClient(create_app(store, Coach(FakeModel())), base_url="http://127.0.0.1")
    got = c.get(f"/sessions/{sid}/setup").json()
    assert got["lap_time"] == 99.0
    assert got["setup"]["VM_WING_REAR"]["stringValue"] == "2"
    assert c.get("/sessions/nope/setup").json() == {}


def test_get_last_setup_names_the_previous_sessions_fastest_lap(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    track, car = synthetic_lap.track, synthetic_lap.car
    # The legacy autosave is newer and different: it must NOT win over a per-lap setup.
    store.save_session_setup("old", track, car, _setup(9))
    _stored(store, synthetic_lap, "prev", [(101.0, True, 3), (99.5, True, 4), (100.0, True, 5)])
    cur = _stored(store, dataclasses.replace(synthetic_lap, timestamp=synthetic_lap.timestamp
                                             + 10_000), "cur", [(95.0, True, 8)])
    out = CoachContext(store, track=track, car=car, session_id=cur).get_last_setup()
    assert "fastest lap" in out.lower() and "previous session" in out.lower()
    assert "Wing Rear: 4" in out
    assert "Wing Rear: 8" not in out and "Wing Rear: 9" not in out


def test_get_last_setup_falls_back_to_the_legacy_autosave_named_as_such(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    store.save_session_setup("old", "spa", "gt3", _setup(6))
    out = CoachContext(store, track="spa", car="gt3").get_last_setup()
    assert "Wing Rear: 6" in out and "autosave" in out.lower()


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
    assert store.conn.execute("SELECT COUNT(*) FROM setups").fetchone()[0] == 0
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 26
