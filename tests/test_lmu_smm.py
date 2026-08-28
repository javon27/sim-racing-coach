"""Self-consistency tests for simcoach/lmu/rf2structs.py + smm.py.

These do NOT touch real shared memory (LMU is not running). They prove our
own ctypes structs serialize/deserialize consistently with themselves and
that sample_from_telem() maps fields correctly -- NOT that the offsets match
a real LMU process. Live offset/sign validation is a separate [LUNCH] task.
"""
import ctypes as C

from simcoach.lmu.rf2structs import TelemInfoV01, TelemWheelV01, VehicleScoringInfoV01
from simcoach.lmu.smm import sample_from_telem


def make_telem(**overrides) -> TelemInfoV01:
    t = TelemInfoV01()
    t.mLocalVel.x, t.mLocalVel.y, t.mLocalVel.z = 0.0, 0.0, -55.0
    t.mLocalAccel.x, t.mLocalAccel.y, t.mLocalAccel.z = 9.80665, 0.0, 0.0
    t.mUnfilteredThrottle = 0.5
    t.mUnfilteredBrake = 0.25
    t.mUnfilteredSteering = -0.3
    t.mGear = 4
    t.mFuel = 42.0
    t.mLapNumber = 1
    for i in range(4):
        t.mWheel[i].mTemperature[1] = 273.15 + 80.0  # 80C center temp
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

    assert sample["speed"] == 55.0  # |(-55 in z)|
    assert sample["throttle"] == 0.5
    assert sample["brake"] == 0.25
    assert sample["steering"] == -0.3
    assert abs(sample["lat_g"] - 1.0) < 1e-9
    assert sample["lap_distance"] == 123.4
    assert sample["fuel"] == 42.0
    assert abs(sample["tire_temp_fl"] - 80.0) < 1e-9
    assert abs(sample["tire_temp_rr"] - 80.0) < 1e-9


def test_sample_from_telem_speed_is_vector_magnitude():
    t = roundtrip(make_telem())
    t.mLocalVel.x, t.mLocalVel.y, t.mLocalVel.z = 3.0, 4.0, 0.0
    sample = sample_from_telem(t, lap_dist=0.0)
    assert sample["speed"] == 5.0  # 3-4-5 triangle


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
