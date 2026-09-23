import dataclasses

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import _capture_loop, create_app
from simcoach.store import LapStore


class _OneShotSource:
    """Fake TelemetrySource: yields one lap then stops (deterministic, no thread)."""

    def __init__(self, lap):
        self.lap = lap

    def laps(self):
        yield self.lap


def test_capture_loop_saves_lap_and_updates_latest(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    lap = dataclasses.replace(synthetic_lap, session_id="test-session")

    _capture_loop(_OneShotSource(lap), store, app.state)

    assert app.state.latest is not None
    assert app.state.latest["record"].track == lap.track
    lap_id = app.state.latest["lap_id"]

    sessions = store.recent_race_sessions()
    assert any(s["session_id"] == "test-session" for s in sessions)
    laps = store.session_laps("test-session")
    assert [l["id"] for l in laps] == [lap_id]


class _ListSource:
    def __init__(self, laps):
        self._laps = laps

    def laps(self):
        yield from self._laps


def test_capture_loop_refreshes_the_race_snapshot_off_the_capture_thread(tmp_path, synthetic_lap):
    """#152: each stored race lap refreshes the race tools' cached snapshot on the race
    pool -- never inline on the capture thread, whose generator is suspended while the
    loop body runs."""
    import threading
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    seen = []
    real_commit = app.state.race.commit

    def spy(*args):
        seen.append(threading.current_thread().name)
        return real_commit(*args)

    app.state.race.commit = spy
    laps = [dataclasses.replace(synthetic_lap, phase="race", lap_no=n, timestamp=float(n),
                                session_id="spa--gt3--offline") for n in (1, 2)]
    _capture_loop(_ListSource(laps), store, app.state)
    app.state.race_pool.shutdown(wait=True)
    assert app.state.race.snapshot.as_of_lap == 2
    assert seen and all(name.startswith("race-state") for name in seen)


def test_capture_keeps_saving_laps_after_the_race_pool_is_shut_down(tmp_path, synthetic_lap):
    """#152: at app/interpreter exit the race pool refuses new work; that must never
    stop capture from storing a lap (the race snapshot is a nice-to-have)."""
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    app.state.race_pool.shutdown(wait=True)
    laps = [dataclasses.replace(synthetic_lap, phase="race", lap_no=n, timestamp=float(n),
                                session_id="spa--gt3--offline") for n in (1, 2)]
    _capture_loop(_ListSource(laps), store, app.state)
    assert [l["lap_no"] for l in store.session_laps("spa--gt3--offline")] == [1, 2]
