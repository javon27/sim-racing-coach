from dataclasses import dataclass
import numpy as np


@dataclass
class LapRecord:
    sim: str
    track: str
    car: str
    lap_time: float
    valid: bool
    timestamp: float
    channels: dict[str, np.ndarray]
