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
      lat_g = mLocalAccel.x / 9.80665 (x is lateral in local vehicle coords)
      tire temps: mTemperature[1] (center) Kelvin -> Celsius
      virtual_energy / soc / battery_charge: read as-is, no unit conversion --
        all three are already fractions/percent-scale per the struct fields
        (mVirtualEnergy, mSoC, mBatteryChargeFraction), unlike fuel (liters)
        or tire temp (Kelvin) which need conversion.
    """
    wheels = telem.mWheel
    return {
        "speed": telem.mLocalVel.magnitude() * 3.6,
        "throttle": telem.mUnfilteredThrottle,
        "brake": telem.mUnfilteredBrake,
        "steering": telem.mUnfilteredSteering,
        "lat_g": telem.mLocalAccel.x / 9.80665,
        "lap_distance": lap_dist,
        "fuel": telem.mFuel,
        "virtual_energy": telem.mVirtualEnergy,
        "soc": telem.mSoC,
        "battery_charge": telem.mBatteryChargeFraction,
        "tire_temp_fl": wheels[0].mTemperature[1] - 273.15,
        "tire_temp_fr": wheels[1].mTemperature[1] - 273.15,
        "tire_temp_rl": wheels[2].mTemperature[1] - 273.15,
        "tire_temp_rr": wheels[3].mTemperature[1] - 273.15,
    }


def build_lap_record(
    sim: str,
    track: str,
    car: str,
    lap_time: float,
    invalidated: bool,
    timestamp: float,
    channels: dict[str, list[float]],
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
        channels: dict[str, list[float]] = {}
        current_lap_number: int | None = None

        while True:
            telem = read_consistent(telemetry_name, rF2Telemetry)
            scoring = read_consistent(scoring_name, rF2Scoring)

            player = _find_player(scoring)
            vt = _find_telem_by_id(telem, player.mID) if player is not None else None
            if player is None or vt is None:
                time.sleep(period)
                continue

            if current_lap_number is None:
                current_lap_number = vt.mLapNumber

            if vt.mLapNumber != current_lap_number:
                if channels:
                    record = build_lap_record(
                        sim="lmu",
                        track=vt.mTrackName.decode(errors="replace"),
                        car=vt.mVehicleName.decode(errors="replace"),
                        lap_time=player.mLastLapTime,
                        invalidated=vt.mLapInvalidated,
                        timestamp=time.time(),
                        channels=channels,
                    )
                    if record is not None:
                        yield record
                channels = {}
                current_lap_number = vt.mLapNumber

            sample = sample_from_telem(vt, player.mLapDist)
            for k, v in sample.items():
                channels.setdefault(k, []).append(v)

            time.sleep(period)
