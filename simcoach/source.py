from collections.abc import Iterator
from typing import Protocol

from simcoach.model_types import LapRecord
from simcoach.store import LapStore


class TelemetrySource(Protocol):
    """Seam for lap capture. ReplaySource implements it now; a future LmuSource
    (live shared-memory reader) implements it later with no changes to callers."""

    def laps(self) -> Iterator[LapRecord]:
        ...


class ReplaySource:
    """Replays laps already saved in the store, for a given track/car."""

    def __init__(self, store: LapStore, track: str, car: str):
        self.store = store
        self.track = track
        self.car = car

    def laps(self) -> Iterator[LapRecord]:
        # Complete laps only: a replay stands in for live capture of finished laps.
        # An out/in/partial row (#77) has no lap time, so replaying it would feed a
        # consumer a lap capture itself would never have handed it. An invalidated
        # whole lap (#181) IS complete and replays -- untimed, as capture hands it.
        for lap_id in self.store.list_lap_ids(self.track, self.car, complete_only=True):
            yield self.store.load(lap_id)
