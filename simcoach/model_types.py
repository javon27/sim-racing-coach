from dataclasses import dataclass

import numpy as np


@dataclass
class RaceSample:
    """The race situation at a lap's commit (#152), read from the same scoring frame
    that committed the lap. Every field is None when the game did not report it."""
    class_position: int | None = None
    class_ahead: str | None = None        # "driver (entry)" one place up in class
    class_gap_ahead: float | None = None  # s; None = a lap or more, or no car ahead
    class_behind: str | None = None
    class_gap_behind: float | None = None
    time_left: float | None = None        # s to the session clock's end; None = no clock
    max_laps: int | None = None           # a lap-count race's length; None = not one


@dataclass
class LapRecord:
    sim: str
    track: str
    car: str
    lap_time: float | None   # None ONLY on a partial lap -- the game posts no time
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
    # The kind of UNFINISHED lap this is, or None for a normal timed lap (#77):
    # 'out'     -- the lap began in the pits (a stint's first lap out of the box)
    # 'in'      -- the lap ended in the pits
    # 'partial' -- neither: a spin, a teleport, a return to the garage
    # A partial always carries lap_time None and valid False, so every consumer
    # that already filters on `valid = 1` ignores it with no change.
    partial: str | None = None
    # The race situation at this lap's commit (#152); stored on the lap row (v21).
    race: RaceSample | None = None
