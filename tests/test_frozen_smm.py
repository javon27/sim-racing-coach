"""Rig 2026-10-31: in LMU's race-weekend menus the game runs but the plugin stops
writing -- the buffer keeps the last session's frame, frozen, and the app read it as
"on track" at Road Atlanta. A scoring update counter that hasn't moved for
_FROZEN_S is a dead buffer: every live reader reads it as no session."""
from types import SimpleNamespace

from simcoach.lmu import smm


def test_a_scoring_counter_that_stops_moving_reads_as_frozen(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(smm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(smm, "_scoring_seen", [None, 0.0])
    frame = SimpleNamespace(mVersionUpdateBegin=1065)
    assert smm.scoring_frozen(frame) is False                 # first sight: assume live
    clock[0] += smm._FROZEN_S - 0.5
    assert smm.scoring_frozen(frame) is False                 # not long enough yet
    clock[0] += 1.0
    assert smm.scoring_frozen(frame) is True                  # frozen past the window
    assert smm.scoring_frozen(SimpleNamespace(mVersionUpdateBegin=1066)) is False  # moving
