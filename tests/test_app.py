"""REPL client state logic (simcoach/app.py). run_repl itself is an infinite
input() loop and not practically unit-testable without a live rig; the state
transition it depends on is pulled out as `_on_track_now` so it can be."""
from simcoach.app import _on_track_now


def test_on_track_true_while_peek_finds_a_live_session(synthetic_lap):
    assert _on_track_now(synthetic_lap, lambda: ("Le Mans", "Car")) is True


def test_on_track_clears_once_the_driver_leaves_track_even_with_a_past_lap(synthetic_lap):
    # Issue #6: a lap was captured earlier (record is not None) but the live peek
    # now misses (left the track / quit to menu / LMU closed) -- on_track must
    # follow the live peek, not stay stuck True from the one-time capture.
    assert _on_track_now(synthetic_lap, lambda: None) is False


def test_on_track_falls_back_to_record_when_no_peek_available():
    assert _on_track_now(None, None) is False
    assert _on_track_now("a lap record", None) is True
