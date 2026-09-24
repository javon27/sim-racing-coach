"""#160: the lap in progress, readable corner by corner before the line.

The coach only saw COMPLETED laps, so a spin on the lap being driven was invisible
("You need to be able to read current lap incidents"). LmuSource publishes the
in-progress lap's own buffer (`source.live_lap`, set once per lap -- nothing is copied
per poll); a single worker thread (`LiveLap.run`) looks at it twice a second and, for
each corner the car has left, runs the SAME per-corner analysis a stored lap gets
(`_metrics_for_corner`, `lap_events`, `damage_events`) and stores it as provisional
(store.live_corners). The stored lap supersedes those rows at commit; a lap that never
commits (crash, retirement) keeps them. Capture never waits on or sees this worker.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import time
from types import SimpleNamespace

import numpy as np

from .analysis import (
    _corner_at,
    _metrics_for_corner,
    corner_tag,
    damage_events,
    damage_state,
    lap_events,
)
from .corners import in_window
from .model_types import LapRecord

# ponytail: a flat 0.5 s look, so a corner lands at most ~0.5 s after its window end.
_TICK_S = 0.5


def _snapshot(channels: dict) -> dict[str, np.ndarray]:
    """The live buffer as arrays, every channel cut to the same sample count (capture
    may be mid-append across channels while this reads)."""
    items = list(channels.items())
    if not items:
        return {}
    n = min(len(v) for _, v in items)
    return {k: np.asarray(v[:n], dtype=float) for k, v in items}


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
    n = _corner_at(cmap, e["lap_distance"])
    c = next((c for c in cmap.corners if c.number == n), None) if cmap else None
    where = f" ({corner_tag(c)})" if c is not None else ""
    ago = ""
    if now_et is not None and math.isfinite(now_et) and now_et >= e["et"]:
        ago = f", {now_et - e['et']:.0f} s ago"
    return (f"impact {e['magnitude']:.0f} at {e['lap_distance']:,.0f} m{where}, "
            f"{e['label']}{ago}")


def car_status_text(meta: dict, ch: dict, state: dict | None, cmap) -> str:
    """The car right now, from the lap-in-progress buffer (its last sample is one poll
    old) plus the live pit/garage state. Only what the game reports; nothing inferred."""
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
    st = damage_state(ch)
    if st:
        lines.append(f"Bodywork: dent severity max {st['dent_max']:.0f}, total "
                     f"{st['dent_total']:.0f} (raw 0\N{EN DASH}255 scale, panel unknown).")
        flags = (["bodywork part detached"] if st["detached"] else []) \
            + (["engine overheating"] if st["overheating"] else []) \
            + [f"{w.upper()} flat" for w in st["wheels_flat"]] \
            + [f"{w.upper()} wheel detached" for w in st["wheels_detached"]]
        lines.append("Flags: " + ", ".join(flags) + "." if flags else
                     "Flags: nothing detached, no flat tire, not overheating.")
    return "\n".join(lines)


class LiveLap:
    """The #160 worker. `source` is set by _capture_loop; `cmap_for(track)` resolves
    the corner map (the /live cache pattern, #151)."""

    def __init__(self, store, cmap_for):
        self.store = store
        self.cmap_for = cmap_for
        self.source = None
        self._key: tuple | None = None
        self._done: set[str] = set()
        self._cmap: tuple = (None, None)   # (track, CornerMap | None)

    def _live(self):
        live = getattr(self.source, "live_lap", None)
        return live if live is not None else (None, None)

    def _cmap_of(self, track):
        if self._cmap[0] != track:
            self._cmap = (track, self.cmap_for(track))
        return self._cmap[1]

    def tick(self) -> None:
        """Store every corner the lap in progress has newly completed."""
        meta, ch = self._live()
        if meta is None:
            return
        key = (meta["session_id"], meta["lap_no"], meta["start_ts"])
        if key != self._key:
            # A new lap: forget the last one's corners, and delete provisional rows
            # the laps table has since superseded (reads already hide them).
            self._key, self._done = key, set()
            self.store.drop_superseded_live_corners()
        cmap = self._cmap_of(meta["track"])
        if cmap is None:
            return
        dist = np.asarray(ch.get("lap_distance", [])[:], dtype=float)
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
        cms = [_metrics_for_corner(record, c, cmap.length, None, rpm_max) for c in todo]
        events = lap_events(record, cmap, SimpleNamespace(corners=cms))
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

    def now(self, state: dict | None) -> tuple[dict | None, str | None]:
        """(in-progress lap meta, car status text) for a coach turn, or (None, None)
        with no lap in progress."""
        meta, ch = self._live()
        if meta is None:
            return None, None
        return meta, car_status_text(meta, _snapshot(ch), state, self._cmap_of(meta["track"]))
