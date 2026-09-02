import numpy as np
import pytest
from simcoach.model_types import LapRecord
import keyring as _keyring
from keyring.backend import KeyringBackend


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
        ch[f"brake_temp_{t}"] = 200 + 150 * (brake > 0)   # hotter under braking
        ch[f"grip_{t}"] = np.where(brake > 0, 0.9, 1.0)    # grip dips in the corners
        ch[f"tire_wear_{t}"] = np.linspace(1.0, 0.97, n)   # slight wear over the lap
    ch["fuel"] = np.linspace(80.0, 77.4, n)   # liters; 2.6 L consumed over the lap
    return LapRecord("lmu", "test_track", "test_car", 92.5, True, 0.0, ch)


class MemoryKeyring(KeyringBackend):
    priority = 1
    def __init__(self): self._store = {}
    def get_password(self, service, username): return self._store.get((service, username))
    def set_password(self, service, username, password): self._store[(service, username)] = password
    def delete_password(self, service, username): self._store.pop((service, username), None)


@pytest.fixture
def mem_keyring():
    prev = _keyring.get_keyring()
    kr = MemoryKeyring()
    _keyring.set_keyring(kr)
    yield kr
    _keyring.set_keyring(prev)
