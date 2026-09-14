from dataclasses import dataclass
import numpy as np


@dataclass
class LapRecord:
    sim: str
    track: str
    car: str
    lap_time: float
    valid: bool
    timestamp: float
    channels: dict[str, np.ndarray]
    phase: str | None = None
    session_id: str | None = None
    sectors: list[float] | None = None
    position: int | None = None
    sector_bounds: list[float] | None = None  # lap_distance of S1/S2 boundaries
    # The game's own lap number (scoring mTotalLaps at the commit) -- issue #77.
    lap_no: int | None = None
    # Was the car in the pits at any point during this lap (scoring mInPits)?
    # None on rows captured before this field existed, or where it wasn't tracked.
    pit: bool | None = None
