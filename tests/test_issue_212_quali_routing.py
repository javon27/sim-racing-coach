"""#212: a qualy question at the qualy -> race boundary was routed as race_review
(race ack + router note), and get_stint_events had no current-phase scope.

The shape is the real Long Beach sitting (rig 2026-09-27, server.log ~44803-44930):
practice out + lap 2, qualy out + laps 2-6; at 22:03:34 LMU flipped the live phase to
race, the question landed at 22:03:38 with no race lap stored, and race lap 0 (an
in-lap) was stored at 22:03:41 -- before the stint scan ran."""
import dataclasses
import logging

import pytest

from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.store import LapStore
from simcoach.thinking import ack_line, race_reviewable, route

SID = "s1"
T0 = 1_790_000_000.0
# (phase, lap_no, partial, lap_time, ts offset) -- the real rows 7730-7738
LONG_BEACH = [
    ("practice", 1, "out", None, 0),
    ("practice", 2, None, 84.44, 85),
    ("qualifying", 1, "partial", None, 323),
    ("qualifying", 2, None, 92.94, 416),
    ("qualifying", 3, None, 83.01, 499),
    ("qualifying", 4, None, 82.21, 581),
    ("qualifying", 5, None, 106.62, 688),
    ("qualifying", 6, None, 81.64, 769),
]
RACE_LAP_0 = ("race", 0, "in", None, 830)       # row 7739, stored after the question
RACE_LAP_1 = ("race", 1, None, 98.16, 1042)     # row 7741: the first lap from the green
RACE_ACK = ack_line("race_review")
CLASSIFIED = {"tier": "deep", "intent": "race_review", "corner": None, "lap": None,
              "rival": None, "wants_detail": True}


def _store(tmp_path, synthetic_lap, shape, flat_spot=()):
    """`flat_spot`: ts offsets of laps given a 9-point FL wear step at 1000 m (the
    #180 fixture's real incident) -- every other lap wears evenly."""
    from tests.test_issue_180_race_scope import _wear_step
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for phase, lap_no, partial, t, dt in shape:
        lap = _wear_step(synthetic_lap, "fl") if dt in flat_spot else synthetic_lap
        store.save(dataclasses.replace(
            lap, session_id=SID, phase=phase, lap_no=lap_no, partial=partial,
            lap_time=t, valid=t is not None, timestamp=T0 + dt))
    return store


def _reviewable(store, live_phase="race"):
    return race_reviewable(store.race_windows(SID), store.session_laps(SID), live_phase)


# --- the gate ------------------------------------------------------------------------

@pytest.mark.parametrize("extra, live_phase, ok", [
    ([], "race", False),                        # the question: live race, no race lap
    ([RACE_LAP_0], "race", False),              # race lap 0 only: not from the green
    ([("race", 0, None, 120.0, 900)], "race", False),   # even a complete lap 0
    ([RACE_LAP_0, RACE_LAP_1], "race", True),   # race lap 1 complete
    ([RACE_LAP_0, RACE_LAP_1], None, True),     # left the session after the race
    ([RACE_LAP_0, ("race", 1, "partial", None, 1042)], "race", False),  # lap 1 not done
    ([RACE_LAP_0, RACE_LAP_1], "practice", False),   # a practice session is live now
])
def test_race_review_needs_a_completed_race_lap(tmp_path, synthetic_lap, extra,
                                                live_phase, ok):
    assert _reviewable(_store(tmp_path, synthetic_lap, LONG_BEACH + extra),
                       live_phase) is ok


R1 = [RACE_LAP_0, RACE_LAP_1, ("race", 2, None, 83.0, 1130)]
Q2 = [("qualifying", 1, "out", None, 3000), ("qualifying", 2, None, 82.0, 3090)]


@pytest.mark.parametrize("extra, ok", [
    ([], False),     # race 2's quali -> race boundary: race 1 is over, race 2 has no lap
    ([("race", 0, "out", None, 3300)], False),
    ([("race", 0, "out", None, 3300), ("race", 1, None, 97.0, 3400)], True),
])
def test_race_review_at_the_second_race_boundary(tmp_path, synthetic_lap, extra, ok):
    store = _store(tmp_path, synthetic_lap, LONG_BEACH + R1 + Q2 + extra)
    assert _reviewable(store, "race") is ok


def test_route_downgrades_race_review_to_a_session_review():
    clf = FakeModel(classify_result=CLASSIFIED)
    q = "All right, I got a few laps down. How am I doing?"
    tier, intent, hint, _, via = route(q, classifier=lambda: clf, race_reviewable=False)
    assert (tier, intent, via["via"]) == ("deep", "session_review", "classifier")
    assert ack_line(intent) != RACE_ACK and "race" not in ack_line(intent)
    assert "question type: session review" in hint and "race" not in hint
    tier, intent, hint, _, _ = route(q, classifier=lambda: clf, race_reviewable=True)
    assert (tier, intent) == ("deep", "race_review")
    assert "question type: race review" in hint


# --- the boundary, replayed over the websocket ---------------------------------------

def _app(tmp_path, monkeypatch, synthetic_lap, shape):
    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.lmu import live as lmu_live
    from simcoach.lmu import smm
    from simcoach.server.app import create_app
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("test_track", "test_car"))
    monkeypatch.setattr(smm, "read_live_session_id", lambda: SID)
    monkeypatch.setattr(smm, "read_live_status", lambda: {
        "track": "test_track", "car": "test_car", "state": "garage", "speed": 0.0})
    field = lmu_live.empty_snapshot()
    field = dataclasses.replace(field, session=dataclasses.replace(
        field.session, live=True, phase="race", current_lap=0))
    monkeypatch.setattr(lmu_live, "read_field_snapshot", lambda: field)
    store = _store(tmp_path, synthetic_lap, shape)
    model = FakeModel(reply="Good pace.")
    coach = Coach(model, classifier_for=lambda name: FakeModel(classify_result=CLASSIFIED))
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1"), model


def _ask(client, question):
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": question, "session_id": SID, "voice": "ptt"})
        frames = []
        while frames[-1:] == [] or frames[-1]["type"] not in ("done", "error"):
            frames.append(ws.receive_json())
    return frames


@pytest.mark.parametrize("extra, race_ack", [
    ([], False),                         # the rig case: quali done, no race lap stored
    ([RACE_LAP_0], False),               # race lap 0 stored: still not a race
    ([RACE_LAP_0, RACE_LAP_1], True),    # a completed race lap: the race review stands
])
def test_ws_boundary_question_gets_no_race_review(tmp_path, monkeypatch, mem_keyring,
                                                  synthetic_lap, caplog, extra, race_ack):
    c, model = _app(tmp_path, monkeypatch, synthetic_lap, LONG_BEACH + extra)
    with caplog.at_level(logging.INFO, logger="simcoach.thinking"):
        frames = _ask(c, "All right, I got a few laps down. How am I doing?")
    assert frames[-1]["type"] == "done" and model.thinking == ["deep"]
    assert (frames[0] == {"type": "ack", "text": RACE_ACK}) is race_ack
    sent = model.last_messages[-1]["content"]
    assert ("question type: race review" in sent) is race_ack
    assert ("question type: session review" in sent) is not race_ack
    # the classifier's state line is logged with the routing line
    line = next(r.getMessage() for r in caplog.records
                if "coach thinking" in r.getMessage())
    assert "state='garage, stationary; session phase race;" in line
    assert f"intent={'race_review' if race_ack else 'session_review'}" in line


def test_router_log_bounds_the_state(caplog):
    with caplog.at_level(logging.INFO, logger="simcoach.thinking"):
        route("tyres?", state="x" * 5000)
    line = next(r.getMessage() for r in caplog.records if "coach thinking" in r.getMessage())
    assert "x" * 300 in line and "x" * 301 not in line


# --- get_stint_events' phase scope ---------------------------------------------------

# What coach_ws hands CoachContext: resolve_car's display model, not the stored entry
# name ('Racing Team Turkey by TF 2026 #34:LM') -- a car-matched query finds nothing.
DISPLAY_CAR = "Chevrolet Corvette Z06 LMGT3.R"


def _ctx(store, synthetic_lap):
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=synthetic_lap.track, car=DISPLAY_CAR, session_id=SID)


def _laps(out):
    return [line.split(" (game lap")[0].removeprefix("Lap: ")
            for line in out.splitlines() if line.startswith("Lap: ")]


QUALY = [f"qualifying lap {n}" for n in range(1, 7)]


@pytest.mark.parametrize("extra, tail", [([], []), ([RACE_LAP_0], ["race lap 0"])])
def test_stint_events_default_to_the_current_phase(tmp_path, synthetic_lap, extra, tail):
    """In qualy -- and at the boundary, race lap 0 stored but no race lap complete --
    the unscoped scan is the qualy laps (no practice lap) plus anything stored since:
    race lap 0, labelled not racing."""
    ctx = _ctx(_store(tmp_path, synthetic_lap, LONG_BEACH + extra), synthetic_lap)
    out = ctx.dispatch("get_stint_events", {})
    assert _laps(out) == QUALY + tail
    assert out.startswith("Scanned: the qualifying laps (the latest completed lap's "
                          "phase) + anything stored since.")


def test_stint_events_default_keeps_a_newer_lap_with_its_incident(tmp_path, synthetic_lap):
    """Red team #212: a practice partial after the race (a spin lap at the rig: 7447,
    7690) is newer than the latest complete lap -- the default scan must report it."""
    spin = ("practice", 1, "partial", None, 3000)
    store = _store(tmp_path, synthetic_lap, LONG_BEACH + R1 + [spin], flat_spot=(3000,))
    out = _ctx(store, synthetic_lap).dispatch("get_stint_events", {})
    assert _laps(out) == ["race lap 0", "race lap 1", "race lap 2", "practice lap 1"]
    tail = out.split("Lap: practice lap 1")[1]
    assert "FL wear step" in tail and "FL wear step" not in out.split("Lap: practice")[0]


def test_stint_events_flat_spot_on_a_short_scoped_run(tmp_path, synthetic_lap):
    """A 2-lap run's own median holds the step lap: the baseline falls back to the
    session's complete laps -- matched by session id, not by ctx.car (a display name
    in the app, never the stored entry name) -- so the flat spot is still flagged."""
    store = _store(tmp_path, synthetic_lap, LONG_BEACH, flat_spot=(85,))   # practice lap 2
    out = _ctx(store, synthetic_lap).dispatch("get_stint_events", {"phase": "practice"})
    assert _laps(out) == ["practice lap 1", "practice lap 2"]
    assert "FL wear step" in out.split("Lap: practice lap 2")[1]


def test_stint_events_phase_is_case_folded_and_checked(tmp_path, synthetic_lap):
    ctx = _ctx(_store(tmp_path, synthetic_lap, LONG_BEACH), synthetic_lap)
    assert ctx.dispatch("get_stint_events", {"phase": "Practice"}) == ctx.dispatch(
        "get_stint_events", {"phase": "practice"})
    assert ctx.dispatch("get_stint_events", {"phase": "qualy"}) == (
        "phase must be one of practice, qualifying, warmup, race -- not 'qualy'.")


def test_stint_events_explicit_phase(tmp_path, synthetic_lap):
    store = _store(tmp_path, synthetic_lap, LONG_BEACH + [RACE_LAP_0, RACE_LAP_1])
    ctx = _ctx(store, synthetic_lap)
    # in the race now: the default is the race, lap 0 included (not racing, labelled)
    assert _laps(ctx.dispatch("get_stint_events", {})) == ["race lap 0", "race lap 1"]
    assert _laps(ctx.dispatch("get_stint_events", {"phase": "practice"})) == [
        "practice lap 1", "practice lap 2"]
    assert _laps(ctx.dispatch("get_stint_events", {"phase": "qualifying"})) == QUALY
    assert _laps(ctx.dispatch("get_stint_events", {"phase": "race"})) == ["race lap 1"]
    assert ctx.dispatch("get_stint_events", {"phase": "warmup"}).startswith(
        "No warmup laps in this session")


def test_stint_events_read_the_latest_run_of_a_phase(tmp_path, synthetic_lap):
    """One session id spans the evening: practice after the race is a new run."""
    later = [("practice", 1, "out", None, 2000), ("practice", 2, None, 81.67, 2082)]
    ctx = _ctx(_store(tmp_path, synthetic_lap,
                      LONG_BEACH + [RACE_LAP_0, RACE_LAP_1] + later), synthetic_lap)
    want = ["practice lap 1", "practice lap 2"]
    assert _laps(ctx.dispatch("get_stint_events", {})) == want
    assert _laps(ctx.dispatch("get_stint_events", {"phase": "practice"})) == want


def test_stint_events_race_default_is_the_current_race(tmp_path, synthetic_lap):
    """Two races back to back, no lap between: one run of race laps, two races
    (#204's lap-number reset) -- the default scan is the current race only."""
    races = [("race", 1, None, 98.0, 1000), ("race", 2, None, 83.0, 1083),
             ("race", 0, "out", None, 1400), ("race", 1, None, 97.0, 1500)]
    ctx = _ctx(_store(tmp_path, synthetic_lap, LONG_BEACH + races), synthetic_lap)
    assert _laps(ctx.dispatch("get_stint_events", {})) == ["race 2 lap 0", "race 2 lap 1"]
