"""#251 (Road Atlanta rig, 2026-10-27): a game-valid lap with a spin or a stop on track
counted as clean, so 'recent clean pace' read 1:34.7 on ~1:23 laps (race 2 lap 14:
three spins, stuck 9.0 + 8.3 s; lap 7: stopped 1.9 s at T10B after contact)."""
from simcoach.race import is_clean, lap_facts
from tests.test_issue_210_event_location import _SPIN, _STOP, _lap

_FLAT = (60.0, 0.0, 180.0, 180.0)


def test_a_valid_lap_with_a_spin_or_a_stop_is_not_clean():
    assert is_clean(_lap([_FLAT, _FLAT]))                       # the fixture's premise
    spun = _lap([_FLAT, _SPIN, (5.0, 0.0, 30.0, 180.0), _FLAT])
    stopped = _lap([_FLAT, *_STOP, _FLAT])
    for rec in (spun, stopped):
        assert rec.valid and rec.lap_time                       # the game kept it
        assert not is_clean(rec)
        assert not lap_facts(rec).clean


def test_contact_alone_keeps_a_lap_clean():
    # #251 review: most contact laps ran at pace (7801 55.255 s vs a 55.586 s pace);
    # dropping them starved pace and strategy for whole race openings.
    hit = _lap([_FLAT, _FLAT], hit_at_m=1500.0)
    from simcoach.analysis import damage_events
    assert damage_events(hit.channels)                          # the fixture's premise
    assert is_clean(hit)


def test_a_track_limits_strike_drops_the_lap():
    lap = _lap([_FLAT, _FLAT])
    n = len(lap.channels["lap_distance"])
    steps = [0.0] * (n // 2) + [1.0] * (n - n // 2)          # mTrackLimitsSteps rises once
    struck = _lap([_FLAT, _FLAT])
    struck.channels["track_limits_steps"] = steps
    assert is_clean(lap) and not is_clean(struck)
