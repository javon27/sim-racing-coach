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
