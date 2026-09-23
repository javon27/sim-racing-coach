# tests/test_next_corner.py
from simcoach.corners import Corner, next_corner


def _c(n, start, apex, end, direction, name):
    return Corner(number=n, name=name, direction=direction, start_dist=start,
                  apex_dist=apex, end_dist=end, label=str(n))


T1 = _c(1, 100.0, 150.0, 200.0, "right", "La Source")
T2 = _c(2, 900.0, 950.0, 1000.0, "left", "Raidillon")
T3 = _c(3, 3900.0, 3980.0, 50.0, "right", "Bus Stop")  # window wraps the S/F line


def test_mid_lap_returns_the_first_corner_ahead():
    assert next_corner([T1, T2], 500.0) is T2


def test_inside_a_corner_window_returns_that_corner():
    assert next_corner([T1, T2], 150.0) is T1
    assert next_corner([T1, T2], 200.0) is T1  # window is inclusive at the exit


def test_after_the_last_corner_wraps_to_the_first():
    assert next_corner([T1, T2], 3000.0) is T1


def test_between_the_last_corner_and_the_line_with_a_seam_corner():
    assert next_corner([T1, T2, T3], 2000.0) is T3
    assert next_corner([T1, T2, T3], 3950.0) is T3  # inside it, before the line
    assert next_corner([T1, T2, T3], 20.0) is T3    # inside it, past the line
    assert next_corner([T1, T2, T3], 60.0) is T1    # out of it: T1 is next


def test_input_order_does_not_matter():
    assert next_corner([T2, T1], 500.0) is T2
    assert next_corner([T2, T1], 3000.0) is T1


def test_negative_lap_distance_before_the_first_crossing():
    # rF2 reports a small negative lap distance on the grid before the line.
    assert next_corner([T1, T2], -30.0) is T1


def test_no_corners_is_none():
    assert next_corner([], 500.0) is None


def test_label_name_and_direction_come_from_the_map():
    c = next_corner([T1, T2], 500.0)
    assert (c.label, c.name, c.direction) == ("2", "Raidillon", "left")
