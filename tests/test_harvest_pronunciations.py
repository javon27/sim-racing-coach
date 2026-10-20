"""#197: the harvest script's vendored track-JSON writer must stay byte-identical to
the app's, or a --write would reformat a shipped map."""
from scripts.harvest_pronunciations import dump_track_json as harvest_dump
from simcoach.track_lookup import dump_track_json


def test_harvest_writer_matches_the_app_writer():
    data = {"name": "Circuit de Spa-Francorchamps", "corners": [{"name": "Eau Rouge", "n": 3}],
            "pronunciations": {"Blanchimont": "blˈɑ̃ʃimɔ̃"},
            "nested": {"x": [1.5, None, True]}}
    assert harvest_dump(data) == dump_track_json(data)
