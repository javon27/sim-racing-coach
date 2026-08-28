def test_replay_source_yields_saved(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    from simcoach.source import ReplaySource
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"d"))
    s.save(synthetic_lap)
    got = list(ReplaySource(s, "test_track", "test_car").laps())
    assert len(got) == 1 and got[0].track == "test_track"
