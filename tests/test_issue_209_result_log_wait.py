"""#209: a race review asked right after the flag ran before LMU wrote the result log.

Timings are the rig's (Long Beach, 2026-09-27, online): the in-lap committed at
22:36:34 (T0 + 1980.03, T0 = the log's race start), get_race_result ran at 22:37:07
(+33 s), LMU wrote 2026_09_27_22_37_25-56R1.xml at T0 + 2031.87 (+52 s). The log is the
committed real-shaped fixture (sample_race.xml) re-pointed at this race; ingest is the
real ingest_all. Time is faked: no test really sleeps."""
import dataclasses
import os
import queue
import threading
import time
from pathlib import Path

import pytest

from simcoach.coach_tools import CoachContext
from simcoach.lmu import ingest
from simcoach.lmu.ingest import ingest_all
from simcoach.store import LapStore

SID = "grand-prix-of-long-beach--racing-team-turkey-by-tf-2026-34-lm--online"
T0 = 1_790_564_614.0
LAST = T0 + 1980.03          # the in-lap after the flag: the race window's end
LOGGED = T0 + 2031.87        # when LMU wrote the result log
_SAMPLE = Path(__file__).parent / "fixtures" / "lmu_results" / "sample_race.xml"
PROVISIONAL = "The result log isn't written yet -- this review is PROVISIONAL"


class _Clock:
    def __init__(self, now):
        self.now, self.slept = now, 0.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s
        self.slept += s


def _setup(tmp_path, monkeypatch, synthetic_lap, now, lmu_writes_at=LOGGED, **ctx_kw):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for dt, lap_no, partial, pos in [(1965.45, 22, None, 18), (1980.03, 22, "in", 17),
                                     (1883.44, 21, None, 18), (219.03, 1, None, 24)]:
        store.save(dataclasses.replace(
            synthetic_lap, session_id=SID, phase="race", lap_no=lap_no, partial=partial,
            position=pos, valid=partial is None, lap_time=None if partial else 82.0,
            timestamp=T0 + dt))
    results = tmp_path / "UserData" / "Log" / "Results"
    results.mkdir(parents=True)
    log = results / "2026_09_27_22_37_25-56R1.xml"
    xml = (_SAMPLE.read_text()
           .replace("Sebring International Raceway", synthetic_lap.track)
           .replace("Team WRT #41:LM", synthetic_lap.car)
           .replace("<DateTime>1750473029</DateTime>", f"<DateTime>{int(T0)}</DateTime>"))
    clock = _Clock(now)
    scans = []

    def scan():
        # The sidecar's poller wake, run inline: LMU has written the log once the clock
        # passes `lmu_writes_at`, then the real importer reads it.
        scans.append(clock.now)
        if lmu_writes_at is not None and clock.now >= lmu_writes_at and not log.exists():
            log.write_text(xml)
            os.utime(log, (lmu_writes_at, lmu_writes_at))
        ingest_all(store, userdata_dir=tmp_path / "UserData", player_names=["Jayson G"])

    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id=SID, on_track=False, scan_results=scan, **ctx_kw)
    return ctx, clock, scans, scan


def test_log_arrives_during_the_wait(tmp_path, monkeypatch, synthetic_lap):
    ctx, clock, _, _ = _setup(tmp_path, monkeypatch, synthetic_lap, now=LAST + 33)
    out = ctx.get_race_result()
    # Class result, grid slot and the car-to-car contact from the log.
    assert "P8 in class" in out and "P17 overall" in out
    assert "P4 in class" in out and "P9 overall" in out and "on the grid." in out
    assert "reported contact (42.27) with another vehicle Enrique Ojeda(12)" in out
    assert "This race is FINISHED" in out and PROVISIONAL not in out
    # Waited only until the log landed: LMU wrote it 18.8 s in, seen within two polls.
    assert LOGGED < clock.now <= LOGGED + 2.0
    assert clock.slept < 25


def test_log_never_arrives_the_wait_is_bounded(tmp_path, monkeypatch, synthetic_lap):
    ctx, clock, scans, _ = _setup(tmp_path, monkeypatch, synthetic_lap, now=LAST + 33,
                                  lmu_writes_at=None)
    out = ctx.get_race_result()
    assert clock.slept == pytest.approx(25.0)
    assert len(scans) == 25
    assert PROVISIONAL in out and "ask again in a minute" in out
    assert "on the grid" not in out and "Incidents" not in out
    assert "No race is live now" in out


def test_wait_ends_when_the_flag_is_no_longer_recent(tmp_path, monkeypatch, synthetic_lap):
    # 80 s after the last race lap only 10 s of the 90 s window remain.
    ctx, clock, _, _ = _setup(tmp_path, monkeypatch, synthetic_lap, now=LAST + 80,
                                  lmu_writes_at=None)
    assert PROVISIONAL in ctx.get_race_result()
    assert clock.slept == pytest.approx(10.0)


@pytest.mark.parametrize("now, kw", [
    (LAST + 600, {}),                                          # flag long ago
    (LAST + 33, {"on_track": True, "race_note": "Race live: no lap committed yet."}),
])
def test_no_wait_when_the_flag_is_old_or_the_race_is_live(tmp_path, monkeypatch,
                                                          synthetic_lap, now, kw):
    ctx, clock, scans, _ = _setup(tmp_path, monkeypatch, synthetic_lap, now=now)
    ctx.on_track = kw.get("on_track", False)
    ctx.race_note = kw.get("race_note")
    out = ctx.get_race_result()
    assert scans == [] and clock.slept == 0
    assert PROVISIONAL not in out and "on the grid" not in out


def test_no_wait_and_no_scan_when_the_log_is_already_in(tmp_path, monkeypatch, synthetic_lap):
    ctx, clock, scans, scan = _setup(tmp_path, monkeypatch, synthetic_lap, now=LOGGED + 5)
    scan()                           # the poller imported it before the question
    scans.clear()
    out = ctx.get_race_result()
    assert scans == [] and clock.slept == 0
    assert "P8 in class" in out and "reported contact" in out and PROVISIONAL not in out


def test_scan_now_wakes_the_sidecar_poller(monkeypatch):
    # What the sidecar wires as scan_results: the real poller, out of its 30 s sleep.
    calls, n = queue.Queue(), []

    def fake_ingest_all(store, **kw):
        n.append(1)
        calls.put(1)
        if len(n) > 1:
            raise SystemExit   # ends the poller thread quietly (not an Exception)
        return {"imported": 0}

    monkeypatch.setattr(ingest, "ingest_all", fake_ingest_all)
    threading.Thread(target=ingest.poll_results, args=(None,), daemon=True).start()
    calls.get(timeout=5)                      # the pass at start
    ingest.scan_now()
    calls.get(timeout=5)                      # woken, well inside POLL_INTERVAL_S
