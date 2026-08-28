from __future__ import annotations

import os
import sqlite3

import numpy as np

from simcoach.model_types import LapRecord

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    sim TEXT,
    track TEXT,
    car TEXT,
    lap_time REAL,
    valid INTEGER,
    telemetry_path TEXT
);
CREATE TABLE IF NOT EXISTS setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    track TEXT,
    car TEXT,
    params_json TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS corner_metrics (
    lap_id INTEGER,
    corner_no INTEGER,
    entry_spd REAL,
    apex_spd REAL,
    min_spd REAL,
    exit_spd REAL,
    brake_point REAL,
    throttle_point REAL,
    trailbrake_overlap REAL
);
"""


class LapStore:
    def __init__(self, db_path: str, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def save(self, record: LapRecord, metrics: list | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.timestamp, record.sim, record.track, record.car,
             record.lap_time, int(record.valid), None),
        )
        lap_id = cur.lastrowid
        telemetry_path = os.path.join(self.data_dir, f"{lap_id}.npz")
        np.savez_compressed(telemetry_path, **record.channels)
        self.conn.execute(
            "UPDATE laps SET telemetry_path = ? WHERE id = ?",
            (telemetry_path, lap_id),
        )
        if metrics:
            self.conn.executemany(
                "INSERT INTO corner_metrics (lap_id, corner_no, entry_spd, apex_spd, "
                "min_spd, exit_spd, brake_point, throttle_point, trailbrake_overlap) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (lap_id, m.corner_no, m.entry_spd, m.apex_spd, m.min_spd,
                     m.exit_spd, m.brake_point, m.throttle_point, m.trailbrake_overlap)
                    for m in metrics
                ],
            )
        self.conn.commit()
        return lap_id

    def load(self, lap_id: int) -> LapRecord:
        row = self.conn.execute(
            "SELECT ts, sim, track, car, lap_time, valid, telemetry_path "
            "FROM laps WHERE id = ?",
            (lap_id,),
        ).fetchone()
        ts, sim, track, car, lap_time, valid, telemetry_path = row
        with np.load(telemetry_path) as npz:
            channels = {k: npz[k] for k in npz.files}
        return LapRecord(sim, track, car, lap_time, bool(valid), ts, channels)

    def best_lap(self, track: str, car: str) -> LapRecord | None:
        row = self.conn.execute(
            "SELECT id FROM laps WHERE track = ? AND car = ? AND valid = 1 "
            "ORDER BY lap_time ASC LIMIT 1",
            (track, car),
        ).fetchone()
        if row is None:
            return None
        return self.load(row[0])
