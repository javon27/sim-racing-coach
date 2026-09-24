"""Deterministic rendering of analysis facts for the coach — corner lines, tire
report, unit conversion. Used by the on-demand tools (coach_tools). All numbers
are computed facts; nothing here is model-guessed."""
from __future__ import annotations

import math

from simcoach.analysis import _ENVELOPE_MIN_LAPS, corner_tag


def format_laptime(seconds) -> str:
    """Race lap time as m:ss.mmm (e.g. 83.456 -> '1:23.456'), matching the UI lap
    list. Non-finite/negative/None -> '—'."""
    if seconds is None or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return "—"
    # Round to ms BEFORE splitting, else e.g. 119.9996 -> "1:60.000" (the :.3f
    # rounds the seconds field up past 60) instead of "2:00.000".
    total_ms = round(seconds * 1000)
    m, rem_ms = divmod(total_ms, 60_000)
    return f"{m}:{rem_ms / 1000:06.3f}"


def unit_fns(units: str = "metric"):
    """Return (speed_fn, temp_fn, speed_label, temp_label). Distances stay meters."""
    imperial = units == "imperial"
    spd = (lambda v: v * 0.621371) if imperial else (lambda v: v)      # km/h -> mph
    temp = (lambda v: v * 9 / 5 + 32) if imperial else (lambda v: v)   # °C -> °F
    return spd, temp, ("mph" if imperial else "km/h"), ("°F" if imperial else "°C")


# Dead-time reporting floors. Below these it's sampling noise, not a coachable
# finding: at 50 Hz and 200 km/h one sample interval is already ~1.1 m, so a 1 m
# floor would print the sample spacing.
# ponytail: tuning knobs — a rig read on a corner the driver knows they coast
# through settles them.
_COAST_REPORT_S = 0.05
_GAP_REPORT_M = 5.0

# #102: floors for the vs-reference deltas, one per unit family. Below these the
# reading is axis/sensor noise, not a coachable technique difference.
# ponytail: tuning knobs -- a rig read settles them further.
_DELTA_DIST_M = 3.0      # brake/throttle point deltas (m) -- #101 gives a ~1 m axis
_DELTA_COAST_S = 0.1     # coast-time delta (s)
_DELTA_PTS = 0.05        # trail/ABS/TC deltas (fraction points; 0.05 = 5 pts)
_DELTA_G = 0.2           # peak brake/lat g deltas
_DELTA_SPEED_KMH = 3.0   # entry/exit speed deltas, in the native km/h channel

# #38 P1.4: below this a corner's dead-middle fraction isn't worth a clause.
_GG_DEAD_REPORT = 0.30

# #38 P1.8: input-shape report floors.
# ponytail: tuning knobs -- a rig read on a known-pumping/sawing corner settles them.
_DUMPED_FRAC = 0.7      # brake_release_steep_frac at/above this reads as "dumped"
_MOD_REPORT = 2         # brake/throttle reversal count floor before it's worth a clause
_MOD_REPORT_STEER = 3   # steering reversal floor -- sawing is noisier, needs more


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11-13 -> 'Nth' (not 'Nst'/'Nnd'/'Nrd')."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _dist_delta_txt(label: str, delta) -> str:
    if delta is None or abs(delta) < _DELTA_DIST_M:
        return ""
    word = "later" if delta > 0 else "earlier"
    return f", {label} {abs(delta):.0f} m {word} than best"


def _coast_delta_txt(delta) -> str:
    if delta is None or abs(delta) < _DELTA_COAST_S:
        return ""
    return f", {delta:+.1f}s coasting vs best"


def _pct_delta_txt(label: str, delta) -> str:
    if delta is None or abs(delta) < _DELTA_PTS:
        return ""
    return f", {label} {round(delta * 100):+d} pts"


def _g_delta_txt(label: str, delta) -> str:
    if delta is None or abs(delta) < _DELTA_G:
        return ""
    return f", {label} {delta:+.1f}g vs best"


def _speed_delta_txt(label: str, delta, spd, spd_u: str) -> str:
    if delta is None or abs(delta) < _DELTA_SPEED_KMH:
        return ""
    return f", {label} {spd(delta):+.0f} {spd_u} vs best"


def _release_txt(m, delta) -> str:
    if m is None:
        return ""
    txt = f", released brake over {m:.0f} m"
    if delta is not None and abs(delta) >= _DELTA_DIST_M:
        txt += f" ({delta:+.0f} m vs best)"
    return txt


def _brake_reversals_txt(n, delta) -> str:
    if n is None or n < _MOD_REPORT:
        return ""
    txt = f", {n} brake reversals"
    if delta is not None:
        txt += f" (best {n - delta})"
    return txt


def _throttle_reversals_txt(n) -> str:
    if n is None or n < _MOD_REPORT:
        return ""
    return f", {n} throttle stabs"


def _steer_reversals_txt(n) -> str:
    if n is None or n < _MOD_REPORT_STEER:
        return ""
    return f", steering sawing ({n})"


def format_corner_line(cm, units: str = "metric", envelope_formed: bool = True) -> str:
    """One driver-facing corner line: speeds, apex-relative brake/throttle, trail,
    peak g, and the delta vs best. Absolute lap-distance is never shown."""
    spd, _, spd_u, _ = unit_fns(units)
    tag = corner_tag(cm)
    sect = f" [part of {cm.section}]" if cm.section else ""
    if math.isnan(cm.min_spd):
        # No telemetry in this corner's window (e.g. a sparse out-lap): say so
        # rather than render nan speeds + the defaulted (fake) brake/throttle.
        return f"{tag} ({cm.direction}){sect}: no telemetry captured here"
    # None = the driver never braked here. Guard on the field, not on the sign of the
    # subtraction: the sign only stood in for "not found" while the sentinel happened
    # to sit past the apex.
    brake_txt = (
        f"brake {cm.apex_dist - cm.brake_point:.0f}m before apex @ {cm.peak_brake:.0%} peak"
        if cm.brake_point is not None else "no heavy braking"
    )
    # Same for the throttle gate: say nothing rather than turn the corner's own
    # geometry into a claimed driver input.
    thr_txt = (f", back to throttle {cm.throttle_point - cm.apex_dist:.0f}m after apex"
               if cm.throttle_point is not None else "")
    line = (
        f"{tag} ({cm.direction}){sect}: "
        f"{spd(cm.entry_spd):.0f}→min {spd(cm.min_spd):.0f}→{spd(cm.exit_spd):.0f} {spd_u}, "
        f"{brake_txt}{thr_txt}, trail {cm.trailbrake_overlap:.0%}, peak {cm.peak_lat_g:.1f}g"
    )
    # Extended inputs/loads (guarded — older laps lack these channels).
    if not math.isnan(getattr(cm, "peak_brake_g", float("nan"))):
        line += f", brake {cm.peak_brake_g:.1f}g"
    if not math.isnan(getattr(cm, "peak_accel_g", float("nan"))):
        line += f", accel {cm.peak_accel_g:.1f}g"
    if not math.isnan(getattr(cm, "peak_steer", float("nan"))):
        line += f", steer {cm.peak_steer:.0%}"
    aids = []
    if not math.isnan(getattr(cm, "abs_pct", float("nan"))) and cm.abs_pct > 0:
        aids.append(f"ABS {cm.abs_pct:.0%}")
    if not math.isnan(getattr(cm, "tc_pct", float("nan"))) and cm.tc_pct > 0:
        aids.append(f"TC {cm.tc_pct:.0%}")
    if aids:
        line += ", " + " ".join(aids)
    # #38 P1.7: gear/RPM sanity -- silent when the lap lacks gear/rpm channels.
    gear_min = getattr(cm, "gear_min", None)
    if gear_min is not None:
        line += f", {ordinal(gear_min)} gear at min speed"
    if getattr(cm, "bog", None):
        line += ", bogged on exit"
    if getattr(cm, "short_shift", None):
        line += ", short-shifted"
    # track_edge_min is computed but NOT rendered (#99 second review): the
    # mPathLateral/mTrackEdge units and sign are still PENDING LIVE VALIDATION
    # (smm.py), and the rule is cut, don't hedge. Restore once rig-verified.
    # Dead time. Silent on a clean corner — zero coasting shouldn't carry a line.
    coast_t = getattr(cm, "coast_time", float("nan"))
    if not math.isnan(coast_t) and coast_t > _COAST_REPORT_S:
        line += f", coasting {coast_t:.1f}s ({cm.coast_dist:.0f} m)"
    gap = getattr(cm, "brake_to_throttle_dist", float("nan"))
    if not math.isnan(gap) and gap > _GAP_REPORT_M:
        line += f", {gap:.0f} m brake\N{RIGHTWARDS ARROW}throttle gap"
    # Double-apex: name it and give the per-apex minima so the coach advises on both.
    aps = cm.apexes or []
    if len(aps) > 1 and cm.apex_min_speeds:
        mins = "/".join(f"{spd(s):.0f}" for s in cm.apex_min_speeds)
        line += f", {len(aps)}-apex (per-apex min {mins} {spd_u})"
    # #38 balance read -- short, the model narrates the detail from get_handling.
    handling = getattr(cm, "handling", None)
    if handling == "understeer":
        line += ", understeer (steering still winding, lat g flat)"
    elif handling == "oversteer":
        line += ", oversteer (countersteer at exit)"
    if getattr(cm, "lockup", None):
        line += ", lockup"
    if getattr(cm, "wheelspin", None):
        line += ", wheelspin"
    # #38 P1.4 / #108 item 2: traction-circle utilization -- silent without an
    # envelope read, and silent (not just clamped) while the envelope is still
    # forming: a ratio against a barely-sampled envelope is noise, not a read.
    gg_used = getattr(cm, "gg_used", None)
    if gg_used is not None and envelope_formed:
        line += f", grip {min(gg_used, 1.0):.0%} of envelope"
        dead = getattr(cm, "gg_dead_middle", None)
        if dead is not None and dead >= _GG_DEAD_REPORT:
            line += f", {dead:.0%} of corner under half grip (inputs not combined)"
    if cm.min_spd_delta is not None:
        line += f" (min vs best: {spd(cm.min_spd_delta):+.1f} {spd_u})"
    # #102: every other comparable metric, vs the reference -- relative and
    # driver-facing, silent below its floor or with no reference (delta is None).
    line += _dist_delta_txt("brake", getattr(cm, "brake_point_delta", None))
    line += _dist_delta_txt("throttle", getattr(cm, "throttle_point_delta", None))
    line += _coast_delta_txt(getattr(cm, "coast_time_delta", None))
    line += _pct_delta_txt("trail", getattr(cm, "trailbrake_overlap_delta", None))
    line += _pct_delta_txt("ABS", getattr(cm, "abs_pct_delta", None))
    line += _pct_delta_txt("TC", getattr(cm, "tc_pct_delta", None))
    line += _g_delta_txt("brake", getattr(cm, "peak_brake_g_delta", None))
    line += _g_delta_txt("peak", getattr(cm, "peak_lat_g_delta", None))
    line += _speed_delta_txt("entry", getattr(cm, "entry_spd_delta", None), spd, spd_u)
    line += _speed_delta_txt("exit", getattr(cm, "exit_spd_delta", None), spd, spd_u)
    # #38 P1.8: input-shape facts -- silent without a braking phase / channel.
    line += _release_txt(getattr(cm, "brake_release_m", None),
                         getattr(cm, "brake_release_m_delta", None))
    steep_frac = getattr(cm, "brake_release_steep_frac", None)
    if steep_frac is not None and steep_frac >= _DUMPED_FRAC:
        line += ", brake dumped"
    line += _brake_reversals_txt(getattr(cm, "brake_reversals", None),
                                 getattr(cm, "brake_reversals_delta", None))
    line += _throttle_reversals_txt(getattr(cm, "throttle_reversals", None))
    line += _steer_reversals_txt(getattr(cm, "steer_reversals", None))
    return line


# #108 item 12: a code-generated spoken locator ("Turn 10, sector 2, the fast
# left after the long straight") -- a bare corner NUMBER only works with a map
# open, and a corner NAME the driver doesn't know is useless in the headset.
# Built entirely from owned data (map geometry + measured min_spd); the model
# never guesses direction, position, or what precedes a corner.
_SPEED_CLASS_KMH = ((70, "hairpin"), (110, "slow"), (160, "medium"), (220, "fast"))
_FLAT_OUT_CLASS = "flat-out"

# ponytail: tuning knobs for the "precedes" clause's gap wording -- a rig read
# on a straight the driver knows settles these.
_GAP_LONG_STRAIGHT_M = 600.0
_GAP_STRAIGHT_M = 250.0
_GAP_STRAIGHT_AFTER_M = 80.0


def _speed_class(min_spd_kmh: float) -> str:
    for limit, name in _SPEED_CLASS_KMH:
        if min_spd_kmh < limit:
            return name
    return _FLAT_OUT_CLASS


def corner_descriptor(cmap, corner_no, analysis=None, sector_bounds=None) -> str:
    """One spoken locator for a corner -- direction, speed class, what precedes
    it, where in the lap -- composed from the map (`cmap`) and, optionally, a
    lap's measured speeds (`analysis`, a `LapAnalysis`; pass the reference/best
    lap's -- a spun lap's min speed lies) and sector splits (`sector_bounds`,
    `record.sector_bounds`). Each part is omitted, not guessed, when it can't be
    computed. `cmap.corners` must be sorted by `start_dist`."""
    corners = cmap.corners
    idx = next((i for i, c in enumerate(corners) if c.number == corner_no), None)
    if idx is None:
        return f"Turn {corner_no}"
    corner = corners[idx]
    parts = [f"Turn {corner.label or corner.number}"]

    if sector_bounds:
        b0, b1 = sector_bounds[0], sector_bounds[1]
        sector = 1 if corner.apex_dist <= b0 else (2 if corner.apex_dist <= b1 else 3)
        parts.append(f"sector {sector}")
    elif cmap.length:
        frac = corner.apex_dist / cmap.length
        parts.append("early in the lap" if frac <= 1 / 3 else
                     "middle of the lap" if frac <= 2 / 3 else "back half")

    if not sector_bounds:
        if idx == 0:
            parts.append("the first corner")
        elif idx == len(corners) - 1:
            parts.append("the last corner")

    # Speed class + direction -- e.g. "the fast left"; hairpins read direction-first.
    cls = None
    if analysis is not None:
        cm = next((c for c in analysis.corners if c.corner_no == corner_no), None)
        if cm is not None and not math.isnan(cm.min_spd):
            cls = _speed_class(cm.min_spd)
    tail = []
    if corner.direction:
        if cls == "hairpin" and corner.direction in ("left", "right"):
            tail.append(f"the {corner.direction}-hand hairpin")
        elif cls and corner.direction in ("left", "right"):
            tail.append(f"the {cls} {corner.direction}")
        elif corner.direction in ("left", "right"):
            # No speed class: "the left-hander", never a bare "left" -- spoken,
            # "left after the straight" reads as "immediately after".
            tail.append(f"the {corner.direction}-hander")
        else:
            tail.append(corner.direction)

    # What precedes it -- gap from the previous map corner's end to this one's
    # start; wraps the S/F seam for the first corner when the lap length is known.
    prev = corners[idx - 1] if len(corners) > 1 else None
    if prev is not None:
        gap = ((cmap.length - prev.end_dist) + corner.start_dist if idx == 0 and cmap.length
               else None if idx == 0 else corner.start_dist - prev.end_dist)
        if gap is not None:
            prev_label = prev.label or prev.number
            if gap >= _GAP_LONG_STRAIGHT_M:
                tail.append("after the long straight")
            elif gap >= _GAP_STRAIGHT_M:
                tail.append("after the straight")
            elif gap <= _GAP_STRAIGHT_AFTER_M:
                tail.append(f"straight after Turn {prev_label}")
            else:
                tail.append(f"after Turn {prev_label}")

    if tail:
        parts.append(" ".join(tail))
    return ", ".join(parts)


# How many loss buckets the coach is handed. More than this and the ranking stops
# being a ranking. ponytail: a flat cap; a "everything above 0.1s" rule would adapt.
_TOP_BUCKETS = 5
# Below this a bucket is integration/interpolation noise, not a coachable loss.
# Two floors, keyed to which time axis `time_loss_buckets` used (#101): the exact
# elapsed_time path is precise to interpolation noise only, while the ds/v
# fallback integrates ~50 Hz speed samples and carries the model's own ~0.2s
# median error (smm.py) -- a fixed 0.02s floor was too tight for the ds/v path's
# real noise and too loose for the exact path's.
# ponytail: tuning knobs — a rig read settles them further.
_BUCKET_NOISE_S_EXACT = 0.005
_BUCKET_NOISE_S_DS_V = 0.02


def format_time_loss(buckets, reference_label: str = "your best lap") -> str:
    """Net delta plus the biggest loss buckets, ranked. `buckets` carry delta
    POSITIVE = time lost; output signs a loss NEGATIVE, the way a driver reads a
    delta ("you're -0.31s at T4"). The biggest gain is named too, so the coach can
    reinforce what worked."""
    if not buckets:
        return ("No comparable reference lap to measure time loss against yet — "
                "you need a second valid lap on this track/car.")
    # Every bucket in one call carries the same "exact" flag (see
    # `time_loss_buckets`); buckets without one (older callers/fixtures) get the
    # wider, more conservative ds/v floor.
    noise = _BUCKET_NOISE_S_EXACT if buckets[0].get("exact") else _BUCKET_NOISE_S_DS_V
    net = sum(b["delta"] for b in buckets)
    out = f"Net {-net:+.2f}s vs {reference_label}."
    losses = sorted((b for b in buckets if b["delta"] > noise),
                    key=lambda b: -b["delta"])[:_TOP_BUCKETS]
    if losses:
        out += " Biggest losses: " + ", ".join(
            f"{b['label']} {-b['delta']:+.2f}s" for b in losses) + "."
    gains = [b for b in buckets if b["delta"] < -noise]
    if gains:
        g = min(gains, key=lambda b: b["delta"])
        out += f" Biggest gain: {g['label']} {-g['delta']:+.2f}s."
    if not losses and not gains:
        out += " No single section stands out — this lap tracks the reference closely."
    return out


# A per-field sigma below this many samples is one lap standing in for a trend --
# stay silent rather than print a "spread" that isn't one.
_MIN_FIELD_SAMPLES = 3
# How many of the ranked corners to name.
_TOP_CONSISTENCY_CORNERS = 3


def format_consistency(result: dict, units: str = "metric") -> str:
    """House style for `analysis.stint_consistency`: lap-time spread, then up to
    `_TOP_CONSISTENCY_CORNERS` least-consistent corners (highest-variance first).
    Silent on any per-field sigma with n < `_MIN_FIELD_SAMPLES` samples, and an
    honest one-liner when the stint itself was too short to analyse."""
    if not result.get("ok"):
        n = result.get("n", 0)
        return (f"Only {n} lap{'s' if n != 1 else ''} recorded this stint — need at "
                "least 3 to read consistency.")
    spd, _, spd_u, _ = unit_fns(units)
    out = (f"Lap time \N{GREEK SMALL LETTER SIGMA} {result['lap_time_sigma']:.1f}s over "
           f"{result['n']} laps (best {format_laptime(result['lap_time_best'])}, "
           f"mean {format_laptime(result['lap_time_mean'])}).")
    corner_parts = []
    for label in result["ranking"][:_TOP_CONSISTENCY_CORNERS]:
        entry = result["corners"].get(label, {})
        bits = []
        ms = entry.get("min_spd")
        if ms and ms["n"] >= _MIN_FIELD_SAMPLES:
            bits.append(f"min speed ±{spd(ms['sigma']):.0f} {spd_u}")
        bp = entry.get("brake_point")
        if bp and bp["n"] >= _MIN_FIELD_SAMPLES:
            bits.append(f"brake point ±{bp['sigma']:.0f} m")
        ct = entry.get("coast_time")
        if ct and ct["n"] >= _MIN_FIELD_SAMPLES:
            bits.append(f"coasting ±{ct['sigma']:.1f}s")
        if bits:
            corner_parts.append(f"{label} — " + ", ".join(bits))
    if corner_parts:
        out += " Least consistent: " + "; ".join(corner_parts) + "."
    return out


def format_wheel_report(analysis, units: str = "metric") -> str | None:
    """Lap-level brake temps, hot pressures, and tire wear per wheel — or None if
    the lap predates the extended capture."""
    w = analysis.wheels
    if not w:
        return None
    _, temp, _, temp_u = unit_fns(units)
    bt_m, bt_p = w["brake_temp_mean"], w["brake_temp_peak"]
    lines = [
        f"Brake temps {temp_u} (mean/peak): "
        + " / ".join(f"{k.upper()} {temp(bt_m[k]):.0f}/{temp(bt_p[k]):.0f}" for k in ("fl", "fr", "rl", "rr"))
    ]
    if "tire_press_mean" in w:
        p = w["tire_press_mean"]
        lines.append("Tire pressures kPa (mean): "
                     + " / ".join(f"{k.upper()} {p[k]:.0f}" for k in ("fl", "fr", "rl", "rr")))
    if "tire_wear_end" in w:
        wr = w["tire_wear_end"]
        lines.append("Tire wear remaining: "
                     + " / ".join(f"{k.upper()} {wr[k]:.0%}" for k in ("fl", "fr", "rl", "rr")))
    return "\n".join(lines)


# --- tire-deg trend (#38 P1.6) -----------------------------------------------
# Below these magnitudes a per-lap slope is sensor/sampling noise, not a
# coachable trend -- name only what clears its floor, "steady" for the rest.
# ponytail: tuning knobs -- a rig read across a real stint settles them.
_TREND_WEAR_PCT = 0.3    # %/lap
_TREND_PACE_S = 0.05     # s/lap
# #161: wear this fast (%/lap, any wheel) is enough to call a pace loss degradation.
# ponytail: a judgement, PENDING RIG -- the real signal is grip vs tread, which LMU
# doesn't expose (mGripFract is dead); tune against a long stint that truly fell off.
_DEG_WEAR_PCT = 1.0
_TREND_TEMP_C = 1.0      # °C/lap
_TREND_PRESS_KPA = 1.0   # kPa/lap


def format_tire_trend(result: dict, units: str = "metric") -> str:
    """House style for `analysis.stint_tire_trend`: pace drift, wheel wear (plus
    which wheel is running hottest, when its own temp is trending), and
    pressures -- naming only what clears its floor, "steady" for the rest.
    Honest one-liner when the stint was too short to read a trend."""
    if not result.get("ok"):
        n = result.get("n", 0)
        return (f"Only {n} lap{'s' if n != 1 else ''} recorded this stint — need at "
                "least 3 to read tire trend.")
    _, temp, _, temp_u = unit_fns(units)
    wheels = ("fl", "fr", "rl", "rr")

    pace = result.get("pace_slope_s")
    pace_trending = pace is not None and abs(pace) >= _TREND_PACE_S
    pace_txt = (f"pace {pace:+.2f} s/lap" if pace_trending else "pace steady"
                if pace is not None else "pace: not enough incident-free laps for a trend")

    wear = result.get("wear_pct_per_lap") or {}
    most_worn = result.get("most_worn")
    bits = [
        f"{w.upper()} wear {wear[w]:+.1f}%/lap" + (" (most worn)" if w == most_worn else "")
        for w in wheels if wear.get(w) is not None and abs(wear[w]) >= _TREND_WEAR_PCT
    ]

    hottest = result.get("hottest")
    temp_slope = result.get("temp_slope") or {}
    temp_mean = result.get("temp_mean") or {}
    if hottest is not None and abs(temp_slope.get(hottest, 0.0)) >= _TREND_TEMP_C:
        bits.append(f"{hottest.upper()} running hottest "
                     f"({temp(temp_mean[hottest]):.0f} {temp_u} mean)")
    wear_txt = ", ".join(bits) if bits else "temps and wear steady"

    press = result.get("press_slope") or {}
    press_bits = [
        f"{w.upper()} pressure {press[w]:+.1f} kPa/lap"
        for w in wheels if press.get(w) is not None and abs(press[w]) >= _TREND_PRESS_KPA
    ]
    press_txt = ", ".join(press_bits) if press_bits else "pressures steady"

    out = f"Over {result['n']} laps: {pace_txt}; {wear_txt}; {press_txt}."
    # #161: a pace LOSS is called degradation only when the tires show it -- wear
    # fast enough to cost time, or a tire heating lap on lap. 0.6 %/lap at 93 %
    # tread (Road Atlanta) is not that; its "3.53 s/lap" was two spins.
    tires_show_it = (any(v >= _DEG_WEAR_PCT for v in wear.values())
                     or any(v >= _TREND_TEMP_C
                            for v in (result.get("temp_slope_settled") or {}).values()))
    if pace_trending and pace > 0:
        out += (" — deg-limited" if tires_show_it else
                " — pace dropped, but wear/temps don't show degradation")
    k = result.get("incident_laps") or 0
    if k:
        out += (f" {k} incident lap{'s' if k != 1 else ''} (spin/off/contact) left out "
                "of the pace trend.")
    return out


def format_tire_report(analysis, units: str = "metric") -> str | None:
    """Per-wheel mean temps (imbalance) + early→late warm-up, or None if the lap
    was captured without tire channels."""
    if not analysis.tire_means:
        return None
    _, temp, _, temp_u = unit_fns(units)
    m = analysis.tire_means
    out = (
        f"Tire temps {temp_u} (mean over lap): FL {temp(m['fl']):.0f} / FR {temp(m['fr']):.0f} / "
        f"RL {temp(m['rl']):.0f} / RR {temp(m['rr']):.0f}."
    )
    if analysis.tire_warmup:
        early, late = analysis.tire_warmup
        out += (
            f"\nTire warm-up: ~{temp(early):.0f}{temp_u} early in the lap → "
            f"~{temp(late):.0f}{temp_u} late."
        )
    return out


def format_lap_facts(analysis, units: str = "metric", corner_order=None, top_loss=None) -> str:
    """The full per-corner table + tire report for one lap.

    `corner_order` (optional): explicit corner_no ordering, for a caller that wants
    the worst time-loss corners surfaced first (#102) instead of track order (the
    default). `top_loss` (optional): corner_no's to flag with a leading marker.
    """
    lines = ["Corner facts (authoritative, from telemetry):"]
    if analysis.has_reference:
        lines.append("Deltas below are vs the driver's best lap on this track/car.")
    by_no = {cm.corner_no: cm for cm in analysis.corners}
    order = corner_order if corner_order is not None else list(by_no)
    top_loss = top_loss or set()
    envelope_laps = getattr(analysis, "gg_envelope_laps", None)
    envelope_formed = envelope_laps is not None and envelope_laps >= _ENVELOPE_MIN_LAPS
    for no in order:
        cm = by_no.get(no)
        if cm is None:
            continue
        mark = "\N{BLACK UP-POINTING TRIANGLE} " if no in top_loss else ""
        lines.append(mark + format_corner_line(cm, units, envelope_formed=envelope_formed))
    env = getattr(analysis, "gg_envelope", None)
    lap_gg_used = getattr(analysis, "gg_used", None)
    lap_gg_peak = getattr(analysis, "gg_peak", None)
    # #108 item 2: a percentage off an envelope resting on too few laps is
    # meaningless (a first hot lap once read "143%") -- report peak g instead.
    if envelope_formed and env is not None and lap_gg_used is not None:
        lines.append(f"Grip envelope ~{env:.1f} g (observed); this lap used "
                     f"{min(lap_gg_used, 1.0):.0%} of it in the corners.")
    elif not envelope_formed and lap_gg_peak is not None:
        lines.append(f"Peak combined grip this lap: {lap_gg_peak:.1f} g (grip envelope "
                     f"still forming: {envelope_laps or 0} of {_ENVELOPE_MIN_LAPS} laps).")
    coast = getattr(analysis, "coast_total", None)
    if coast and coast[0] > _COAST_REPORT_S:
        lines.append(f"Coasting this lap: {coast[0]:.1f} s ({coast[1]:.0f} m) with "
                     "neither pedal.")
    tire = format_tire_report(analysis, units)
    if tire:
        lines.append(tire)
    return "\n".join(lines)


def _event_corner_tag(cmap, corner_no) -> str:
    """'T10 Pouhon' for a mapped corner, 'T5' when the map names none, '' when the
    incident sits outside every corner window. Same designation `format_corner_line`
    shows, so the driver reads one naming scheme."""
    if corner_no is None or cmap is None:
        return ""
    for c in getattr(cmap, "corners", []):
        if c.number == corner_no:
            return corner_tag(c)
    return f"T{corner_no}"


def format_events(events, cmap, units: str = "metric") -> list[str]:
    """One driver-facing line per incident from `analysis.lap_events`, corner-tagged
    the way corner lines are. Every number here is measured; nothing is hedged."""
    spd, _, spd_u, _ = unit_fns(units)
    lines = []
    for e in events:
        tag = _event_corner_tag(cmap, e.get("corner_no"))
        head = f"{tag}: " if tag else ""
        kind, d = e["kind"], e.get("detail") or {}
        if kind == "spin":
            lines.append(f"{head}spin \N{EM DASH} {spd(d['speed_from_kmh']):.0f} "
                         f"\N{RIGHTWARDS ARROW} {spd(d['speed_min_kmh']):.0f} {spd_u}, "
                         f"turned {d['turned_deg']:.0f}\N{DEGREE SIGN}")
        elif kind == "off":
            # The metres figure stays in the event detail only: track_edge units are
            # still pending live validation (see `_metrics_for_corner`) and the corner
            # line deliberately withholds the same number. State the fact, not an
            # unverified measurement.
            lines.append(f"{head}off track")
        elif kind in ("lockup", "wheelspin"):
            lines.append(f"{head}{kind}")
        elif kind == "wear_step":
            where = f" at {tag}" if tag else ""
            lines.append(f"{d['wheel'].upper()} wear step {d['step_pct']:.1f}%{where} "
                         f"(stint median {d['baseline_pct']:.1f}%/lap)")
    return lines


def format_damage_line(events: list, state: dict) -> str | None:
    """One line for a lap that took contact, or None for a clean lap -- and None
    for a lap with no damage channels (older capture), never implied clean.
    Impact magnitude and dent severity are raw, unitless numbers (#143: the only
    anchor is the rig driver's own "more than a tap" at 365 -- no invented
    tiers); the dent scale is per-panel-unknown, so it is never broken out by
    panel, only max/total."""
    if not events:
        return None
    heaviest = max(events, key=lambda e: e["magnitude"])
    n = len(events)
    line = (f"Contact: {n} impact{'s' if n != 1 else ''}, heaviest "
            f"{heaviest['magnitude']:.0f} at {heaviest['lap_distance']:,.0f} m "
            f"({heaviest['label']})")
    if state:
        line += (f". Bodywork damage: dent severity max {state['dent_max']:.0f}, "
                 f"total {state['dent_total']:.0f} (raw 0\N{EN DASH}255 scale, panel unknown)")
        flags = []
        if state.get("detached"):
            flags.append("bodywork detached")
        if state.get("overheating"):
            flags.append("overheating")
        flags.extend(f"{w.upper()} flat" for w in state.get("wheels_flat", []))
        flags.extend(f"{w.upper()} wheel detached" for w in state.get("wheels_detached", []))
        if flags:
            line += "; " + ", ".join(flags)
    return line + "."


def find_corner(analysis, corner) -> object | None:
    """Look up a corner by number (int or numeric string) or name (case-insensitive
    substring), so a tool can fetch just the one the driver asked about."""
    key = str(corner).strip().lower()
    for cm in analysis.corners:
        lbl = str(cm.label or cm.corner_no).lower()
        if key in (str(cm.corner_no), f"t{cm.corner_no}", lbl, f"t{lbl}"):
            return cm
    for cm in analysis.corners:
        if key and key in cm.name.lower():
            return cm
    return None
