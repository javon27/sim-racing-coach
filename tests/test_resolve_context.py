"""resolve_context: the coaching context (map/car/note) must derive from the live
track/car, so an Algarve lap maps to Algarve — not a stale startup track."""
from simcoach.__main__ import TRACKS_DIR, resolve_context


def test_resolves_map_and_note_from_track_name():
    cmap, car, note = resolve_context(None, TRACKS_DIR, "Algarve International Circuit",
                                      "DKR Engineering #4:ELMS25")
    assert note == "algarve_international_circuit"
    assert cmap is not None and cmap.corners[0].name == "Primeira Curva"
    assert car  # resolve_car returned something


def test_different_track_gives_different_map():
    algarve, _, _ = resolve_context(None, TRACKS_DIR, "Algarve International Circuit", "x")
    silver, _, _ = resolve_context(None, TRACKS_DIR, "Silverstone Grand Prix Circuit - ELMS", "x")
    assert [c.name for c in algarve.corners] != [c.name for c in silver.corners]


def test_unknown_track_without_lap_yields_no_map():
    cmap, car, note = resolve_context(None, TRACKS_DIR, "Nonexistent Raceway 9000", "x")
    assert cmap is None            # no map, no channels to build one -> general guidance
    assert note == "nonexistent_raceway_9000"
