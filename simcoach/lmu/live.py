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
    sector: int
    laps: int
    in_pits: bool
    is_player: bool
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
    elapsed: float
    end_time: float
    cur_s1: float | None
    cur_s2: float | None
    best_s1: float | None
    best_s2: float | None
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
            elapsed=0.0, end_time=0.0, cur_s1=None, cur_s2=None,
            best_s1=None, best_s2=None, last_lap=None, best_lap=None,
        ),
        cars=[],
    )


def _phase(m_session: int) -> str:
    # Reuse the coarse mapping smm.session_phase already defines.
    from simcoach.lmu.smm import session_phase
    return session_phase(m_session)


def _vehicle_state(v: VehicleScoringInfoV01, class_position: int) -> VehicleState:
    return VehicleState(
        id=int(v.mID),
        place=int(v.mPlace),
        class_position=class_position,
        driver=_decode(v.mDriverName),
        car=_decode(v.mVehicleName),
        cls=_decode(v.mVehicleClass),
        lap_dist=float(v.mLapDist),
        sector=int(v.mSector),
        laps=int(v.mTotalLaps),
        in_pits=bool(v.mInPits),
        is_player=bool(v.mIsPlayer),
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
        for rank, v in enumerate(sorted(members, key=lambda x: int(x.mPlace)), start=1):
            class_pos[int(v.mID)] = rank

    cars = [_vehicle_state(v, class_pos[int(v.mID)]) for v in raw]
    player = next((v for v in raw if v.mIsPlayer), None)

    if player is None:
        snap = empty_snapshot()
        snap.cars = cars
        return snap

    return FieldSnapshot(
        session=SessionState(
            live=True,
            phase=_phase(int(info.mSession)),
            current_lap=int(player.mTotalLaps) + 1,
            max_laps=int(info.mMaxLaps) or None,
            elapsed=float(info.mCurrentET),
            end_time=float(info.mEndET),
            cur_s1=_pos(player.mCurSector1),
            cur_s2=_pos(player.mCurSector2),
            best_s1=_pos(player.mBestSector1),
            best_s2=_pos(player.mBestSector2),
            last_lap=_pos(player.mLastLapTime),
            best_lap=_pos(player.mBestLapTime),
        ),
        cars=cars,
    )
