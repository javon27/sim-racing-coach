import numpy as np

from simcoach.model_types import LapRecord


def test_save_load_and_best(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lid = s.save(synthetic_lap)
    back = s.load(lid)
    assert back.lap_time == synthetic_lap.lap_time
    assert np.allclose(back.channels["speed"], synthetic_lap.channels["speed"])
    slow = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 99.0})
    s.save(slow)
    assert s.best_lap("test_track", "test_car").lap_time == 92.5
