"""Self-consistency tests for simcoach/lmu/rf2structs.py + smm.py.

These do NOT touch real shared memory (LMU is not running). They prove our
own ctypes structs serialize/deserialize consistently with themselves and
that sample_from_telem() maps fields correctly -- NOT that the offsets match
a real LMU process. Live offset/sign validation is a separate [LUNCH] task.
"""
import ctypes as C
import math
import uuid

import pytest

from simcoach.lmu.rf2structs import (
    TelemInfoV01,
    VehicleScoringInfoV01,
    rF2Telemetry,
)
from simcoach.lmu.smm import (
    build_lap_record,
    compatibility_check,
    compute_sectors,
    open_first_available,
    player_position,
    sample_from_telem,
    sector_bounds_from,
    telemetry_is_sane,
    version_ok,
)


def test_sector_bounds_from_two_clean_transitions():
    assert sector_bounds_from([1900.0, 3800.0], 5793.0) == pytest.approx([1900.0, 3800.0])


def test_sector_bounds_from_guards():
    # Wrong count (missing a transition, or a flicker adding a third).
    assert sector_bounds_from([1900.0], 5793.0) is None
    assert sector_bounds_from([1000.0, 2000.0, 3000.0], 5793.0) is None
    # Out of order / not strictly increasing.
    assert sector_bounds_from([3800.0, 1900.0], 5793.0) is None
    assert sector_bounds_from([1900.0, 1900.0], 5793.0) is None
    # Out of (0, length).
    assert sector_bounds_from([0.0, 3800.0], 5793.0) is None
    assert sector_bounds_from([1900.0, 6000.0], 5793.0) is None
    # No/invalid lap length.
    assert sector_bounds_from([1900.0, 3800.0], None) is None
    assert sector_bounds_from([1900.0, 3800.0], 0.0) is None


def test_build_lap_record_carries_sector_bounds():
    record = build_lap_record(
        "lmu", "track", "car", lap_time=92.5, invalidated=0, timestamp=0.0,
        channels={"speed": [100.0]}, sector_bounds=[1900.0, 3800.0],
    )
    assert record.sector_bounds == [1900.0, 3800.0]


def test_player_position_positive_is_int():
    assert player_position(5) == 5
    assert player_position(1) == 1


def test_player_position_zero_or_unset_is_none():
    assert player_position(0) is None
    assert player_position(None) is None


def test_compute_sectors_reconciling_values():
    # mLastSector2 is cumulative (s1+s2); lap = 91.2
    assert compute_sectors(30.1, 60.3, 91.2) == pytest.approx([30.1, 30.2, 30.9])


def test_compute_sectors_bad_convention_returns_none():
    # If mLastSector2 were ISOLATED (just s2 ~30, not cumulative ~60), it reads
    # < s1, so the derived s2 = last_s2 - last_s1 goes negative -> guard rejects
    # rather than store garbage.
    assert compute_sectors(30.1, 29.0, 91.2) is None


def test_compute_sectors_nonpositive_returns_none():
    assert compute_sectors(0.0, 60.0, 91.0) is None          # s1 <= 0
    assert compute_sectors(60.3, 30.1, 91.2) is None          # s2 <= 0 (s2 = -30.2)
    assert compute_sectors(30.1, 91.2, 91.2) is None          # s3 <= 0


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


def test_sample_from_telem_maps_setup_channels():
    """Issue #38 Phase 0: capture-only setup channels (ride height, suspension
    deflection/force, tire deflection, lateral patch velocity, camber, and the
    tread-edge temp pair). Raw passthrough except Kelvin->C on the tread pair.
    tire_temp_l/r use the header's actual left/center/right order -- NOT
    in/out, since for the right-side wheels "left" is the outside."""
    t = make_telem()
    for i in range(4):
        t.mWheel[i].mRideHeight = 0.045
        t.mWheel[i].mSuspensionDeflection = 0.012
        t.mWheel[i].mSuspForce = 3500.0
        t.mWheel[i].mVerticalTireDeflection = 0.008
        t.mWheel[i].mLateralPatchVel = 0.15
        t.mWheel[i].mCamber = -0.03
        t.mWheel[i].mTemperature[0] = 273.15 + 85.0
        t.mWheel[i].mTemperature[2] = 273.15 + 75.0
    t = roundtrip(t)
    s = sample_from_telem(t, lap_dist=0.0)
    for name in ("fl", "fr", "rl", "rr"):
        assert s[f"ride_height_{name}"] == pytest.approx(0.045)
        assert s[f"susp_defl_{name}"] == pytest.approx(0.012)
        assert s[f"susp_force_{name}"] == pytest.approx(3500.0)
        assert s[f"tire_defl_{name}"] == pytest.approx(0.008)
        assert s[f"lat_patch_vel_{name}"] == pytest.approx(0.15)
        assert s[f"camber_{name}"] == pytest.approx(-0.03)
        assert s[f"tire_temp_l_{name}"] == pytest.approx(85.0)
        assert s[f"tire_temp_r_{name}"] == pytest.approx(75.0)
        # center temp (tire_temp_{w}) is unchanged by this addition
        assert s[f"tire_temp_{name}"] == pytest.approx(80.0)


def test_sample_from_telem_maps_energy_channels():
    t = roundtrip(make_telem(mVirtualEnergy=0.75, mSoC=0.6, mBatteryChargeFraction=0.42))
    sample = sample_from_telem(t, lap_dist=0.0)

    assert abs(sample["virtual_energy"] - 0.75) < 1e-6
    assert abs(sample["soc"] - 0.6) < 1e-6
    assert abs(sample["battery_charge"] - 0.42) < 1e-9


def test_sample_from_telem_maps_lap_time_channels():
    """Issue #38 prerequisite: capture the in-lap time axis. mElapsedTime and
    mLapStartET are declared on TelemInfoV01 but were never read, so every lap
    driven so far is permanently missing a time channel and delta-t has to be
    integrated as ds/(v/3.6) off the 5 Hz scoring lap_distance.

    time_into_lap is derived per-sample rather than at read time so it stays
    correct across the lap seam, where mLapStartET jumps mid-array."""
    t = roundtrip(make_telem(mElapsedTime=1234.5, mLapStartET=1200.0))
    s = sample_from_telem(t, lap_dist=0.0)
    assert s["elapsed_time"] == 1234.5
    assert s["lap_start_et"] == 1200.0
    assert abs(s["time_into_lap"] - 34.5) < 1e-9


def test_sample_from_telem_maps_driver_aid_index_channels():
    """Issues #33/#38: TelemInfoV01 declares each cockpit-adjustable map as a
    current+ceiling uint8 pair, the same shape as the garage payload's
    value/maxValue. The four aid ones are named after the garage keys they would
    be cross-checked against (VM_ANTILOCKBRAKESYSTEMMAP, VM_TRACTIONCONTROLMAP,
    ...POWERCUTMAP, ...SLIPANGLEMAP); the rest mirror the struct.

    Capture only -- what these fields actually hold on track is unmeasured.
    abs_map is distinct from the existing abs_active, which is engagement."""
    t = roundtrip(make_telem(
        mABS=3, mABSMax=10, mTC=4, mTCMax=12, mTCSlip=10, mTCSlipMax=12,
        mTCCut=3, mTCCutMax=12, mMotorMap=2, mMotorMapMax=6,
        mMigration=5, mMigrationMax=9, mFrontAntiSway=1, mFrontAntiSwayMax=7,
        mRearAntiSway=6, mRearAntiSwayMax=7,
    ))
    s = sample_from_telem(t, lap_dist=0.0)
    assert (s["abs_map"], s["abs_map_max"]) == (3.0, 10.0)
    assert (s["tc_map"], s["tc_map_max"]) == (4.0, 12.0)
    assert (s["tc_slip_map"], s["tc_slip_map_max"]) == (10.0, 12.0)
    assert (s["tc_cut_map"], s["tc_cut_map_max"]) == (3.0, 12.0)
    assert (s["motor_map"], s["motor_map_max"]) == (2.0, 6.0)
    assert (s["migration"], s["migration_max"]) == (5.0, 9.0)
    assert (s["front_antisway"], s["front_antisway_max"]) == (1.0, 7.0)
    assert (s["rear_antisway"], s["rear_antisway_max"]) == (6.0, 7.0)
    # the engagement booleans are untouched and still mean something else
    assert s["abs_active"] == 1.0 and s["tc_active"] == 0.0


def test_sample_from_telem_maps_damage_channels():
    """#143, the exact 2026-09-20 rig readout (Imola, Garage 59 #10, 1.3 s after
    a real front-left hit) -- the fixture the repo's own "tests that encode the
    bug" lesson demands, not invented numbers."""
    t = make_telem()
    t.mDentSeverity = (C.c_ubyte * 8)(0, 0, 0, 0, 0, 0, 0, 1)
    t.mLastImpactET = 2178.5
    t.mLastImpactMagnitude = 365.47
    t.mLastImpactPos.x, t.mLastImpactPos.y, t.mLastImpactPos.z = -1.02, 0.20, 1.75
    t.mOverheating = False
    t.mDetached = False
    t.mWheel[0].mFlat = False
    t.mWheel[0].mDetached = False
    t = roundtrip(t)
    s = sample_from_telem(t, lap_dist=0.0)

    assert s["dent_max"] == 1.0
    assert s["dent_total"] == 1.0
    assert s["last_impact_et"] == pytest.approx(2178.5)
    assert s["last_impact_mag"] == pytest.approx(365.47)
    assert s["last_impact_x"] == pytest.approx(-1.02)
    assert s["last_impact_z"] == pytest.approx(1.75)
    assert s["overheating"] == 0.0
    assert s["detached"] == 0.0
    assert s["flat_fl"] == 0.0
    assert s["wheel_detached_fl"] == 0.0


def test_sample_from_telem_maps_rear_brake_bias():
    """mRearBrakeBias is a plain double in the offset-anchored region with
    unambiguous rF2 semantics, unlike the uint8 map indices above -- it is what
    would settle the brake-balance half of #33. Stored raw: the scale (fraction
    vs percent) is inferred, not measured, so no conversion is applied here.
    A populated aid *Max (mABSMax) is included so the #33 pre-population gate
    below does not mask this value -- see test_electronics_block_gated_on_max."""
    t = roundtrip(make_telem(mRearBrakeBias=0.485, mABSMax=10))
    assert sample_from_telem(t, lap_dist=0.0)["rear_brake_bias"] == pytest.approx(0.485)


def test_electronics_block_is_nan_before_any_max_populates():
    """Issue #33: at frame 1, in the garage, LMU's aid *Max fields (and everything
    in the same block, including mRearBrakeBias) hold pre-population state --
    '0 of 0-0' for the aid indices, but a perfectly plausible 0.5150 for brake
    bias, which a value-based range check cannot catch. Gate on any *Max > 0
    instead: until one populates, every field in the block must read NaN, not
    a plausible-looking number."""
    t = roundtrip(make_telem(mRearBrakeBias=0.5150))   # all *Max default to 0
    s = sample_from_telem(t, lap_dist=0.0)
    for key in ("abs_map", "abs_map_max", "tc_map", "tc_map_max",
                "tc_slip_map", "tc_slip_map_max", "tc_cut_map", "tc_cut_map_max",
                "motor_map", "motor_map_max", "migration", "migration_max",
                "front_antisway", "front_antisway_max",
                "rear_antisway", "rear_antisway_max", "rear_brake_bias"):
        assert math.isnan(s[key]), key


def test_electronics_block_reports_values_once_any_max_populates():
    """The gate keys off ANY *Max being populated -- e.g. mMigrationMax stays 0
    all lap (brake migration never touched), but the block is trusted anyway
    once a sibling *Max (mABSMax) has populated."""
    t = roundtrip(make_telem(mABS=3, mABSMax=10, mMigration=0, mMigrationMax=0,
                             mRearBrakeBias=0.4950))
    s = sample_from_telem(t, lap_dist=0.0)
    assert (s["abs_map"], s["abs_map_max"]) == (3.0, 10.0)
    assert (s["migration"], s["migration_max"]) == (0.0, 0.0)
    assert s["rear_brake_bias"] == pytest.approx(0.4950)


def test_lap_time_channels_survive_a_store_round_trip(tmp_path):
    """The new channels are purely additive: store.save/load pass channels through
    generically via npz, so no schema migration is needed -- prove it end to end
    from sample_from_telem output rather than a hand-built dict."""
    from simcoach.model_types import LapRecord
    from simcoach.store import LapStore

    samples = [
        sample_from_telem(roundtrip(make_telem(mElapsedTime=100.0 + i * 0.02,
                                               mLapStartET=100.0, mABS=3, mABSMax=10,
                                               mRearBrakeBias=0.485)),
                          lap_dist=i * 1.0)
        for i in range(5)
    ]
    channels = {k: [s[k] for s in samples] for k in samples[0]}
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(LapRecord("lmu", "t", "c", 92.5, True, 0.0, channels))
    back = store.load(lap_id).channels

    assert list(back["time_into_lap"]) == pytest.approx([0.0, 0.02, 0.04, 0.06, 0.08])
    assert list(back["elapsed_time"]) == pytest.approx([100.0 + i * 0.02 for i in range(5)])
    assert list(back["lap_start_et"]) == pytest.approx([100.0] * 5)
    assert list(back["abs_map"]) == pytest.approx([3.0] * 5)
    assert list(back["abs_map_max"]) == pytest.approx([10.0] * 5)
    assert list(back["rear_brake_bias"]) == pytest.approx([0.485] * 5)
    assert list(back["speed"]) == pytest.approx([198.0] * 5)  # existing channels intact


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


def test_build_lap_record_carries_lap_no_and_pit():
    record = build_lap_record(
        "lmu", "track", "car", lap_time=92.5, invalidated=0, timestamp=0.0,
        channels=_channels(), lap_no=8, pit=True,
    )
    assert record.lap_no == 8
    assert record.pit is True


def test_build_lap_record_defaults_lap_no_and_pit_to_none():
    record = build_lap_record(
        "lmu", "track", "car", lap_time=92.5, invalidated=0, timestamp=0.0,
        channels=_channels(),
    )
    assert record.lap_no is None
    assert record.pit is None


def test_laps_commits_only_completed_freshly_timed_laps():
    """Unit: LmuSource._poll_once() against a scripted fake buffer, driven
    synchronously -- write a sample, then poll once, no threads and no sleep-based
    races. Only a lap the car actually finished (distance wraps at S/F AND a fresh
    lap time posts) commits; an out-lap, a teleport, and an abandoned/frozen exit are
    all discarded.

    This used to drive a writer thread against LmuSource.laps() with 0.03s
    wall-clock sleeps and assert on whatever the reader thread happened to capture --
    flaky under load (issue #52) because it raced two real OS threads' scheduling
    instead of testing the commit decision itself. _poll_once() (smm.py) is the
    single-step unit extracted so this can drive the decision deterministically.
    """
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    # Test-only MMF names so this NEVER opens/writes LMU's real live buffers (which
    # share the production candidate names) -- that would corrupt a running game and
    # pollute the store.
    # Windows named mmaps are session-global, not process-scoped: a fixed tagname
    # collides across concurrent pytest PROCESSES (not just threads), which is the
    # actual root cause of issue #52's "flaky under load" -- three suites running
    # concurrently each open the SAME named shared memory and stomp on each other's
    # writes. A per-run unique suffix makes this test process-isolated.
    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
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

    # Fixed, injected clock: the commit path stamps timestamp=now_fn() rather than
    # racing a real wall clock -- see LmuSource.__init__(now_fn=...).
    src = smm.LmuSource(poll_hz=60, now_fn=lambda: 1_700_000_000.0)
    records = []

    def step():
        record = src._poll_once(tn, sn)
        if record is not None:
            records.append(record)

    def ramp(d0, d1, last, lap, n=40):
        for i in range(n):
            write(d0 + (d1 - d0) * i / n, last, lap)
            step()

    write(5.0, -1.0); step()
    ramp(5, 4000, -1.0, 0)                    # OUT-LAP (no valid time yet)
    write(3.0, -1.0, 1); step()               # cross S/F, out-lap still -1 -> DROP
    ramp(3, 4000, -1.0, 1)                    # the flying lap
    write(2.0, 92.5, 2); step()               # cross S/F, fresh time 92.5 -> COMMIT
    ramp(2, 1500, 92.5, 2)                    # into next lap...
    write(3.0, 92.5, 3); step()               # TELEPORT-style wrap, stale time -> DROP
    ramp(3, 800, 92.5, 3)                     # drove a bit then...
    for _ in range(20):                       # ABANDONED: frozen, never wraps again
        write(800.0, 92.5, 3); step()

    tmap.close(); smap.close()

    assert len(records) == 1, f"expected 1 committed lap, got {len(records)}"
    assert abs(records[0].lap_time - 92.5) < 1e-6
    d = records[0].channels["lap_distance"]
    assert d.max() > 3000 and d.min() < 100   # spans the flying lap, not the out-lap/teleport
    # Exact sample count -- catches a skipped buffer reset (e.g. the per-lap
    # `self._channels = {}` in _poll_once no-opped): a leaked out-lap buffer would
    # merge into this one and still happen to satisfy the range check above.
    assert len(d) == 41


def test_laps_commits_game_lap_no_and_pit_flag_from_scoring():
    """Unit: _poll_once() records the SCORING struct's mTotalLaps (the game's own
    lap counter, read at the same commit as mLastLapTime) as LapRecord.lap_no --
    issue #77's rig log: mTotalLaps at the wrap equals the result XML `num` of the
    lap that just finished. LapRecord.pit is True if mInPits was ever true at any
    polled instant during the lap, else False -- needed by #69's map-rebuild gate
    to exclude pit laps by fact rather than by lateral-spread shape."""
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    def write(dist, last_lap, total_laps=0, in_pits=False, spd=180.0):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLocalVel.z = -spd / 3.6
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 2
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist
        pv.mLastLapTime = last_lap; pv.mTotalLaps = total_laps; pv.mInPits = in_pits
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    src = smm.LmuSource(poll_hz=60, now_fn=lambda: 1_700_000_000.0)
    records = []

    def step():
        record = src._poll_once(tn, sn)
        if record is not None:
            records.append(record)

    def ramp(d0, d1, last, total_laps, in_pits=False, n=10):
        for i in range(n):
            write(d0 + (d1 - d0) * i / n, last, total_laps, in_pits)
            step()

    write(5.0, -1.0, total_laps=1); step()        # start of an out-lap
    ramp(5, 4000, -1.0, 1)                         # out-lap, never in pits
    write(3.0, -1.0, 2); step()                    # cross S/F ending out-lap: untimed -> DROP
    ramp(3, 4000, -1.0, 2)                         # lap A: the flying lap, never in pits
    write(2.0, 92.5, 2); step()                    # cross S/F, fresh time -> COMMIT lap A
    ramp(2, 1500, 92.5, 3, in_pits=True)           # lap B: dips into the pits partway...
    ramp(1500, 4000, 92.5, 3, in_pits=False)       # ...then back out for the rest of it
    write(2.0, 88.0, 3); step()                    # cross S/F, fresh time -> COMMIT lap B

    tmap.close(); smap.close()

    assert len(records) == 2
    assert records[0].lap_no == 2 and records[0].pit is False
    assert records[1].lap_no == 3 and records[1].pit is True


def test_sector_boundary_dists_forward_transitions_only():
    from simcoach.lmu.smm import sector_boundary_dists
    # A lap's samples: sector steps 1->2 at d=1000, 2->3 at d=2000; the S/F wrap
    # back to 1 (a DECREASE) is not a boundary.
    dists = [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0, 0.0]
    sectors = [1, 1, 2, 2, 3, 3, 1]
    assert sector_boundary_dists(dists, sectors) == [1000.0, 2000.0]
    # Feeds cleanly into the guard.
    from simcoach.lmu.smm import sector_bounds_from
    assert sector_bounds_from(sector_boundary_dists(dists, sectors), 3000.0) == [1000.0, 2000.0]


def test_sector_boundary_dists_messy_flicker_is_accepted_by_the_guard():
    # #115 audit finding 8: this test used to be named "..._rejected_by_guard" but
    # never fed the flicker to the guard at all -- it only checked the extractor
    # (sector_boundary_dists) and separately checked the guard's trivial
    # len(...) != 2 arm on unrelated input. No assertion here would have failed
    # if the guard rejected nothing. Fixed to assert what actually happens:
    # a genuine 1->2->1->2 flicker produces exactly two FORWARD transitions
    # (the guard's own bar, per its docstring), so it passes every check
    # sector_bounds_from makes and is ACCEPTED, not rejected. That's a real
    # behaviour gap (a flicker is not a clean single crossing) -- tracked as a
    # follow-up decision (tighten the guard to require monotonic sectors, or
    # ship [500.0, 900.0] as the real reported split), not fixed here: changing
    # guard behaviour would change what lands in stored laps, which is outside
    # this audit-cleanup finding's scope.
    from simcoach.lmu.smm import sector_boundary_dists, sector_bounds_from
    flick_d = [0.0, 500.0, 700.0, 900.0]
    flick_s = [1, 2, 1, 2]
    boundaries = sector_boundary_dists(flick_d, flick_s)
    assert boundaries == [500.0, 900.0]
    assert sector_bounds_from(boundaries, 3000.0) == [500.0, 900.0]
    # An incomplete lap (only one transition) IS rejected -> None.
    assert sector_bounds_from(sector_boundary_dists([0.0, 1000.0], [1, 2]), 3000.0) is None


def test_laps_records_sector_bounds_end_to_end():
    """Unit guard: _poll_once() reads mCurrentSector from the TELEM struct and
    a flying lap stepping 1->2->3 records two boundaries. Driven synchronously
    (write, then poll once) -- no threads, no sleep-based races. Mirrors the
    committed-lap test's cadence (proven deterministic) and shares its mechanism:
    see LmuSource._poll_once (smm.py)."""
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    # See test_laps_commits_only_completed_freshly_timed_laps: a fixed tagname
    # collides across concurrent pytest processes on Windows (session-global named
    # mmaps), which was the real cause of issue #52's "flaky under load".
    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_sb_{tag}$", f"$simcoach_test_scoring_sb_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    def write(dist, last_lap, sector, lap_no=0, spd=180.0):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLapNumber = lap_no; v.mLocalVel.z = -spd / 3.6
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"; v.mCurrentSector = sector
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 2
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist; pv.mLastLapTime = last_lap
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    src = smm.LmuSource(poll_hz=60, now_fn=lambda: 1_700_000_000.0)
    records = []

    def step():
        record = src._poll_once(tn, sn)
        if record is not None:
            records.append(record)

    def ramp(d0, d1, last, lap, n=40):
        for i in range(n):
            d = d0 + (d1 - d0) * i / n
            sector = 1 if d < 1500 else (2 if d < 3000 else 3)
            write(d, last, sector, lap)
            step()

    write(5.0, -1.0, 1); step()
    ramp(3, 4500, -1.0, 1)                        # the flying lap, sectors step 1->2->3
    write(2.0, 92.5, 1, lap_no=2); step()         # cross S/F, fresh time -> COMMIT

    tmap.close(); smap.close()

    assert len(records) == 1
    b = records[0].sector_bounds
    assert b is not None, "expected two clean sector transitions to record bounds"
    assert 1300 < b[0] < 1700 and 2800 < b[1] < 3200
    # Exact sample count -- catches a skipped buffer reset (e.g. the per-lap
    # `self._channels = {}` in _poll_once no-opped) that a range-only check on
    # sector bounds wouldn't notice.
    assert len(records[0].channels["lap_distance"]) == 41


def test_laps_resets_buffer_across_reused_instance_no_ghost_lap(monkeypatch):
    """A long-lived capture loop reuses ONE LmuSource across LMU drop/reconnect
    cycles: `while True: try: for lap in source.laps(): ...` -- on a RuntimeError
    (dropped connection) it loops back and calls laps() again on the SAME instance.
    A second laps() call must start with a clean buffer: otherwise a stale
    _prev_dist can read the reconnect frame as a finish-line wrap of the pre-drop
    buffer, and a stale _committed_time (still None, never having committed) lets
    it through -- a "ghost lap" gets committed under whatever mLastLapTime the sim
    happens to be reporting at reconnect, built from samples the car never drove in
    that lap. Scripted read_consistent feed -- no threads, no mmaps, deterministic.
    """
    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    def frame(dist, last_lap, lap_no=1):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLapNumber = lap_no; v.mLocalVel.z = -50.0
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 2
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist; pv.mLastLapTime = last_lap
        return t, s

    # Pre-drop: partway through a lap (never reaches a wrap) before the connection dies.
    pre_drop = [frame(500.0, -1.0), frame(1500.0, -1.0), frame(2900.0, -1.0)]
    # Post-reconnect: ONE frame, near the S/F line, with a STALE-but-valid lap_time
    # (95.0, left over in the sim's mLastLapTime from before the drop) -- the trap.
    post_reconnect = [frame(50.0, 95.0)]

    script = iter(pre_drop + [None] + post_reconnect + [None])
    telem_q: list = []
    scoring_q: list = []

    def fake_read_consistent(name, ctype, retries=6):
        # A new frame is pulled only once BOTH queues have drained (i.e. at the
        # true start of the next _poll_once call, which reads telem then scoring)
        # -- pulling on "telem_q empty" alone desyncs the pair, since telem_q is
        # also empty immediately after the telem half of the SAME call is served.
        if not telem_q and not scoring_q:
            nxt = next(script)
            if nxt is None:
                raise RuntimeError("connection dropped")  # what a real reconnect sees
            t, s = nxt
            telem_q.append(t)
            scoring_q.append(s)
        return telem_q.pop(0) if ctype is rF2Telemetry else scoring_q.pop(0)

    class _NullHandle:
        def close(self):
            pass

    monkeypatch.setattr(smm, "read_consistent", fake_read_consistent)
    monkeypatch.setattr(
        smm, "open_first_available",
        lambda candidates, size: (candidates[0], _NullHandle()),
    )
    monkeypatch.setattr(smm.time, "sleep", lambda s: None)

    src = smm.LmuSource(poll_hz=1000, now_fn=lambda: 0.0)

    def drain_until_drop(gen):
        # Keeps whatever committed before the RuntimeError -- list(gen) would
        # discard that on the exception, which is exactly what we need to inspect.
        records = []
        try:
            for lap in gen:
                records.append(lap)  # noqa: PERF402 -- must keep partial results on the exception below; list(gen) can't
        except RuntimeError:
            pass  # what a reconnecting capture loop catches before retrying
        return records

    # First connection: accumulates a partial-lap buffer, then drops. Nothing
    # commits (no wrap ever occurs pre-drop).
    records_before_drop = drain_until_drop(src.laps())
    assert records_before_drop == []

    # Reconnect: a NEW laps() call on the SAME instance.
    records_after_reconnect = drain_until_drop(src.laps())

    assert records_after_reconnect == [], (
        f"ghost lap committed from a leaked pre-drop buffer: {records_after_reconnect!r}"
    )


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


def test_read_consistent_retries_past_a_torn_read(monkeypatch):
    """The regression issue #52 is really about: read_consistent must not hand back
    a frame whose mVersionUpdateBegin != mVersionUpdateEnd (caught mid-write) -- it
    has to retry read_struct until version_ok() passes. Exercised directly via a
    monkeypatched read_struct, so it's deterministic (no real shared memory, no
    threads, no sleeps). The old writer-thread/reader-thread integration tests set
    begin=end=1 on every write, which never tripped this guard and let torn,
    interleaved reads through -- undetected -- as the actual cause of their flake."""
    from simcoach.lmu import smm

    torn = make_wrapper()
    torn.mVersionUpdateBegin, torn.mVersionUpdateEnd = 2, 1  # mid-write
    settled = make_wrapper()
    settled.mVersionUpdateBegin = settled.mVersionUpdateEnd = 5

    reads = [torn, torn, settled]
    calls = []

    def fake_read_struct(name, ctype):
        calls.append(name)
        return reads.pop(0)

    monkeypatch.setattr(smm, "read_struct", fake_read_struct)
    result = smm.read_consistent("dummy", rF2Telemetry, retries=6)

    assert result is settled
    assert len(calls) == 3  # 1 initial read + 2 retries before it settled


def test_read_consistent_gives_up_after_retries_exhausted(monkeypatch):
    """Best-effort per the docstring: if it never settles within `retries`, return
    the last read rather than hanging or (worse) fabricating a consistent one."""
    from simcoach.lmu import smm

    calls = []

    def always_torn(name, ctype):
        calls.append(name)
        w = make_wrapper()
        w.mVersionUpdateBegin, w.mVersionUpdateEnd = 2, 1
        return w

    monkeypatch.setattr(smm, "read_struct", always_torn)
    result = smm.read_consistent("dummy", rF2Telemetry, retries=3)

    assert version_ok(result) is False  # gave up, didn't fabricate consistency
    assert len(calls) == 4  # 1 initial read + 3 retries, all torn


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


# --- issue #77: out/in/partial laps are STORED, not discarded -------------------

def _partial_rig(tag: str):
    """(write, step, src, records, close) over a private pair of test MMFs, the same
    scripted-buffer harness the commit tests use. Extracted only because the four
    partial-lap cases below each need their own fresh LmuSource."""
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    def write(dist, last_lap, total_laps=0, in_pits=False, spd=180.0):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLocalVel.z = -spd / 3.6
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 2
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist
        pv.mLastLapTime = last_lap; pv.mTotalLaps = total_laps; pv.mInPits = in_pits
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    src = smm.LmuSource(poll_hz=60, now_fn=lambda: 1_700_000_000.0)
    records = []

    def step():
        record = src._poll_once(tn, sn)
        if record is not None:
            records.append(record)

    def close():
        tmap.close(); smap.close()

    return write, step, records, close


def test_out_lap_fragment_is_stored_as_a_partial_lap_with_no_time():
    """Rig 2026-09-15: the out lap is the game's lap 1 and used to be DISCARDED
    (mLastLapTime is still -1 at the line), so the app's numbering jumped. It is now
    stored with lap_time None, valid False, partial 'out' and the game's own lap_no
    -- 'out' because mInPits was true on the FIRST frame of the fragment."""
    write, step, records, close = _partial_rig(uuid.uuid4().hex)

    write(5.0, -1.0, total_laps=1, in_pits=True); step()     # leaves the pit box
    for i in range(1, 250):                                   # drives the out lap
        write(5.0 + i * 16.0, -1.0, total_laps=1); step()
    write(3.0, -1.0, total_laps=1); step()                    # crosses S/F, still untimed
    close()

    assert len(records) == 1
    r = records[0]
    assert r.partial == "out"
    assert r.lap_time is None
    assert r.valid is False
    assert r.lap_no == 1
    assert r.sectors is None
    assert len(r.channels["lap_distance"]) == 250


def test_in_lap_fragment_is_stored_as_partial_in():
    """The lap the driver pits on: untimed at the wrap, mInPits true on the WRAP
    frame."""
    write, step, records, close = _partial_rig(uuid.uuid4().hex)

    write(5.0, -1.0, total_laps=9); step()
    for i in range(1, 250):
        write(5.0 + i * 16.0, -1.0, total_laps=9); step()
    write(3.0, -1.0, total_laps=10, in_pits=True); step()     # dives into the pit lane
    close()

    assert len(records) == 1
    assert records[0].partial == "in"
    assert records[0].lap_no == 10
    # `pit` keeps its shipped meaning -- mInPits over the lap's OWN sampled frames,
    # which end before the wrap frame. The wrap frame's mInPits is what makes the
    # kind 'in'; it is deliberately not retconned into this lap's pit flag.
    assert records[0].pit is False


def test_spin_fragment_with_no_pit_involvement_is_stored_as_partial():
    """Neither in the pits at the start nor at the wrap (a spin/teleport back to
    the line): still worth keeping -- a spin lap's inputs are exactly what the
    driver asks about."""
    write, step, records, close = _partial_rig(uuid.uuid4().hex)

    write(5.0, -1.0, total_laps=4); step()
    for i in range(1, 250):
        write(5.0 + i * 16.0, -1.0, total_laps=4); step()
    write(3.0, -1.0, total_laps=5); step()
    close()

    assert len(records) == 1
    assert records[0].partial == "partial"
    assert records[0].pit is False


def test_garage_return_with_almost_no_telemetry_stores_nothing():
    """A return to the garage ticks the game's lap counter with no line crossing and
    leaves a handful of samples over a few metres. There is nothing to analyse, so it
    is still DROPPED -- the fragment thresholds are what separate the two."""
    write, step, records, close = _partial_rig(uuid.uuid4().hex)

    write(5.0, -1.0, total_laps=5); step()
    for i in range(1, 40):
        write(5.0 + i * 2.0, -1.0, total_laps=5); step()      # 40 samples, ~80 m
    write(1.0, -1.0, total_laps=6); step()
    close()

    assert records == []


def test_build_lap_record_keeps_an_untimed_lap_when_it_is_flagged_partial():
    from simcoach.lmu.smm import build_lap_record

    ch = {"speed": [100.0, 101.0], "lap_distance": [0.0, 10.0]}
    rec = build_lap_record(sim="lmu", track="T", car="C", lap_time=None, invalidated=False,
                           timestamp=1.0, channels=ch, partial="out", lap_no=1)
    assert rec is not None
    assert rec.lap_time is None
    assert rec.valid is False       # so every `valid = 1` consumer ignores it, unchanged
    assert rec.partial == "out"
    # Unflagged, an untimed lap is still the sentinel that must never be stored.
    assert build_lap_record(sim="lmu", track="T", car="C", lap_time=-1.0, invalidated=False,
                            timestamp=1.0, channels=ch) is None
