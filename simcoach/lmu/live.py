"""Pure live-field extraction for the Pit Wall view.

read_field() maps one LMU scoring frame (all vehicles) into a FieldSnapshot the
UI renders directly. Pure and MMF-free so it is unit-tested on synthetic
rF2Scoring structs (tests/test_lmu_live.py), same pattern as test_lmu_smm.py.

Sign/unit conventions follow rf2structs.py + smm.compute_sectors and are PENDING
LIVE VALIDATION: mCurSector2/mBestSector2 are CUMULATIVE (s1+s2); a sector/lap
time <= 0 is LMU's "not set" sentinel.
"""
from dataclasses import dataclass, field

from simcoach.lmu.rf2structs import MAX_MAPPED_VEHICLES, VehicleScoringInfoV01, rF2Scoring


@dataclass
class VehicleState:
    id: int
    place: int
    class_position: int
    driver: str
    car: str
    cls: str
    lap_dist: float
    speed: float  # m/s, |mLocalVel|; used for the Pit Wall relative-gap estimate
    sector: int
    laps: int
    in_pits: bool
    is_player: bool
    best_lap: float | None  # this car's best lap (for the class-fastest marker)
    last_lap: float | None  # this car's last completed lap (flashed on line-cross)
    best_s1: float | None   # this car's best cumulative sector splits (for session-class bests)
    best_s2: float | None
    gap_leader: float | None
    laps_behind_leader: int
    gap_next: float | None
    laps_behind_next: int


@dataclass
class SessionState:
    live: bool
    phase: str
    current_lap: int | None
    max_laps: int | None
    track_len: float  # meters (ScoringInfo.mLapDist); 0 when unknown
    elapsed: float
    end_time: float
    cur_s1: float | None
    cur_s2: float | None
    best_s1: float | None
    best_s2: float | None
    last_s1: float | None  # completed last lap's cumulative sector splits
    last_s2: float | None
    last_lap: float | None
    best_lap: float | None


@dataclass
class FieldSnapshot:
    session: SessionState
    cars: list[VehicleState] = field(default_factory=list)


def _pos(x: float) -> float | None:
    """LMU sentinel: a sector/lap time <= 0 means 'not set'."""
    return float(x) if x and x > 0 else None


def _decode(raw: bytes) -> str:
    return raw.decode(errors="replace").strip()


def empty_snapshot() -> FieldSnapshot:
    return FieldSnapshot(
        session=SessionState(
            live=False, phase="", current_lap=None, max_laps=None,
            track_len=0.0, elapsed=0.0, end_time=0.0, cur_s1=None, cur_s2=None,
            best_s1=None, best_s2=None, last_s1=None, last_s2=None,
            last_lap=None, best_lap=None,
        ),
        cars=[],
    )


def _phase(m_session: int) -> str:
    # Reuse the coarse mapping smm.session_phase already defines.
    from simcoach.lmu.smm import session_phase
    return session_phase(m_session)


def _speed(v: VehicleScoringInfoV01) -> float:
    lv = v.mLocalVel
    return (float(lv.x) ** 2 + float(lv.y) ** 2 + float(lv.z) ** 2) ** 0.5


def _vehicle_state(v: VehicleScoringInfoV01, class_position: int) -> VehicleState:
    return VehicleState(
        id=int(v.mID),
        place=int(v.mPlace),
        class_position=class_position,
        driver=_decode(v.mDriverName),
        car=_decode(v.mVehicleName),
        cls=_decode(v.mVehicleClass),
        lap_dist=float(v.mLapDist),
        speed=_speed(v),
        sector=int(v.mSector),
        laps=int(v.mTotalLaps),
        in_pits=bool(v.mInPits),
        is_player=bool(v.mIsPlayer),
        best_lap=_pos(v.mBestLapTime),
        last_lap=_pos(v.mLastLapTime),
        best_s1=_pos(v.mBestSector1),
        best_s2=_pos(v.mBestSector2),
        gap_leader=None if v.mLapsBehindLeader else float(v.mTimeBehindLeader),
        laps_behind_leader=int(v.mLapsBehindLeader),
        gap_next=None if v.mLapsBehindNext else float(v.mTimeBehindNext),
        laps_behind_next=int(v.mLapsBehindNext),
    )


def read_field(scoring: rF2Scoring) -> FieldSnapshot:
    info = scoring.mScoringInfo
    n = max(0, min(int(info.mNumVehicles), MAX_MAPPED_VEHICLES))
    raw = [scoring.mVehicles[i] for i in range(n)]

    # class_position: rank within each class by place (1-based).
    by_class: dict[str, list] = {}
    for v in raw:
        by_class.setdefault(_decode(v.mVehicleClass), []).append(v)
    class_pos: dict[int, int] = {}
    for members in by_class.values():
        # Sort by place (1-based), pushing mPlace==0 (unclassified) to the end.
        # Deterministic tiebreaker: mID (ascending).
        def sort_key(v):
            place = int(v.mPlace)
            return (place if place > 0 else 10_000, int(v.mID))
        for rank, v in enumerate(sorted(members, key=sort_key), start=1):
            class_pos[int(v.mID)] = rank

    cars = [_vehicle_state(v, class_pos[int(v.mID)]) for v in raw]
    player = next((v for v in raw if v.mIsPlayer), None)

    # Not actually running: no player (menus) or sitting in the garage stall. Report
    # not-live AND drop the field so neither the map nor the timing tower shows a
    # stale grid — mirrors read_live_status's garage check (which drives the header).
    if player is None or player.mInGarageStall:
        return empty_snapshot()

    # Practice/timed sessions report an INT_MAX-ish sentinel for "unlimited" —
    # only show a lap target for a real lap-count race.
    raw_max_laps = int(info.mMaxLaps)
    max_laps = raw_max_laps if 0 < raw_max_laps <= 1000 else None

    return FieldSnapshot(
        session=SessionState(
            live=True,
            phase=_phase(int(info.mSession)),
            current_lap=int(player.mTotalLaps) + 1,
            max_laps=max_laps,
            track_len=float(info.mLapDist),
            elapsed=float(info.mCurrentET),
            end_time=float(info.mEndET),
            cur_s1=_pos(player.mCurSector1),
            cur_s2=_pos(player.mCurSector2),
            best_s1=_pos(player.mBestSector1),
            best_s2=_pos(player.mBestSector2),
            last_s1=_pos(player.mLastSector1),
            last_s2=_pos(player.mLastSector2),
            last_lap=_pos(player.mLastLapTime),
            best_lap=_pos(player.mBestLapTime),
        ),
        cars=cars,
    )


import ctypes as _C
from dataclasses import asdict


def snapshot_to_dict(snap: FieldSnapshot) -> dict:
    return asdict(snap)


def read_field_snapshot() -> FieldSnapshot:
    """Read one live scoring frame and map it. Never raises: returns
    empty_snapshot() (live=False) when LMU isn't running / the read fails, so the
    /live socket can keep polling instead of dropping."""
    from simcoach.lmu.smm import SCORING_CANDIDATES, open_first_available, read_consistent
    try:
        name, handle = open_first_available(SCORING_CANDIDATES, _C.sizeof(rF2Scoring))
        handle.close()
        scoring = read_consistent(name, rF2Scoring)
    except Exception:
        return empty_snapshot()
    return read_field(scoring)
