"""Reads LMU's live telemetry/scoring shared memory (Iron Wolf
rF2SharedMemoryMapPlugin buffers) and exposes it as a TelemetrySource.

Struct layout is front-loaded from the on-disk header only -- LMU was not
running while this was written, so none of it has been checked against a
live sim. See rf2structs.py for exactly which parts are verified vs. guessed.
Treat any lap/channel data this module produces as PENDING LIVE VALIDATION
until confirmed at [LUNCH].
"""
import ctypes as C
import mmap
import time
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

TELEMETRY = "$rFactor2SMMP_Telemetry$"
SCORING = "$rFactor2SMMP_Scoring$"

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


def read_struct(name: str, ctype):
    """Open `name`, copy sizeof(ctype) bytes, and return a filled instance."""
    mm = open_mmap(name, C.sizeof(ctype))
    try:
        buf = mm.read(C.sizeof(ctype))
    finally:
        mm.close()
    inst = ctype()
    C.memmove(C.addressof(inst), buf, C.sizeof(ctype))
    return inst


def read_consistent(name: str, ctype, retries: int = 6):
    """read_struct, retried while mVersionUpdateBegin != mVersionUpdateEnd
    (a torn read caught mid-write). Best-effort: returns the last read even
    if it never settles within `retries` attempts."""
    inst = read_struct(name, ctype)
    for _ in range(retries):
        if inst.mVersionUpdateBegin == inst.mVersionUpdateEnd:
            return inst
        inst = read_struct(name, ctype)
    return inst


def sample_from_telem(telem: TelemInfoV01, lap_dist: float) -> dict[str, float]:
    """Pure channel mapping: TelemInfoV01 + player lap distance -> the flat
    per-sample channel dict LapRecord accumulates. No MMF access -- this is
    what tests/test_lmu_smm.py exercises directly on synthetic structs.

    Sign/unit conventions per rf2structs.py docstring, PENDING LIVE VALIDATION:
      speed [m/s] = |mLocalVel| (z is longitudinal per the header's coordinate note)
      lat_g = mLocalAccel.x / 9.80665 (x is lateral in local vehicle coords)
      tire temps: mTemperature[1] (center) Kelvin -> Celsius
    """
    wheels = telem.mWheel
    return {
        "speed": telem.mLocalVel.magnitude(),
        "throttle": telem.mUnfilteredThrottle,
        "brake": telem.mUnfilteredBrake,
        "steering": telem.mUnfilteredSteering,
        "lat_g": telem.mLocalAccel.x / 9.80665,
        "lap_distance": lap_dist,
        "fuel": telem.mFuel,
        "tire_temp_fl": wheels[0].mTemperature[1] - 273.15,
        "tire_temp_fr": wheels[1].mTemperature[1] - 273.15,
        "tire_temp_rl": wheels[2].mTemperature[1] - 273.15,
        "tire_temp_rr": wheels[3].mTemperature[1] - 273.15,
    }


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
            read_struct(TELEMETRY, rF2Telemetry)
        except OSError as exc:
            raise RuntimeError(
                "Could not open LMU telemetry shared memory "
                f"('{TELEMETRY}'). Is Le Mans Ultimate running with the "
                "Iron Wolf rF2SharedMemoryMapPlugin enabled?"
            ) from exc

        period = 1.0 / self.poll_hz
        channels: dict[str, list[float]] = {}
        current_lap_number: int | None = None

        while True:
            telem = read_consistent(TELEMETRY, rF2Telemetry)
            scoring = read_consistent(SCORING, rF2Scoring)

            player = _find_player(scoring)
            vt = _find_telem_by_id(telem, player.mID) if player is not None else None
            if player is None or vt is None:
                time.sleep(period)
                continue

            if current_lap_number is None:
                current_lap_number = vt.mLapNumber

            if vt.mLapNumber != current_lap_number:
                if channels:
                    yield LapRecord(
                        sim="lmu",
                        track=vt.mTrackName.decode(errors="replace"),
                        car=vt.mVehicleName.decode(errors="replace"),
                        lap_time=player.mLastLapTime,
                        valid=True,
                        timestamp=time.time(),
                        channels={k: np.array(v) for k, v in channels.items()},
                    )
                channels = {}
                current_lap_number = vt.mLapNumber

            sample = sample_from_telem(vt, player.mLapDist)
            for k, v in sample.items():
                channels.setdefault(k, []).append(v)

            time.sleep(period)
