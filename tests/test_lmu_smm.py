"""Self-consistency tests for simcoach/lmu/rf2structs.py + smm.py.

These do NOT touch real shared memory (LMU is not running). They prove our
own ctypes structs serialize/deserialize consistently with themselves and
that sample_from_telem() maps fields correctly -- NOT that the offsets match
a real LMU process. Live offset/sign validation is a separate [LUNCH] task.
"""
import ctypes as C

import pytest

from simcoach.lmu.rf2structs import (
    TelemInfoV01,
    TelemWheelV01,
    VehicleScoringInfoV01,
    rF2Telemetry,
)
from simcoach.lmu.smm import (
    build_lap_record,
    compatibility_check,
    open_first_available,
    sample_from_telem,
    telemetry_is_sane,
    version_ok,
)


def make_telem(**overrides) -> TelemInfoV01:
    t = TelemInfoV01()
    t.mLocalVel.x, t.mLocalVel.y, t.mLocalVel.z = 0.0, 0.0, -55.0
    t.mLocalAccel.x, t.mLocalAccel.y, t.mLocalAccel.z = 9.80665, 0.0, 0.0
    t.mUnfilteredThrottle = 0.5
    t.mUnfilteredBrake = 0.25
    t.mUnfilteredSteering = -0.3
    t.mGear = 4
    t.mEngineRPM = 7500.0
    t.mFuel = 42.0
    t.mLapNumber = 1
    t.mABSActive = True
    for i in range(4):
        t.mWheel[i].mTemperature[1] = 273.15 + 80.0  # 80C center temp
        t.mWheel[i].mBrakeTemp = 273.15 + 350.0       # 350C brake temp
        t.mWheel[i].mPressure = 165.0                 # kPa
        t.mWheel[i].mRotation = -40.0                 # rad/s (rolling)
        t.mWheel[i].mGripFract = 0.95
        t.mWheel[i].mTireLoad = 3000.0
        t.mWheel[i].mWear = 0.98
    for field, value in overrides.items():
        setattr(t, field, value)
    return t


def roundtrip(struct):
    """Serialize to bytes and back through a fresh instance of the same
    ctype, the way smm.read_struct does from a real mmap buffer."""
    raw = bytes(struct)
    copy = type(struct)()
    C.memmove(C.addressof(copy), raw, C.sizeof(copy))
    return copy


def test_telem_survives_byte_roundtrip():
    t = make_telem()
    t2 = roundtrip(t)
    assert t2.mUnfilteredThrottle == 0.5
    assert t2.mUnfilteredBrake == 0.25
    assert t2.mUnfilteredSteering == -0.3
    assert t2.mGear == 4
    assert t2.mFuel == 42.0
    assert t2.mWheel[2].mTemperature[1] == 273.15 + 80.0


def test_sample_from_telem_maps_channels():
    t = roundtrip(make_telem())
    sample = sample_from_telem(t, lap_dist=123.4)

    assert sample["speed"] == 198.0  # |(-55 in z)| m/s * 3.6 -> km/h
    assert sample["throttle"] == 0.5
    assert sample["brake"] == 0.25
    assert sample["steering"] == -0.3
    assert abs(sample["lat_g"] - 1.0) < 1e-9
    assert sample["lap_distance"] == 123.4
    assert sample["fuel"] == 42.0
    assert abs(sample["tire_temp_fl"] - 80.0) < 1e-9
    assert abs(sample["tire_temp_rr"] - 80.0) < 1e-9


def test_sample_from_telem_maps_extended_channels():
    t = roundtrip(make_telem())
    s = sample_from_telem(t, lap_dist=0.0)
    # chassis/inputs
    assert s["gear"] == 4.0
    assert s["rpm"] == 7500.0
    assert s["abs_active"] == 1.0 and s["tc_active"] == 0.0
    assert abs(s["long_g"]) < 1e-9              # z accel is 0 in make_telem
    # per-wheel: brake temp K->C, pressure, rotation, grip, load, wear
    assert abs(s["brake_temp_fl"] - 350.0) < 1e-9
    assert s["tire_press_rr"] == 165.0
    assert s["wheel_rot_fl"] == -40.0
    assert s["grip_fr"] == 0.95
    assert s["tire_load_rl"] == 3000.0
    assert s["tire_wear_fr"] == 0.98


def test_sample_from_telem_maps_energy_channels():
    t = roundtrip(make_telem(mVirtualEnergy=0.75, mSoC=0.6, mBatteryChargeFraction=0.42))
    sample = sample_from_telem(t, lap_dist=0.0)

    assert abs(sample["virtual_energy"] - 0.75) < 1e-6
    assert abs(sample["soc"] - 0.6) < 1e-6
    assert abs(sample["battery_charge"] - 0.42) < 1e-9


def test_sample_from_telem_speed_is_vector_magnitude():
    t = roundtrip(make_telem())
    t.mLocalVel.x, t.mLocalVel.y, t.mLocalVel.z = 3.0, 4.0, 0.0
    sample = sample_from_telem(t, lap_dist=0.0)
    assert sample["speed"] == 18.0  # 3-4-5 triangle (5 m/s) * 3.6 -> km/h


def test_sample_from_telem_speed_is_km_h_not_m_s():
    t = roundtrip(make_telem())
    t.mLocalVel.x, t.mLocalVel.y, t.mLocalVel.z = 0.0, 0.0, -30.0  # 30 m/s
    sample = sample_from_telem(t, lap_dist=0.0)
    assert abs(sample["speed"] - 108.0) < 1e-9
    assert abs(sample["speed"] - 30.0) > 1.0  # not left as raw m/s


def test_vehicle_scoring_survives_byte_roundtrip():
    v = VehicleScoringInfoV01()
    v.mID = 7
    v.mIsPlayer = True
    v.mLapDist = 999.5
    v.mLastLapTime = 91.234
    v.mTotalLaps = 3
    v2 = roundtrip(v)
    assert v2.mID == 7
    assert v2.mIsPlayer is True
    assert v2.mLapDist == 999.5
    assert v2.mLastLapTime == 91.234
    assert v2.mTotalLaps == 3


def test_struct_sizes_are_nonzero_and_wheel_array_has_four_entries():
    assert C.sizeof(TelemInfoV01) > 0
    assert C.sizeof(VehicleScoringInfoV01) > 0
    assert len(TelemInfoV01().mWheel) == 4
    assert len(TelemInfoV01().mOri) == 3


def test_pack4_changes_telem_size_vs_natural_alignment():
    """Guards the _pack_ = 4 decision (see rf2structs.py docstring: the
    header wraps everything in #pragma pack(push, 4)). If someone removes
    _pack_, this catches the resulting size drift."""

    class NaturallyAligned(C.Structure):
        _fields_ = TelemInfoV01._fields_

    assert C.sizeof(TelemInfoV01) != C.sizeof(NaturallyAligned)


def _channels():
    return {"speed": [100.0, 110.0]}


def test_build_lap_record_skips_sentinel_lap_time():
    record = build_lap_record(
        "lmu", "track", "car",
        lap_time=-1, invalidated=0, timestamp=0.0, channels=_channels(),
    )
    assert record is None


def test_build_lap_record_invalidated_lap_is_not_valid():
    record = build_lap_record(
        "lmu", "track", "car",
        lap_time=92.5, invalidated=1, timestamp=0.0, channels=_channels(),
    )
    assert record is not None
    assert record.valid is False


def test_build_lap_record_clean_lap_is_valid():
    record = build_lap_record(
        "lmu", "track", "car",
        lap_time=92.5, invalidated=0, timestamp=0.0, channels=_channels(),
    )
    assert record is not None
    assert record.valid is True


def test_build_lap_record_stamps_phase_and_session():
    record = build_lap_record(
        "lmu", "track", "car", lap_time=92.5, invalidated=0, timestamp=0.0,
        channels=_channels(), phase="race", session_id="s1",
    )
    assert record.phase == "race" and record.session_id == "s1"


def test_laps_commits_only_completed_freshly_timed_laps(monkeypatch):
    """Integration: LmuSource.laps() against a scripted fake buffer. Only a lap the
    car actually finished (distance wraps at S/F AND a fresh lap time posts) commits;
    an out-lap, a teleport, and an abandoned/frozen exit are all discarded."""
    import mmap
    import threading
    import time as _t
    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    # Test-only MMF names so this NEVER opens/writes LMU's real live buffers (which
    # share the production candidate names) — that would corrupt a running game and
    # pollute the store.
    tn, sn = "$simcoach_test_telem$", "$simcoach_test_scoring$"
    monkeypatch.setattr(smm, "TELEMETRY_CANDIDATES", [tn])
    monkeypatch.setattr(smm, "SCORING_CANDIDATES", [sn])
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    def write(dist, last_lap, lap_no=0, spd=180.0):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLapNumber = lap_no; v.mLocalVel.z = -spd / 3.6
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 2
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist; pv.mLastLapTime = last_lap
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    write(5.0, -1.0)
    stop = [False]

    def writer():
        def ramp(d0, d1, last, lap, secs=1.2):
            n = int(secs / 0.03)
            for i in range(n):
                if stop[0]:
                    return
                write(d0 + (d1 - d0) * i / n, last, lap); _t.sleep(0.03)
        ramp(5, 4000, -1.0, 0)                    # OUT-LAP (no valid time yet)
        write(3.0, -1.0, 1); _t.sleep(0.05)       # cross S/F, out-lap still -1 -> DROP
        ramp(3, 4000, -1.0, 1)                    # the flying lap
        write(2.0, 92.5, 2); _t.sleep(0.4)        # cross S/F, fresh time 92.5 -> COMMIT
        ramp(2, 1500, 92.5, 2)                    # into next lap...
        write(3.0, 92.5, 3); _t.sleep(0.2)        # TELEPORT-style wrap, stale time -> DROP
        ramp(3, 800, 92.5, 3)                     # drove a bit then...
        for _ in range(20):                       # ABANDONED: frozen, never wraps again
            if stop[0]:
                break
            write(800.0, 92.5, 3); _t.sleep(0.05)
        stop[0] = True

    records = []

    def cap():
        try:
            for lap in smm.LmuSource(poll_hz=60).laps():
                records.append(lap)
                if stop[0]:
                    break
        except Exception:
            pass  # mmaps closed on teardown while mid-read — expected, harmless

    threading.Thread(target=writer, daemon=True).start()
    ct = threading.Thread(target=cap, daemon=True); ct.start()
    while not stop[0]:
        _t.sleep(0.05)
    _t.sleep(0.3)
    tmap.close(); smap.close()

    assert len(records) == 1, f"expected 1 committed lap, got {len(records)}"
    assert abs(records[0].lap_time - 92.5) < 1e-6
    d = records[0].channels["lap_distance"]
    assert d.max() > 3000 and d.min() < 100   # spans the flying lap, not the out-lap/teleport


def test_session_phase_maps_msession_index():
    from simcoach.lmu.smm import session_phase
    assert session_phase(0) == "testday"
    assert session_phase(2) == "practice"
    assert session_phase(6) == "qualifying"
    assert session_phase(9) == "warmup"
    assert session_phase(13) == "race"


def _scoring_with_controls(controls: list[int]):
    from simcoach.lmu.rf2structs import rF2Scoring
    s = rF2Scoring()
    s.mScoringInfo.mNumVehicles = len(controls)
    for i, c in enumerate(controls):
        s.mVehicles[i].mControl = c
    return s


def test_session_id_for_is_stable_and_url_safe():
    from simcoach.lmu.smm import session_id_for
    a = session_id_for("Sebring International", "Ferrari 499P", online=False)
    b = session_id_for("Sebring International", "Ferrari 499P", online=False)
    assert a == b
    assert not any(ch in a for ch in " #:/?")


def test_session_id_for_differs_by_track_car_and_mode():
    from simcoach.lmu.smm import session_id_for
    base = session_id_for("Sebring", "Ferrari", online=False)
    assert session_id_for("Spa", "Ferrari", online=False) != base
    assert session_id_for("Sebring", "Porsche", online=False) != base
    assert session_id_for("Sebring", "Ferrari", online=True) != base


def test_is_online_true_with_remote_opponent():
    from simcoach.lmu.smm import _is_online
    assert _is_online(_scoring_with_controls([0, 2])) is True


def test_is_online_false_for_ai_and_solo():
    from simcoach.lmu.smm import _is_online
    assert _is_online(_scoring_with_controls([0, 1, 1])) is False  # all AI
    assert _is_online(_scoring_with_controls([0])) is False        # solo


def make_wrapper(num_vehicles: int = 1, **vehicle_overrides) -> rF2Telemetry:
    """Build an rF2Telemetry wrapper around make_telem(), the way a real MMF
    frame holds mNumVehicles + mVehicles[]."""
    wrapper = rF2Telemetry()
    wrapper.mVersionUpdateBegin = 1
    wrapper.mVersionUpdateEnd = 1
    wrapper.mNumVehicles = num_vehicles
    if num_vehicles > 0:
        wrapper.mVehicles[0] = make_telem(**vehicle_overrides)
    return wrapper


# -- open_first_available -----------------------------------------------

def test_open_first_available_returns_first_that_opens():
    calls = []

    def opener(name, size):
        calls.append(name)
        return f"handle-for-{name}"

    name, handle = open_first_available(["a", "b"], size=8, opener=opener)
    assert name == "a"
    assert handle == "handle-for-a"
    assert calls == ["a"]


def test_open_first_available_falls_back_to_second():
    def opener(name, size):
        if name == "a":
            raise OSError("does not exist")
        return f"handle-for-{name}"

    name, handle = open_first_available(["a", "b"], size=8, opener=opener)
    assert name == "b"
    assert handle == "handle-for-b"


def test_open_first_available_raises_when_all_fail():
    def opener(name, size):
        raise OSError(f"missing {name}")

    with pytest.raises(RuntimeError, match="a.*b|missing"):
        open_first_available(["a", "b"], size=8, opener=opener)


# -- telemetry_is_sane / version_ok --------------------------------------

def test_telemetry_is_sane_good_frame():
    ok, reasons = telemetry_is_sane(make_wrapper())
    assert ok is True
    assert reasons == []


def test_telemetry_is_sane_rejects_bad_num_vehicles():
    ok, reasons = telemetry_is_sane(make_wrapper(num_vehicles=999))
    assert ok is False
    assert reasons


def test_telemetry_is_sane_rejects_out_of_range_throttle():
    ok, reasons = telemetry_is_sane(make_wrapper(mUnfilteredThrottle=5.0))
    assert ok is False
    assert reasons


def test_version_ok_true_when_begin_equals_end():
    wrapper = make_wrapper()
    wrapper.mVersionUpdateBegin = 3
    wrapper.mVersionUpdateEnd = 3
    assert version_ok(wrapper) is True


def test_version_ok_false_when_torn():
    wrapper = make_wrapper()
    wrapper.mVersionUpdateBegin = 3
    wrapper.mVersionUpdateEnd = 4
    assert version_ok(wrapper) is False


# -- compatibility_check ---------------------------------------------------

class _FakeHandle:
    def __init__(self, buf: bytes):
        self._buf = buf

    def read(self, n):
        return self._buf[:n]

    def close(self):
        pass


def test_compatibility_check_ok_with_good_frame():
    good = bytes(make_wrapper())

    def opener(name, size):
        return _FakeHandle(good)

    status = compatibility_check(opener=opener)
    assert status.ok is True
    assert status.telemetry_mmf == "$rFactor2SMMP_Telemetry$"
    assert status.scoring_mmf == "$rFactor2SMMP_Scoring$"
    assert status.issues == []


def test_compatibility_check_not_ok_when_nothing_opens():
    def opener(name, size):
        raise OSError(f"missing {name}")

    status = compatibility_check(opener=opener)
    assert status.ok is False
    assert status.telemetry_mmf is None
    assert status.issues
