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
PROVISIONAL = "The result log isn't imported yet -- this review is PROVISIONAL"


class _Clock:
    def __init__(self, now):
        self.now, self.slept = now, 0.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s
        self.slept += s


def _save_laps(store, synthetic_lap, t0=T0, positions=True):
    for dt, lap_no, partial, pos in [(1965.45, 22, None, 18), (1980.03, 22, "in", 17),
                                     (1883.44, 21, None, 18)]:
        store.save(dataclasses.replace(
            synthetic_lap, session_id=SID, phase="race", lap_no=lap_no, partial=partial,
            position=pos if positions else None, valid=partial is None,
            lap_time=None if partial else 82.0, timestamp=t0 + dt))


def _setup(tmp_path, monkeypatch, synthetic_lap, now, lmu_writes_at=LOGGED, positions=True,
           **ctx_kw):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _save_laps(store, synthetic_lap, positions=positions)
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
    assert "contact with Eli Overtake (another car): 1 report (1 logged by you" in out
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
    assert PROVISIONAL in out and "LMU writes the log when the driver leaves the session" in out
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


def test_one_wait_per_turn(tmp_path, monkeypatch, synthetic_lap):
    # The model re-calling get_race_result in one turn (a race=1 follow-up, a retry)
    # must not block the driver 25 s each time; the row is still re-checked.
    ctx, clock, scans, _ = _setup(tmp_path, monkeypatch, synthetic_lap, now=LAST + 10,
                                  lmu_writes_at=None)
    assert PROVISIONAL in ctx.get_race_result()
    assert PROVISIONAL in ctx.get_race_result(race=1)
    assert PROVISIONAL in ctx.get_race_result()
    assert clock.slept == pytest.approx(25.0) and len(scans) == 25


def test_no_wait_when_the_race_has_no_positions(tmp_path, monkeypatch, synthetic_lap):
    ctx, clock, scans, _ = _setup(tmp_path, monkeypatch, synthetic_lap, now=LAST + 33,
                                  lmu_writes_at=None, positions=False)
    out = ctx.get_race_result()
    assert "No race result recorded" in out and PROVISIONAL not in out
    assert scans == [] and clock.slept == 0


def test_no_wait_and_no_scan_when_the_log_is_already_in(tmp_path, monkeypatch, synthetic_lap):
    ctx, clock, scans, scan = _setup(tmp_path, monkeypatch, synthetic_lap, now=LOGGED + 5)
    scan()                           # the poller imported it before the question
    scans.clear()
    out = ctx.get_race_result()
    assert scans == [] and clock.slept == 0
    assert "P8 in class" in out and "contact with Eli Overtake" in out and PROVISIONAL not in out


class _Stop(BaseException):
    """Not an Exception, so poll_results' retry doesn't swallow it."""


def test_scan_now_wakes_the_sidecar_poller(monkeypatch):
    # What the sidecar wires as scan_results: the real poller, out of its 30 s sleep.
    calls, n = queue.Queue(), []

    def fake_ingest_all(store, **kw):
        n.append(1)
        calls.put(1)
        if len(n) > 1:
            raise _Stop   # the second pass: end the poller thread
        return {"imported": 0}

    def poller():
        try:
            ingest.poll_results(None)
        except _Stop:
            pass

    monkeypatch.setattr(ingest, "ingest_all", fake_ingest_all)
    threading.Thread(target=poller, daemon=True).start()
    calls.get(timeout=5)                      # the pass at start
    ingest.scan_now()
    calls.get(timeout=5)                      # woken, well inside POLL_INTERVAL_S


def test_the_sidecar_coach_turn_wakes_the_importer(tmp_path, monkeypatch, synthetic_lap):
    """The production wiring: coach_ws hands CoachContext app.state.scan_results (main()
    sets it to ingest.scan_now), and a turn that calls get_race_result right after the
    flag, off track, triggers it. The spy stands in for the poller importing the log."""
    from fastapi.testclient import TestClient

    from simcoach import coach_tools
    from simcoach.coach import Coach
    from simcoach.lmu.results import parse_result
    from simcoach.model.fake import FakeModel
    from simcoach.server.app import create_app

    monkeypatch.setattr(coach_tools, "_RESULT_LOG_POLL_S", 0.01)
    t0 = time.time() - 2000.0          # the in-lap committed 20 s ago
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _save_laps(store, synthetic_lap, t0=t0)
    xml = tmp_path / "log.xml"
    xml.write_text(_SAMPLE.read_text()
                   .replace("Sebring International Raceway", synthetic_lap.track)
                   .replace("Team WRT #41:LM", synthetic_lap.car)
                   .replace("<DateTime>1750473029</DateTime>", f"<DateTime>{int(t0)}</DateTime>"))
    scans = []

    def spy():
        scans.append(1)
        store.save_result_session(parse_result(xml, {"jayson g"}), session_id="log",
                                  logged_ts=time.time())

    fake = FakeModel(reply="ok", tool_script=[("get_race_result", {})])
    app = create_app(store, Coach(fake))
    app.state.scan_results = spy
    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "how was that race?", "session_id": SID})
        while ws.receive_json()["type"] not in ("done", "error"):
            pass
    assert scans == [1]
    name, _, out = fake.dispatched[0]
    assert name == "get_race_result"
    assert "P4 in class" in out and "contact with Eli Overtake" in out and PROVISIONAL not in out
