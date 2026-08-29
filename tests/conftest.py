import numpy as np
import pytest
from simcoach.model_types import LapRecord


@pytest.fixture
def synthetic_lap():
    n = 1000
    dist = np.linspace(0, 3000, n)
    speed = np.full(n, 60.0)
    brake = np.zeros(n)
    throttle = np.ones(n)
    steer = np.zeros(n)
    left = (dist > 800) & (dist < 1200)     # left corner
    right = (dist > 2000) & (dist < 2400)   # right corner
    for mask, sign in ((left, -1.0), (right, 1.0)):
        brake[mask] = 0.8
        throttle[mask] = 0.1
        steer[mask] = sign * 0.5
        speed[mask] = 30.0
    lat_g = steer * 2.0
    ch = {
        "lap_distance": dist,
        "time": dist / 40.0,
        "speed": speed,
        "throttle": throttle,
        "brake": brake,
        "steering": steer,
        "lat_g": lat_g,
    }
    for t in ("fl", "fr", "rl", "rr"):
        ch[f"tire_temp_{t}"] = 80 + 5 * (brake > 0)
        ch[f"tire_press_{t}"] = np.full(n, 27.5)
    ch["fuel"] = np.linspace(80.0, 77.4, n)   # liters; 2.6 L consumed over the lap
    return LapRecord("lmu", "test_track", "test_car", 92.5, True, 0.0, ch)
