"""Parse LMU's `.xml` result logs to backfill session/lap history.

Player identification: `isPlayer` is set for every driver in MP result files
(useless). Instead, the result file names its own active profile via
`<PlayerFile>` -> `UserData/player/{PlayerFile}.JSON` -> its "Player Name"
field, matched against each `<Driver><Name>`. A config override
(`lmu_player_name`) short-circuits this lookup. Never raises -- degrades to
None on any missing file/field."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .. import config as _config


def player_name_for(result_root: ET.Element, player_dir: Path) -> str | None:
    try:
        override = _config.load_config().get("lmu_player_name")
        if override:
            return override
    except Exception:
        pass

    player_file = result_root.find(".//PlayerFile")
    if player_file is None or not (player_file.text or "").strip():
        return None
    try:
        profile = json.loads(
            (Path(player_dir) / f"{player_file.text.strip()}.JSON").read_text()
        )
        name = profile.get("Player Name")
        return name if name else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


@dataclass
class ResultLap:
    lap_no: int
    lap_time: float | None
    sectors: list[float]
    valid: bool


@dataclass
class ResultSession:
    sim: str
    track: str | None
    car: str | None
    car_class: str | None
    date_ts: int | None
    session_type: str | None
    position: int | None
    class_position: int | None
    best_lap: float | None
    finish_status: str | None
    laps: list[ResultLap]
    incidents: list[str]
    penalties: list[dict]


_SESSION_TAG_RE = re.compile(r"^(Practice|Qualify|Warmup|Race)\d*$")


def _to_float(text: str | None) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_int(text: str | None) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _find_session_elem(race_results: ET.Element) -> ET.Element | None:
    """The single Practice*/Qualify/Warmup/Race child holding this session's
    Stream + Drivers -- each result file covers exactly one session."""
    for child in race_results:
        if _SESSION_TAG_RE.match(child.tag):
            return child
    return None


def _find_player_driver(session_elem: ET.Element, player_name: str) -> ET.Element | None:
    target = player_name.strip().lower()
    for driver in session_elem.findall("Driver"):
        if (driver.findtext("Name") or "").strip().lower() == target:
            return driver
    return None


def _player_laps(driver: ET.Element) -> list[ResultLap]:
    laps = []
    for el in driver.findall("Lap"):
        lap_no = _to_int(el.get("num"))
        if lap_no is None:
            continue
        lap_time = _to_float(el.text)
        sectors = [
            s for s in (_to_float(el.get("s1")), _to_float(el.get("s2")), _to_float(el.get("s3")))
            if s is not None
        ]
        laps.append(ResultLap(lap_no=lap_no, lap_time=lap_time, sectors=sectors, valid=lap_time is not None))
    return laps


def _player_texts(stream: ET.Element | None, tag: str, player_name: str) -> list[str]:
    """<Incident>/<TrackLimits> lines naming the player -- either in the free
    text or (TrackLimits) the Driver="..." attribute."""
    if stream is None:
        return []
    needle = player_name.strip().lower()
    out = []
    for el in stream.findall(tag):
        text = (el.text or "").strip()
        driver_attr = (el.get("Driver") or "").strip().lower()
        if needle in text.lower() or needle == driver_attr:
            out.append(text)
    return out


def _player_penalties(stream: ET.Element | None, player_name: str) -> list[dict]:
    if stream is None:
        return []
    needle = player_name.strip().lower()
    return [
        {"text": (el.text or "").strip(), "et": el.get("et")}
        for el in stream.findall("Penalty")
        if needle in (el.text or "").lower()
    ]


def parse_result(path, player_name: str) -> ResultSession | None:
    """Parse one LMU result XML for `player_name`'s session/laps/incidents.
    None if the player isn't in the file, the session type can't be
    identified, or the XML is malformed -- this NEVER raises."""
    if not player_name:
        return None
    try:
        race_results = ET.parse(path).getroot().find("RaceResults")
        if race_results is None:
            return None
        session_elem = _find_session_elem(race_results)
        if session_elem is None:
            return None
        driver = _find_player_driver(session_elem, player_name)
        if driver is None:
            return None

        stream = session_elem.find("Stream")
        date_ts = _to_int(session_elem.findtext("DateTime")) or _to_int(race_results.findtext("DateTime"))

        return ResultSession(
            sim="lmu",
            track=race_results.findtext("TrackCourse") or race_results.findtext("TrackVenue"),
            car=driver.findtext("VehName"),
            car_class=driver.findtext("CarClass"),
            date_ts=date_ts,
            session_type=re.sub(r"\d+$", "", session_elem.tag).lower(),
            position=_to_int(driver.findtext("Position")),
            class_position=_to_int(driver.findtext("ClassPosition")),
            best_lap=_to_float(driver.findtext("BestLapTime")),
            finish_status=driver.findtext("FinishStatus"),
            laps=_player_laps(driver),
            incidents=_player_texts(stream, "Incident", player_name) + _player_texts(stream, "TrackLimits", player_name),
            penalties=_player_penalties(stream, player_name),
        )
    except Exception:
        return None
