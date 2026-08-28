from typing import Iterator, Protocol

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
        rows = self.store.conn.execute(
            "SELECT id FROM laps WHERE track = ? AND car = ?",
            (self.track, self.car),
        ).fetchall()
        for (lap_id,) in rows:
            yield self.store.load(lap_id)
