"""Reads LMU's live telemetry/scoring shared memory (Iron Wolf
rF2SharedMemoryMapPlugin buffers) and exposes it as a TelemetrySource.

Struct layout is front-loaded from the on-disk header only -- LMU was not
running while this was written, so none of it has been checked against a
live sim. See rf2structs.py for exactly which parts are verified vs. guessed.
Treat any lap/channel data this module produces as PENDING LIVE VALIDATION
until confirmed at [LUNCH].

ANTI-CHEAT: this module only ever OPENS the sanctioned plugin-published MMFs
read-only (OpenFileMappingW + mmap read access). It must never OpenProcess or
ReadProcessMemory the game -- same footprint as CrewChief/SimHub, which
coexist with LMU anti-cheat.
"""
import ctypes as C
import math
import mmap
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from simcoach.lmu.rf2structs import (
    MAX_MAPPED_VEHICLES,
    TelemInfoV01,
    rF2Scoring,
    rF2Telemetry,
    VehicleScoringInfoV01,
)
from simcoach.model_types import LapRecord

# Candidate MMF names, tried in order -- extensible so a future LMU/plugin
# update that renames the interface is a config change here, not a code
# change. Current names are Iron Wolf's rF2SMMP convention (confirmed via
# CrewChief's changelog: as of 4.19.4.0 CrewChief ships renamed *plugin DLLs*
# -- RF2.3.8.SharedMemoryMapPlugin.dll / LMU.3.8.SharedMemoryMapPlugin.dll --
# with a "use legacy shared memory filenames" toggle, implying the MMF names
# themselves may vary by plugin version; the changelog did not name a new MMF
# string, so no name is added here without a confirmed source).
# Native "LMU_Data" is a different layout entirely -- future candidate, needs
# its own struct, not mixed in here.
TELEMETRY_CANDIDATES = ["$rFactor2SMMP_Telemetry$"]
SCORING_CANDIDATES = ["$rFactor2SMMP_Scoring$"]

_kernel32 = C.WinDLL("kernel32", use_last_error=True)
_FILE_MAP_READ = 0x0004

# A backward jump in lap distance larger than this marks a lap boundary (crossing
# start/finish, or a teleport/reset). Smaller dips are off-track/jitter noise.
_LAP_WRAP_M = 500.0


def open_mmap(name: str, size: int) -> mmap.mmap:
    """Open an EXISTING named shared-memory mapping for read.

    Python's mmap.mmap(-1, size, tagname=name) alone would silently CREATE a
    fresh zero-filled mapping if `name` doesn't exist yet (Windows
    CreateFileMapping semantics) -- that would make "LMU isn't running"
    undetectable instead of raising. OpenFileMappingW only succeeds if the
    mapping already exists, so check with that first.
    """
    handle = _kernel32.OpenFileMappingW(_FILE_MAP_READ, False, name)
    if not handle:
        raise OSError(f"shared memory mapping '{name}' does not exist (is LMU running?)")
    _kernel32.CloseHandle(handle)
    return mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)


def open_first_available(candidates: list[str], size: int, opener=open_mmap):
    """Try each candidate MMF name in order; return (name, handle) for the
    first that opens. `opener` is injectable for tests. Raises a clear
    RuntimeError listing every name tried if none open -- the signal that
    the shared-memory interface was renamed/reshaped (e.g. an LMU update)."""
    errors = []
    for name in candidates:
        try:
            return name, opener(name, size)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(
        "no candidate shared-memory mapping opened (tried: " + "; ".join(errors) + ")"
    )


def _struct_from_handle(handle, ctype):
    """Copy sizeof(ctype) bytes from an already-open handle (mmap.mmap or
    equivalent) into a filled instance."""
    buf = handle.read(C.sizeof(ctype))
    inst = ctype()
    C.memmove(C.addressof(inst), buf, C.sizeof(ctype))
    return inst


def read_struct(name: str, ctype):
    """Open `name`, copy sizeof(ctype) bytes, and return a filled instance."""
    mm = open_mmap(name, C.sizeof(ctype))
    try:
        return _struct_from_handle(mm, ctype)
    finally:
        mm.close()


def read_consistent(name: str, ctype, retries: int = 6):
    """read_struct, retried while mVersionUpdateBegin != mVersionUpdateEnd
    (a torn read caught mid-write). Best-effort: returns the last read even
    if it never settles within `retries` attempts."""
    inst = read_struct(name, ctype)
    for _ in range(retries):
        if version_ok(inst):
            return inst
        inst = read_struct(name, ctype)
    return inst


def version_ok(tele) -> bool:
    """Named predicate for the torn-read check: True if the wrapper's
    versioning preamble is settled (not mid-write)."""
    return tele.mVersionUpdateBegin == tele.mVersionUpdateEnd


_THROTTLE_BRAKE_RANGE = (-0.01, 1.01)
_MAX_SANE_SPEED_MS = 200.0


def telemetry_is_sane(tele: rF2Telemetry) -> tuple[bool, list[str]]:
    """Pure sanity check on one telemetry frame -- no MMF access. Catches an
    MMF that opened fine but no longer matches our struct layout (garbage
    offsets read back as physically impossible values), which a successful
    OpenFileMappingW alone would not catch."""
    reasons = []
    n = tele.mNumVehicles
    if not (0 <= n <= MAX_MAPPED_VEHICLES):
        reasons.append(f"mNumVehicles out of range: {n}")
        return False, reasons

    if n > 0:
        v = tele.mVehicles[0]
        lo, hi = _THROTTLE_BRAKE_RANGE
        for field_name, value in (
            ("mUnfilteredThrottle", v.mUnfilteredThrottle),
            ("mUnfilteredBrake", v.mUnfilteredBrake),
            ("mUnfilteredSteering", v.mUnfilteredSteering),
        ):
            if not math.isfinite(value):
                reasons.append(f"{field_name} is not finite: {value}")
        if not (lo <= v.mUnfilteredThrottle <= hi):
            reasons.append(f"mUnfilteredThrottle out of range: {v.mUnfilteredThrottle}")
        if not (lo <= v.mUnfilteredBrake <= hi):
            reasons.append(f"mUnfilteredBrake out of range: {v.mUnfilteredBrake}")
        speed = v.mLocalVel.magnitude()
        if not math.isfinite(speed) or speed >= _MAX_SANE_SPEED_MS:
            reasons.append(f"implausible speed: {speed} m/s")

    return len(reasons) == 0, reasons


@dataclass
class CompatStatus:
    ok: bool
    telemetry_mmf: str | None = None
    scoring_mmf: str | None = None
    issues: list[str] = field(default_factory=list)


def compatibility_check(opener=open_mmap) -> CompatStatus:
    """Health probe, does NOT raise: open the first available telemetry (and
    scoring) MMF, read one frame, run version_ok + telemetry_is_sane. Reports
    drift (renamed/reshaped shared-memory interface) instead of silently
    misreading it."""
    issues: list[str] = []
    telemetry_mmf: str | None = None
    scoring_mmf: str | None = None

    try:
        telemetry_mmf, handle = open_first_available(
            TELEMETRY_CANDIDATES, C.sizeof(rF2Telemetry), opener=opener
        )
    except RuntimeError as exc:
        issues.append(f"telemetry: {exc}")
    else:
        try:
            telem = _struct_from_handle(handle, rF2Telemetry)
        finally:
            handle.close()
        if not version_ok(telem):
            issues.append("telemetry frame is torn (version begin != end)")
        sane, reasons = telemetry_is_sane(telem)
        if not sane:
            issues.append(
                "shared-memory interface changed or not sane -- check MMF "
                "names/struct (compare against CrewChief): " + "; ".join(reasons)
            )

    try:
        scoring_mmf, handle = open_first_available(
            SCORING_CANDIDATES, C.sizeof(rF2Scoring), opener=opener
        )
    except RuntimeError as exc:
        issues.append(f"scoring: {exc}")
    else:
        handle.close()

    return CompatStatus(
        ok=not issues, telemetry_mmf=telemetry_mmf, scoring_mmf=scoring_mmf, issues=issues
    )


def sample_from_telem(telem: TelemInfoV01, lap_dist: float) -> dict[str, float]:
    """Pure channel mapping: TelemInfoV01 + player lap distance -> the flat
    per-sample channel dict LapRecord accumulates. No MMF access -- this is
    what tests/test_lmu_smm.py exercises directly on synthetic structs.

    Sign/unit conventions per rf2structs.py docstring, PENDING LIVE VALIDATION:
      speed [km/h] = |mLocalVel| (m/s, z is longitudinal per the header's
        coordinate note) * 3.6 -- the rest of the pipeline (coach prompt,
        analysis.py, fixtures) is km/h-scale.
      lat_g = mLocalAccel.x / 9.80665 (x is lateral in local vehicle coords);
        long_g = mLocalAccel.z / 9.80665 (z is longitudinal; braking/accel).
      tire/brake temps: Kelvin -> Celsius (-273.15).
      per-wheel [fl,fr,rl,rr]: wheel_rot (mRotation, rad/s -- lockup ~ 0 while
        moving), brake_temp, brake_press (mBrakePressure), grip (mGripFract,
        0..1), tire_load (mTireLoad, N), tire_press (mPressure, kPa), tire_wear
        (mWear, 0..1). gear/rpm/abs_active/tc_active read as-is.
      virtual_energy / soc / battery_charge: read as-is, no unit conversion --
        all three are already fractions/percent-scale per the struct fields
        (mVirtualEnergy, mSoC, mBatteryChargeFraction), unlike fuel (liters)
        or temps (Kelvin) which need conversion.
    """
    wheels = telem.mWheel
    sample = {
        "speed": telem.mLocalVel.magnitude() * 3.6,
        "throttle": telem.mUnfilteredThrottle,
        "brake": telem.mUnfilteredBrake,
        "steering": telem.mUnfilteredSteering,
        "lat_g": telem.mLocalAccel.x / 9.80665,
        "long_g": telem.mLocalAccel.z / 9.80665,
        "lap_distance": lap_dist,
        # World position for the track-map outline. x/z = horizontal plane;
        # y = elevation (banking, hills) for elevation-aware maps / 3D replay.
        # Appended raw even if non-finite -- the map builder filters.
        "pos_x": telem.mPos.x,
        "pos_y": telem.mPos.y,
        "pos_z": telem.mPos.z,
        "gear": float(telem.mGear),
        "rpm": telem.mEngineRPM,
        "abs_active": 1.0 if telem.mABSActive else 0.0,
        "tc_active": 1.0 if telem.mTCActive else 0.0,
        "fuel": telem.mFuel,
        "virtual_energy": telem.mVirtualEnergy,
        "soc": telem.mSoC,
        "battery_charge": telem.mBatteryChargeFraction,
    }
    for name, i in (("fl", 0), ("fr", 1), ("rl", 2), ("rr", 3)):
        w = wheels[i]
        sample[f"tire_temp_{name}"] = w.mTemperature[1] - 273.15
        sample[f"tire_press_{name}"] = w.mPressure
        sample[f"tire_load_{name}"] = w.mTireLoad
        sample[f"tire_wear_{name}"] = w.mWear
        sample[f"grip_{name}"] = w.mGripFract
        sample[f"wheel_rot_{name}"] = w.mRotation
        sample[f"brake_temp_{name}"] = w.mBrakeTemp - 273.15
        sample[f"brake_press_{name}"] = w.mBrakePressure
    return sample


def session_phase(m_session: int) -> str:
    """Map the rF2/LMU scoring mSession index to a coarse phase. Standard rFactor2
    convention (0=test day, 1-4=practice, 5-8=qualifying, 9=warmup, 10-13=race),
    PENDING LIVE VALIDATION. Used so laps can be filtered/compared by session type
    instead of pooling qualy hot laps with race stints."""
    if m_session <= 0:
        return "testday"
    if m_session <= 4:
        return "practice"
    if m_session <= 8:
        return "qualifying"
    if m_session == 9:
        return "warmup"
    return "race"


def _is_online(scoring: rF2Scoring) -> bool:
    """True if any mapped opponent is a remote/human driver (mControl == 2).
    Offline AI opponents are mControl == 1; a solo/empty grid is the player
    only (mControl == 0). This is the verified online signal.

    # ponytail: hotlapping ALONE on an online server (0 remote opponents) is
    # indistinguishable from offline in the mapped fields, so it classifies as
    # offline. The real online flag lives in the unmapped struct tail; not worth
    # chasing until it matters.
    """
    n = min(scoring.mScoringInfo.mNumVehicles, MAX_MAPPED_VEHICLES)
    for i in range(max(n, 0)):
        if scoring.mVehicles[i].mControl == 2:
            return True
    return False


def _slug(s: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", s.lower())) or "unknown"


def session_id_for(track: str, car: str, online: bool) -> str:
    """Deterministic, URL-safe session id: same (track, car, mode) always yields
    the same id, so a session is one (track, car, online|offline) combo that
    survives sidecar restarts. Used in /sessions/{sid} paths -- no spaces/#/:."""
    return f"{_slug(track)}--{_slug(car)}--{'online' if online else 'offline'}"


def compute_sectors(
    last_s1: float, last_s2: float, last_lap_time: float
) -> list[float] | None:
    """Split a completed lap into [s1, s2, s3], or None if the numbers don't
    reconcile with the lap time.

    # ponytail: mLastSector1 is s1 and mLastSector2 is CUMULATIVE (s1+s2) --
    # the documented rF2/rF2SMMP convention. The real safety here is the all-
    # positive check: if the convention is wrong (e.g. mLastSector2 is isolated
    # s2, so it's < s1), a derived sector goes <= 0 and we store None rather than
    # garbage. The sum-check is belt-and-suspenders (algebraically it always
    # equals last_lap_time under this derivation) -- keep it so a future change
    # to how s3 is sourced still has to reconcile. Live-verify on the rig.
    """
    s1 = last_s1
    s2 = last_s2 - last_s1
    s3 = last_lap_time - last_s2
    if s1 > 0 and s2 > 0 and s3 > 0 and abs((s1 + s2 + s3) - last_lap_time) <= 0.5:
        return [s1, s2, s3]
    return None


def sector_boundary_dists(dists: list[float], sectors: list[int]) -> list[float]:
    """lap_distances at each FORWARD mCurrentSector change (parallel lists). The
    S/F wrap is a DECREASE and is not counted, so a clean lap yields two entries
    (end S1, end S2). Pure; the loop feeds it a lap's accumulated samples."""
    out: list[float] = []
    prev: int | None = None
    for d, s in zip(dists, sectors):
        if prev is not None and s > prev:
            out.append(d)
        prev = s
    return out


def sector_bounds_from(
    sector_dists: list[float], lap_length: float | None
) -> list[float] | None:
    """Two in-lap sector-boundary distances (end of S1, end of S2) from the
    lap_distances captured at forward mCurrentSector transitions, or None if the
    observation isn't clean -- mirrors compute_sectors: ship None over a wrong
    convention rather than a misplaced split.

    # ponytail: the mCurrentSector->sector-number mapping is UNCONFIRMED (rF2 has
    # a known off-by-one). We key only off FORWARD index changes, so a 0/1/2 or a
    # 1/2/3 ordering both yield the same two boundaries; the guard rejects any lap
    # that didn't show exactly two clean, increasing in-bounds transitions. RIG:
    # eyeball that the two distances land at the real S1/S2 split points.
    """
    if lap_length is None or lap_length <= 0 or len(sector_dists) != 2:
        return None
    d1, d2 = sector_dists
    if not (math.isfinite(d1) and math.isfinite(d2)):
        return None
    if not (0 < d1 < d2 < lap_length):
        return None
    return [float(d1), float(d2)]


def player_position(place) -> int | None:
    """The player's live race position (mPlace) as a 1-based int, or None for the
    0/unset sentinel LMU reports when not racing. Guards nonsensical values so a
    stray 0 never lands in the store as a real position."""
    return int(place) if place and place > 0 else None


def build_lap_record(
    sim: str,
    track: str,
    car: str,
    lap_time: float,
    invalidated: bool,
    timestamp: float,
    channels: dict[str, list[float]],
    phase: str | None = None,
    session_id: str | None = None,
    sectors: list[float] | None = None,
    position: int | None = None,
    sector_bounds: list[float] | None = None,
) -> LapRecord | None:
    """Pure decision: turn a completed lap's raw fields into a LapRecord, or
    None if the lap shouldn't be stored.

    lap_time <= 0 is LMU's sentinel for "no real lap time" (e.g. -1) -- never
    yield those, or store.best_lap()'s ORDER BY lap_time ASC would treat the
    sentinel as the permanent fastest lap and poison every delta. validity is
    the inverse of mLapInvalidated (cut track / off-track).
    """
    if lap_time <= 0:
        return None
    return LapRecord(
        sim=sim,
        track=track,
        car=car,
        lap_time=lap_time,
        valid=not invalidated,
        timestamp=timestamp,
        channels={k: np.array(v) for k, v in channels.items()},
        phase=phase,
        session_id=session_id,
        sectors=sectors,
        position=position,
        sector_bounds=sector_bounds,
    )


def _find_player(scoring: rF2Scoring) -> VehicleScoringInfoV01 | None:
    n = min(scoring.mScoringInfo.mNumVehicles, MAX_MAPPED_VEHICLES)
    for i in range(max(n, 0)):
        v = scoring.mVehicles[i]
        if v.mIsPlayer:
            return v
    return None


def _find_telem_by_id(telem: rF2Telemetry, vehicle_id: int) -> TelemInfoV01 | None:
    n = min(telem.mNumVehicles, MAX_MAPPED_VEHICLES)
    for i in range(max(n, 0)):
        t = telem.mVehicles[i]
        if t.mID == vehicle_id:
            return t
    return None


def read_session_track_car() -> tuple[str, str] | None:
    """Peek the current player's (track, car) from live shared memory, or None if
    LMU isn't running / no player on track. Used at startup to pick the corner map."""
    try:
        telem = read_consistent(TELEMETRY_CANDIDATES[0], rF2Telemetry)
        scoring = read_consistent(SCORING_CANDIDATES[0], rF2Scoring)
    except Exception:
        return None
    player = _find_player(scoring)
    vt = _find_telem_by_id(telem, player.mID) if player is not None else None
    if vt is None:
        return None
    return (
        vt.mTrackName.decode(errors="replace").strip(),
        vt.mVehicleName.decode(errors="replace").strip(),
    )


def read_live_status() -> dict | None:
    """Coarse LIVE driving state straight from scoring, independent of whether a
    lap has been captured yet — so the UI can show on-track/garage/pits the moment
    the car is in that state, not only after the first completed lap.

    Returns {"track", "car", "state"} with state ∈ {"track","pits","garage"}, or
    None if LMU isn't running / no player is in the session (menus).

    # PENDING LIVE VALIDATION: mInGarageStall / mInPits offsets are from the
    # header, unconfirmed against a running sim (see module docstring).
    """
    try:
        telem = read_consistent(TELEMETRY_CANDIDATES[0], rF2Telemetry)
        scoring = read_consistent(SCORING_CANDIDATES[0], rF2Scoring)
    except Exception:
        return None
    player = _find_player(scoring)
    if player is None:
        return None
    vt = _find_telem_by_id(telem, player.mID)
    track = vt.mTrackName.decode(errors="replace").strip() if vt is not None else ""
    car = (vt.mVehicleName if vt is not None else player.mVehicleName).decode(errors="replace").strip()
    if player.mInGarageStall:
        state = "garage"
    elif player.mInPits:
        state = "pits"
    else:
        state = "track"
    return {"track": track, "car": car, "state": state}


def read_live_session_id() -> str | None:
    """The deterministic base session id (`track--car--online|offline`) for the
    CURRENT live session, or None if LMU isn't running / no player. Identical to the
    id smm assigns captured laps (`session_id_for`), so a coach chat started before
    any lap (in the garage) can be stamped with the session it belongs to."""
    try:
        telem = read_consistent(TELEMETRY_CANDIDATES[0], rF2Telemetry)
        scoring = read_consistent(SCORING_CANDIDATES[0], rF2Scoring)
    except Exception:
        return None
    player = _find_player(scoring)
    vt = _find_telem_by_id(telem, player.mID) if player is not None else None
    if vt is None:
        return None
    track = vt.mTrackName.decode(errors="replace").strip()
    car = vt.mVehicleName.decode(errors="replace").strip()
    if not (track and car):
        return None
    return session_id_for(track, car, _is_online(scoring))


class LmuSource:
    """TelemetrySource backed by LMU's live shared memory.

    NOT validated against a running sim -- see module docstring. Calling
    laps() when LMU isn't running (or the mapping doesn't exist) raises
    RuntimeError immediately rather than yielding garbage.
    """

    def __init__(self, poll_hz: float = 50.0):
        self.poll_hz = poll_hz

    def laps(self) -> Iterator[LapRecord]:
        try:
            telemetry_name, handle = open_first_available(
                TELEMETRY_CANDIDATES, C.sizeof(rF2Telemetry)
            )
            handle.close()
            scoring_name, handle = open_first_available(
                SCORING_CANDIDATES, C.sizeof(rF2Scoring)
            )
            handle.close()
        except RuntimeError as exc:
            raise RuntimeError(
                "LMU shared memory not found (is LMU running? did an update "
                f"rename the interface?): {exc}"
            ) from exc

        period = 1.0 / self.poll_hz
        # Buffer the in-progress lap; only COMMIT it when the car genuinely crosses
        # the finish line (a downward lap-distance wrap) AND the sim posted a fresh
        # lap time for it. A teleport, pit exit, pause, or out-lap wraps the distance
        # too but posts no new time — those buffers are discarded, so half-driven /
        # frozen / abandoned laps never reach the store or the coach.
        channels: dict[str, list[float]] = {}
        prev_dist: float | None = None
        committed_time: float | None = None
        # Per-lap mCurrentSector sample, parallel to channels["lap_distance"];
        # forward changes become the sector boundaries at commit (see
        # sector_boundary_dists / sector_bounds_from).
        sector_seq: list[int] = []
        # A "session" is one (track, car, online|offline) combo with a stable,
        # deterministic id -- all sim phases merge, and the id survives sidecar
        # restarts. mSession still gives the per-lap phase label.

        while True:
            telem = read_consistent(telemetry_name, rF2Telemetry)
            scoring = read_consistent(scoring_name, rF2Scoring)

            player = _find_player(scoring)
            vt = _find_telem_by_id(telem, player.mID) if player is not None else None
            if player is None or vt is None:
                time.sleep(period)
                continue

            dist = player.mLapDist
            # Finish line / lap boundary: lap distance jumped backward a long way.
            if prev_dist is not None and dist < prev_dist - _LAP_WRAP_M:
                lap_time = player.mLastLapTime
                # Commit ONLY a completed, freshly-timed lap. A teleport/exit/out-lap
                # also wraps here but carries a stale or sentinel (<=0) lap time.
                if channels and lap_time > 0 and lap_time != committed_time:
                    track = vt.mTrackName.decode(errors="replace")
                    car = vt.mVehicleName.decode(errors="replace")
                    phase = session_phase(scoring.mScoringInfo.mSession)
                    session_id = session_id_for(track, car, _is_online(scoring))
                    sectors = compute_sectors(
                        player.mLastSector1, player.mLastSector2, lap_time
                    )
                    position = player_position(player.mPlace)
                    lap_dists = channels.get("lap_distance") or []
                    lap_len = max(lap_dists or [0.0])
                    sector_bounds = sector_bounds_from(
                        sector_boundary_dists(lap_dists, sector_seq), lap_len
                    )
                    record = build_lap_record(
                        sim="lmu", track=track, car=car, lap_time=lap_time,
                        invalidated=vt.mLapInvalidated, timestamp=time.time(),
                        channels=channels, phase=phase, session_id=session_id,
                        sectors=sectors, position=position, sector_bounds=sector_bounds,
                    )
                    if record is not None:
                        committed_time = lap_time
                        yield record
                channels = {}  # start the next lap fresh (committed or discarded)
                sector_seq = []

            sample = sample_from_telem(vt, dist)
            # Track-width usage lives on SCORING, not telemetry: path_lateral is the
            # car's own lateral offset from the racing line's centre path, taken at the
            # car's CENTRE (not its outer wheels); track_edge is that SAME centre path's
            # offset to the track edge on the car's side (~= half the track width there)
            # -- NOT the car's distance to the edge.
            # Distance-to-edge = |track_edge| - |path_lateral|, computed downstream
            # in analysis.py where both channels are available together.
            # PENDING LIVE VALIDATION: mPathLateral/mTrackEdge units + sign.
            sample["path_lateral"] = player.mPathLateral
            sample["track_edge"] = player.mTrackEdge
            for k, v in sample.items():
                channels.setdefault(k, []).append(v)
            prev_dist = dist
            # mCurrentSector lives on the TELEM struct (vt), not scoring.
            sector_seq.append(int(vt.mCurrentSector))  # parallel to lap_distance

            time.sleep(period)
