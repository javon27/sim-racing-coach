"""#160: the lap in progress, readable corner by corner before the line.

The coach only saw COMPLETED laps, so a spin on the lap being driven was invisible
("You need to be able to read current lap incidents"). LmuSource publishes the
in-progress lap's own buffer (`source.live_lap`, set once per lap -- nothing is copied
per poll); a single worker thread (`LiveLap.run`) looks at it twice a second and, for
each corner the car has left, runs the SAME per-corner analysis a stored lap gets
(`_metrics_for_corner`, `lap_events`, `damage_events`) and stores it as provisional
(store.live_corners). Incidents are ALSO scanned over the whole buffer (spins and hits
happen between corner windows too) into one lap-wide row, and again at question time.
Saving the lap deletes its rows; a lap that never commits (crash, retirement) keeps
them. Capture never waits on or sees this worker.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import time
from types import SimpleNamespace

import numpy as np

from .analysis import (
    _damage_label,
    _is_impact,
    _metrics_for_corner,
    damage_events,
    damage_state,
    lap_events,
    prev_corner_end,
    tag_sections,
)
from .corners import in_window
from .format import SUSPENSION_UNSEEN, format_events, place_text
from .model_types import LapRecord

# ponytail: a flat 0.5 s look, so a corner lands at most ~0.5 s after its window end.
_TICK_S = 0.5
# Provisional laps older than this (vs the lap now starting) are deleted -- a crash is
# still readable the same evening, and the table stays bounded.
_KEEP_S = 24 * 3600
# ponytail: how far back a hit taken just before the line still counts as "just now"
# right after it. A guess; widen it if a rig answer misses one.
_RECENT_HIT_S = 15.0
# The channels the lap-wide incident scan reads (spins: path + time; hits: impacts),
# so it never converts the whole ~110-channel buffer.
_INCIDENT_KEYS = ("lap_distance", "speed", "pos_x", "pos_z", "time", "elapsed_time",
                  "lap_start_et", "last_impact_et", "last_impact_mag", "last_impact_x",
                  "last_impact_z", "track_limits_steps")


def _snapshot(channels: dict, keys=None) -> dict[str, np.ndarray]:
    """The live buffer (or `keys` of it) as arrays, every channel cut to the same
    sample count (capture may be mid-append across channels while this reads)."""
    items = [(k, v) for k, v in list(channels.items()) if keys is None or k in keys]
    if not items:
        return {}
    n = min(len(v) for _, v in items)
    return {k: np.asarray(v[:n], dtype=float) for k, v in items}


def _mixed(channels: dict) -> bool:
    """True when the game clock steps BACKWARDS inside the buffer: a new session
    started with no lap wrap (menu -> next session), so the buffer holds two sessions'
    data under the first one's lap meta. Nothing from it is analysed or reported."""
    et = channels.get("elapsed_time")
    if et is None or len(et) < 2:
        return False
    d = np.diff(np.asarray(et[:], dtype=float))
    return bool((d[np.isfinite(d)] < -1.0).any())


def driven_corners(cmap, dist) -> list:
    """Corners this buffer drove all the way through: entered from before the window
    start and now past its end. ponytail: a window straddling S/F is left to the lap
    commit, which covers it; so is a corner an out-lap joined partway through."""
    d = dist[np.isfinite(dist)]
    if d.size < 2:
        return []
    return [c for c in cmap.corners
            if c.start_dist <= c.end_dist and d[0] <= c.start_dist and d.max() > c.end_dist]


def impact_text(e: dict, cmap, now_et: float | None = None) -> str:
    """'impact 365 at 1,004 m (T1 Left Hairpin), front-left, 12 s ago'. Magnitude is
    the game's raw, unitless number (#143) -- never tiered."""
    ago = ""
    if now_et is not None and math.isfinite(now_et) and now_et >= e["et"]:
        ago = f", {now_et - e['et']:.0f} s ago"
    return (f"impact {e['magnitude']:.0f} at {place_text(cmap, e['lap_distance'])}, "
            f"{e['label']}{ago}")


def incident_lines(incidents: dict, cmap, units: str = "metric") -> list[str]:
    """Lap-wide scan -> driver lines; an incident between windows is placed by
    distance and its neighbouring corners (`format_events` places every spin)."""
    return (format_events(incidents.get("events", []), cmap, units)
            + [impact_text(e, cmap) for e in incidents.get("impacts", [])])


def lap_incidents(channels: dict, cmap) -> dict:
    """Spins and hits over the WHOLE buffer so far (not just corner windows), and how
    far it reached -- the only basis on which "no incidents" may be said."""
    snap = _snapshot(channels, _INCIDENT_KEYS)
    d = snap.get("lap_distance", np.array([]))
    fin = d[np.isfinite(d)]
    events = []
    if "speed" in snap:
        rec = LapRecord("lmu", "", "", None, False, 0.0, snap, partial="partial")
        events = lap_events(rec, cmap, None, live=True)   # no analysis: spins, stops
    return {"events": events, "impacts": damage_events(snap),
            "scanned_to": float(fin.max()) if fin.size else None}


def car_status_text(meta: dict, ch: dict, state: dict | None, cmap) -> str:
    """The car right now, from the lap-in-progress buffer (its last sample is one poll
    old) plus the live pit/garage state. Only what the game reports; nothing inferred.

    A NEGATIVE ("no impacts", dent 0) is stated only for channels rig-confirmed live
    and populated (#143, Imola 2026-09-20): mLastImpact* and mDentSeverity. The
    bodywork-detached, overheating, flat and wheel-detached flags have never been seen
    set at the rig, so only a SET flag is reported, marked unverified -- an unset one
    proves nothing and is not stated."""
    phase = meta.get("phase") or "session"
    lines = [f"Car status now (live), game lap {meta['lap_no']} in progress ({phase}):"]
    where = {"track": "On track.", "pits": "In the pit lane.",
             "garage": "In the garage."}.get((state or {}).get("state"))
    if where:
        lines.append(where)
    if "last_impact_et" not in ch:
        lines.append("No damage data in this capture.")
        return "\n".join(lines)
    et = ch.get("elapsed_time")
    now_et = float(et[-1]) if et is not None and len(et) else None
    impacts = damage_events(ch)
    if impacts:
        lines.append(f"Impacts since the start of this lap ({len(impacts)}):")
        lines.extend(f"  {impact_text(e, cmap, now_et)}" for e in impacts)
    else:
        lines.append("No impacts since the start of this lap.")
    # A hit in the last seconds of the lap just completed sits in this buffer's FIRST
    # sample (the field holds the last impact), with no rising edge to find.
    et0 = float(ch["last_impact_et"][0]) if len(ch["last_impact_et"]) else math.nan
    if (now_et is not None and math.isfinite(et0) and et0 > 0
            and 0 <= now_et - et0 <= _RECENT_HIT_S):
        x0, z0 = float(ch["last_impact_x"][0]), float(ch["last_impact_z"][0])
        mag0 = float(ch["last_impact_mag"][0])
        if _is_impact(mag0, x0, z0):
            lines.append(f"Last impact {now_et - et0:.0f} s ago, on the lap just "
                         f"completed: impact {mag0:.0f}, {_damage_label(x0, z0)}.")
    st = damage_state(ch)
    if st:
        lines.append(f"Bodywork: dent severity max {st['dent_max']:.0f}, total "
                     f"{st['dent_total']:.0f} (raw 0\N{EN DASH}255 scale, panel unknown).")
        lines.append(f"Note: {SUSPENSION_UNSEEN}.")
        flags = (["bodywork part detached"] if st["detached"] else []) \
            + (["engine overheating"] if st["overheating"] else []) \
            + [f"{w.upper()} flat" for w in st["wheels_flat"]] \
            + [f"{w.upper()} wheel detached" for w in st["wheels_detached"]]
        if flags:
            lines.append("Flags set: " + ", ".join(f"{f} (unverified channel)"
                                                   for f in flags) + ".")
    return "\n".join(lines)


class LiveLap:
    """The #160 worker. `source` is set by _capture_loop; `cmap_for(track)` resolves
    the corner map every look (the server's own cache honours a saved/edited map)."""

    def __init__(self, store, cmap_for):
        self.store = store
        self.cmap_for = cmap_for
        self.source = None
        self._key: tuple | None = None
        self._done: set[str] = set()
        self._scanned = 0

    def _live(self):
        live = getattr(self.source, "live_lap", None)
        return live if live is not None else (None, None)

    def tick(self) -> None:
        """Store every corner the lap in progress has newly completed, and refresh the
        lap-wide incident scan."""
        meta, ch = self._live()
        if meta is None:
            return
        key = (meta["session_id"], meta["lap_no"], meta["start_ts"])
        if key != self._key:
            # A new lap: forget the last one's corners; bound the table (indexed).
            self._key, self._done, self._scanned = key, set(), 0
            self.store.age_out_live_corners(meta["start_ts"] - _KEEP_S)
        cmap = self.cmap_for(meta["track"])
        if cmap is None or _mixed(ch):
            return
        dist = np.asarray(ch.get("lap_distance", [])[:], dtype=float)
        if dist.size > self._scanned:
            self._scanned = dist.size
            self.store.save_live_corner(meta, "_lap", None, lap_incidents(ch, cmap))
        todo = [c for c in driven_corners(cmap, dist)
                if (c.label or str(c.number)) not in self._done]
        if not todo:
            return
        snap = _snapshot(ch)
        record = LapRecord("lmu", meta["track"], meta["car"], None, False,
                           meta["start_ts"], snap, phase=meta["phase"],
                           session_id=meta["session_id"], lap_no=meta["lap_no"],
                           partial="partial")
        rpm = snap.get("rpm")
        rpm_max = float(np.nanmax(rpm)) if rpm is not None and np.isfinite(rpm).any() else None
        cms = [_metrics_for_corner(record, c, cmap.length, None, rpm_max, prev_corner_end(cmap, c))
               for c in todo]
        tag_sections(cms, cmap)
        for cm in cms:
            # Judged against the lap's peak RPM -- mid-lap only the peak SO FAR, so a
            # live read can be a false negative. Stated once the lap commits.
            cm.short_shift = cm.bog = None
        events = lap_events(record, cmap, SimpleNamespace(corners=cms), live=True)
        impacts = damage_events(snap)
        for c, cm in zip(todo, cms):
            label = c.label or str(c.number)
            self.store.save_live_corner(meta, label, c.number, {
                "metrics": dataclasses.asdict(cm),
                "events": [e for e in events if e["corner_no"] == c.number],
                "impacts": [e for e in impacts
                            if in_window(e["lap_distance"], c.start_dist, c.end_dist)],
            })
            self._done.add(label)

    def step(self) -> None:
        """tick(), never raising: a failure costs a provisional corner, not capture."""
        try:
            self.tick()
        except Exception:
            logging.getLogger(__name__).warning("live corner not written", exc_info=True)

    def run(self) -> None:
        while True:
            self.step()
            time.sleep(_TICK_S)

    def now(self, state: dict | None, live: tuple | None = None,
            session_id: str | None = None) -> tuple[dict | None, str | None]:
        """(in-progress lap meta + its whole-buffer incident scan, car status text) for
        a coach turn, or (None, None) when there is no lap in progress -- or the buffer
        isn't the live session's: `live` is the live (track, car), `session_id` the live
        base session id; a buffer left from the previous session never answers."""
        meta, ch = self._live()
        if meta is None or _mixed(ch):
            return None, None
        if live is not None and (meta["track"].strip(), meta["car"].strip()) != tuple(live):
            return None, None
        if session_id is not None and meta["session_id"] != session_id:
            return None, None
        cmap = self.cmap_for(meta["track"])
        return (dict(meta, incidents=lap_incidents(ch, cmap)),
                car_status_text(meta, _snapshot(ch), state, cmap))
