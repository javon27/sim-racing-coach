"""Rig 2026-10-31: in LMU's race-weekend menus the game runs but the shared memory keeps
the last session's frame -- the app read it as "on track" at Road Atlanta. LMU's REST
game state tells the menus (503, "Game is not in an active session") from a pause (a
vehicle loaded; the update counters freeze then too, so they can't), and every live
reader then reads no session."""
from simcoach.lmu import smm
from simcoach.lmu.rest import LmuRest

MENU = {"status": "unavailable", "reason": "Game is not in an active session", "gameState": "Setup"}
PAUSED = {"MultiStintState": "PAUSED", "playerVehicleLoaded": True, "inControlOfVehicle": True}


def test_rest_game_state_tells_the_menus_from_a_pause():
    assert LmuRest(fetch=lambda p: MENU).in_active_session() is False
    assert LmuRest(fetch=lambda p: PAUSED).in_active_session() is True
    assert LmuRest(fetch=lambda p: {"weird": 1}).in_active_session() is None

    def down(p):
        raise OSError("refused")
    assert LmuRest(fetch=down).in_active_session() is None


def test_session_left_only_when_rest_says_no_session(monkeypatch):
    for state, left in ((MENU, True), (PAUSED, False), (None, False)):
        monkeypatch.setattr(smm, "_rest_session_cache", [-smm._REST_TTL_S, None])
        monkeypatch.setattr("simcoach.lmu.rest.LmuRest.in_active_session",
                            lambda self, s=state: None if s is None else s is PAUSED)
        assert smm.session_left() is left, state      # unreachable REST stays live
