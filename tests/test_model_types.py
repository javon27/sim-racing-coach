def test_laprecord_channels_equal_length(synthetic_lap):
    n = len(synthetic_lap.channels["lap_distance"])
    assert n > 0
    assert all(len(v) == n for v in synthetic_lap.channels.values())
    d = synthetic_lap.channels["lap_distance"]
    assert (d[1:] >= d[:-1]).all()  # monotonic non-decreasing
