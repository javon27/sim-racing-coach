"""resolve_context: the coaching context (map/car/note) must derive from the live
track/car, so a track name maps to its own slug file — not a stale startup track.

Shipped track JSONs are user-owned map data (built/cleared in the editor), so these
tests use the synthetic `test_track.json` fixture and tmp fixtures, never real maps."""
import json

from simcoach.track_lookup import TRACKS_DIR, resolve_context


def test_resolves_map_and_note_from_track_name():
    # "Test Track" slugs to test_track -> the shipped fixture in TRACKS_DIR.
    cmap, car, note, car_class = resolve_context(None, TRACKS_DIR, "Test Track",
                                                 "DKR Engineering #4:ELMS25")
    assert note == "test_track"
    assert cmap is not None and len(cmap.corners) > 0
    assert car  # resolve_car returned something
    assert car_class is None   # no REST -> no carClass


def test_different_track_gives_different_map(tmp_path):
    # Two distinct slug files in a tmp tracks dir -> each name resolves to its own map.
    (tmp_path / "alpha.json").write_text(json.dumps({"corners": [
        {"number": 1, "name": "Alpha 1", "direction": "left",
         "start_dist": 0, "apex_dist": 50, "end_dist": 100}]}))
    (tmp_path / "beta.json").write_text(json.dumps({"corners": [
        {"number": 1, "name": "Beta 1", "direction": "right",
         "start_dist": 0, "apex_dist": 50, "end_dist": 100}]}))
    alpha, _, _, _ = resolve_context(None, str(tmp_path), "Alpha", "x")
    beta, _, _, _ = resolve_context(None, str(tmp_path), "Beta", "x")
    assert [c.name for c in alpha.corners] != [c.name for c in beta.corners]


def test_unknown_track_without_lap_yields_no_map():
    cmap, _car, note, _car_class = resolve_context(None, TRACKS_DIR, "Nonexistent Raceway 9000", "x")
    assert cmap is None            # no map, no channels to build one -> general guidance
    assert note == "nonexistent_raceway_9000"


def test_resolve_context_returns_rest_car_class():
    # #108 item 5: the class REST already looks up, returned rather than a
    # second REST call from the caller.
    class FakeRest:
        def get_setup(self):
            return {"carClass": "LMGT3"}
    _, _, _, car_class = resolve_context(FakeRest(), TRACKS_DIR, "Test Track", "x")
    assert car_class == "LMGT3"
