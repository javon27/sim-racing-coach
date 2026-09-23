"""#147: after LMU exits, its shared-memory mapping can outlive it, frozen on the
last frame. A read must see "LMU offline" then -- but a PAUSED game (still running,
buffer just not advancing) must keep reporting its state, and capture must not care.

Real test-named mappings stand in for LMU's; the process check is faked.
"""
import ctypes as C
import mmap
import uuid

import pytest

from simcoach.lmu import smm
from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

# conftest stubs these to `lambda: None` for every test; keep the real ones.
_real_read_live_status = smm.read_live_status
_real_read_live_session_id = smm.read_live_session_id
_real_read_session_track_car = smm.read_session_track_car
_real_lmu_alive = getattr(smm, "_lmu_alive", None)


@pytest.fixture
def buffers(monkeypatch):
    """A player on track, in two test-named mappings the LMU candidates point at.
    Yields write(elapsed, dist, last_lap) to publish one frame."""
    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)
    monkeypatch.setattr(smm, "TELEMETRY_CANDIDATES", [tn])
    monkeypatch.setattr(smm, "SCORING_CANDIDATES", [sn])

    def write(elapsed=10.0, dist=100.0, last_lap=-1.0):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mElapsedTime = elapsed; v.mLocalVel.z = -50.0
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 2
        s.mScoringInfo.mCurrentET = elapsed
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist
        pv.mLastLapTime = last_lap
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    write()
    yield tn, sn, write
    tmap.close(); smap.close()


def _alive(monkeypatch, value: bool):
    monkeypatch.setattr(smm, "_lmu_alive", lambda: value, raising=False)


def test_frozen_buffer_with_lmu_gone_reads_offline(buffers, monkeypatch):
    """(a) The #147 bug: the mapping still opens and still says 'on track', but the
    game that writes it has exited."""
    _alive(monkeypatch, False)
    assert _real_read_live_status() is None
    assert _real_read_live_session_id() is None


def test_paused_game_frozen_clock_still_reports_state(buffers, monkeypatch):
    """(b) Paused: the same frame, clocks frozen, read again and again -- the game is
    running, so its state stands. A clock-stall timeout would have gone offline here."""
    _alive(monkeypatch, True)
    for _ in range(5):
        live = _real_read_live_status()
        assert live == {"track": "Test Circuit", "car": "Car", "state": "track"}
    assert _real_read_live_session_id() is not None


def test_advancing_buffer_reports_state(buffers, monkeypatch):
    """(c) The normal case."""
    _, _, write = buffers
    _alive(monkeypatch, True)
    for et in (10.0, 10.02, 10.04):
        write(elapsed=et)
        assert _real_read_live_status()["state"] == "track"


def test_offline_then_lmu_comes_back_recovers(buffers, monkeypatch):
    """(d) The guard holds no latch: LMU restarting is seen on the next read."""
    _alive(monkeypatch, False)
    assert _real_read_live_status() is None
    _alive(monkeypatch, True)
    assert _real_read_live_status()["state"] == "track"


def test_every_mmf_reader_goes_through_the_guard(buffers, monkeypatch):
    """One guard in open_mmap covers the lap-start probe, the live field snapshot and
    the per-poll capture read, not just /status."""
    from simcoach.lmu.live import read_field_snapshot
    tn, sn, _ = buffers
    _alive(monkeypatch, True)
    assert read_field_snapshot().session.live is True
    assert _real_read_session_track_car() == ("Test Circuit", "Car")
    _alive(monkeypatch, False)
    assert _real_read_session_track_car() is None
    assert read_field_snapshot().session.live is False
    with pytest.raises(OSError):
        smm.LmuSource()._poll_once(tn, sn)
    with pytest.raises(RuntimeError):
        next(smm.LmuSource().laps())


def test_capture_rides_through_a_pause_and_commits_the_lap(buffers, monkeypatch):
    """Capture keeps its behaviour for a paused-but-running game: a long frozen stretch
    mid-lap neither raises nor splits the lap -- it commits once, at the line."""
    tn, sn, write = buffers
    _alive(monkeypatch, True)
    src = smm.LmuSource(now_fn=lambda: 1_700_000_000.0)
    records = []

    def step():
        rec = src._poll_once(tn, sn)
        if rec is not None:
            records.append(rec)

    for i in range(300):                        # lap in progress
        write(elapsed=10 + i * 0.02, dist=10 + i * 10)
        step()
    for _ in range(500):                        # ESC: same frame, 10 s of polls
        step()
    for i in range(300, 400):                   # resume, finish the lap
        write(elapsed=10 + i * 0.02, dist=10 + i * 10)
        step()
    write(elapsed=20.0, dist=5.0, last_lap=92.5)  # cross the line with a fresh time
    step()
    assert len(records) == 1 and records[0].lap_time == 92.5


def test_process_check_is_cached_and_recovers(monkeypatch):
    """50 Hz readers must not enumerate processes per sample: one snapshot per TTL,
    and a game that starts again is seen once the TTL lapses."""
    assert _real_lmu_alive is not None
    clock = [1000.0]
    running = [False]
    calls = []
    monkeypatch.setattr(smm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(smm, "_lmu_process_running", lambda: calls.append(1) or running[0])
    monkeypatch.setattr(smm, "_lmu_alive_cache", (-smm._LMU_ALIVE_TTL_S, True))

    assert [_real_lmu_alive() for _ in range(50)] == [False] * 50
    assert len(calls) == 1
    running[0] = True
    clock[0] += smm._LMU_ALIVE_TTL_S
    assert _real_lmu_alive() is True
    assert len(calls) == 2


def test_the_real_process_check_finds_a_running_exe_and_not_a_missing_one(monkeypatch):
    """The Toolhelp walk itself, unfaked: a wrong struct layout or name compare would
    otherwise pass every other test here while the guard reads LMU as never running."""
    import os
    import sys

    monkeypatch.setattr(smm, "_LMU_EXE", os.path.basename(sys.executable).lower())
    assert smm._lmu_process_running() is True
    monkeypatch.setattr(smm, "_LMU_EXE", uuid.uuid4().hex + ".exe")
    assert smm._lmu_process_running() is False
