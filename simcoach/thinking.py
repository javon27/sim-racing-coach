"""#175: how hard the coach thinks before answering -- quick / standard / deep.

Code gates first (0 ms: the on-track path never waits on a model), then a small
classifier model behind the `Model` seam (<= 800 ms, JSON), with keyword rules as the
fallback when it is off, slow, or wrong-shaped. Provider-neutral: what a tier means
for a given model is the adapter's business (simcoach/model/). A wrong tier costs a
slower or shallower answer, never a wrong fact -- thinking never produces a figure.
Spec: docs/superpowers/specs/2026-09-25-coach-thinking-design.md.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
from collections.abc import Callable

from simcoach.battle import _corner_label
from simcoach.model.base import TURN_TAG, TokenUsage

_log = logging.getLogger(__name__)

TIERS = ("quick", "standard", "deep")
SETTINGS = ("auto", "off", "always")     # coach.thinking
CLASSIFY_TIMEOUT_S = 0.8
MOVING_KPH = 10.0      # ponytail: fixed; below this the car is parked/crawling (pit box)
# A result row describes the race being driven only if it started this close before
# that race's first captured lap. ponytail: generous fixed bound (grid + formation +
# a long first lap); a result from an earlier race of the sitting is hours older.
RESULT_LEAD_S = 3600.0

INTENTS = ("time_loss", "corner", "setup", "technique", "race_review", "race_live",
           "strategy", "car_status", "history", "small_talk", "other")

SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"enum": ["standard", "deep"]},
        "intent": {"enum": list(INTENTS)},
        "corner": {"type": ["string", "null"]},
        "lap": {"type": ["string", "null"]},
        "rival": {"type": ["string", "null"]},
        "wants_detail": {"type": "boolean"},
    },
    "required": ["tier", "intent", "corner", "lap", "rival", "wants_detail"],
}

_QUICK = re.compile(r"\bquick answer\b", re.IGNORECASE)
_DEEP = re.compile(r"\b(?:think (?:about it|it (?:over|through)|hard)|take your time)\b",
                   re.IGNORECASE)
# A look back over a finished race -- deep automatically once the race is over.
_REVIEW = re.compile(r"\b(?:how (?:was|did) (?:that|the|my) race|how did (?:i|it|that) go|"
                     r"debrief|race review|review (?:the|that|my) race|"
                     r"analy[sz]e (?:the|that|my) race|(?:the|that|my) race in detail)\b",
                     re.IGNORECASE)
# Keyword floor (classifier off/slow/failed): a question that asks for a multi-tool
# answer. ponytail: a word list -- the classifier exists for what it misses.
_DEEP_WORDS = re.compile(r"\b(?:in detail|detailed|analy[sz]e|analysis|break (?:it )?down|"
                         r"walk me through|compare|comparison|debrief|whole (?:race|stint|"
                         r"session)|lap by lap|overall|why (?:do|does|am|is|did)|"
                         r"what'?s causing)\b", re.IGNORECASE)

_ACK = {
    "race_review": "Give me a moment — going through the race lap by lap.",
    "time_loss": "Give me a moment — working out where the time is.",
    "setup": "Give me a moment — thinking through the setup.",
    "history": "Give me a moment — going back through your sessions.",
}
_ACK_DEFAULT = "Give me a moment — thinking this through."

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="classify")


def _gate(question: str, setting: str, moving: bool, race_finished: bool) -> str | None:
    """The code hard gates, in precedence order, or None to let the classifier (or
    the keyword floor) decide."""
    if moving:
        return "quick"          # on track and moving: never wait on deliberation
    if _QUICK.search(question):
        return "quick"
    if _DEEP.search(question):
        return "deep"           # the driver's own words beat the setting
    if setting == "off":
        return "quick"
    if setting == "always":
        return "standard"       # standard everywhere, deep on request (above)
    if race_finished and _REVIEW.search(question):
        return "deep"           # decided 2026-09-25: post-race review is deep
    return None


def thinking_tier(question: str, *, setting: str = "auto", moving: bool = False,
                  race_finished: bool = False, classified: dict | None = None) -> str:
    """The tier for `question`: the code gates, else a valid classifier result
    (`classified`, None when it was off/slow/failed), else the keyword floor --
    deep for a detailed/analytic ask, standard otherwise (the garage default)."""
    if setting not in SETTINGS:
        setting = "auto"
    tier = _gate(question, setting, moving, race_finished)
    if tier:
        return tier
    if classified is not None:
        return classified["tier"]
    return "deep" if _DEEP_WORDS.search(question) else "standard"


def race_finished(windows: list[dict], result_ts: float | None, *, has_result: bool,
                  race_live: bool, live_phase: str | None, laps: list[dict]) -> bool:
    """Is the race to talk about over? For the post-race-review gate and the
    classifier's state line. `windows` = the session's race windows (oldest first),
    `has_result`/`result_ts` = its result-log row and that race's start stamp,
    `race_live` = a live race-phase session is attached (snapshot or its note),
    `live_phase` = the live session's phase (None: not in a session), `laps` = the
    session's laps (with "phase", "ts").

    Over when the result log describes the LAST race (LMU keeps phase "race" after the
    flag until the driver leaves, so race_live alone reads a finished race as live),
    or when there are race laps and no live race. Never over while a non-race session
    is live, or once laps from a non-race session follow the last race: one session id
    spans the evening, and a practice question is not a race review."""
    if live_phase and live_phase != "race":
        return False
    if windows and any(lap.get("phase") != "race"
                       and (lap.get("ts") or 0) > windows[-1]["end_ts"] for lap in laps):
        return False
    if has_result:
        if not windows:
            return True
        after_prev = len(windows) < 2 or (result_ts or 0) > windows[-2]["end_ts"]
        near_last = (result_ts is not None
                     and result_ts >= windows[-1]["start_ts"] - RESULT_LEAD_S)
        if after_prev and (near_last or not race_live):
            return True
    return bool(windows) and not race_live


def ack_line(intent: str | None) -> str:
    """The code-chosen line a deep answer opens with, before the model is called."""
    return _ACK.get(intent or "", _ACK_DEFAULT)


def match_corner(name: str | None, cmap) -> str | None:
    """The classifier's corner, resolved against the OWNED corner map by code: a
    number/label ("5", "T5", "turn 5", "8A") or a corner name. Only an unambiguous
    match counts -- a label, else the one exact full name, else the one name
    containing it ("variante" on Monza names three corners: None). Unmatched -> None:
    a model's corner name is never passed on as a fact."""
    if not name or cmap is None:
        return None
    raw = str(name).strip().lower()
    key = re.sub(r"^(?:turn|t)\s*", "", raw)
    for c in cmap.corners:
        if key in (str(c.label or c.number).lower(), str(c.number)):
            return _corner_label(c)
    named = [c for c in cmap.corners if c.name]
    for hits in ([c for c in named if c.name.lower() == raw],
                 [c for c in named if len(raw) >= 4 and raw in c.name.lower()]):
        if hits:
            return _corner_label(hits[0]) if len(hits) == 1 else None
    return None


def _valid(obj) -> dict | None:
    """A classifier object the router may use, or None. Only `tier` is required;
    an unknown intent is dropped, not trusted."""
    if not isinstance(obj, dict) or obj.get("tier") not in ("standard", "deep"):
        return None
    intent = obj.get("intent")
    return {**obj, "intent": intent if intent in INTENTS else None}


def _prompt(question: str, history: list[dict], state: str) -> str:
    turns = [f"{m['role']}: {str(m['content'])[:300]}" for m in history[-4:]
             if isinstance(m.get("content"), str)]
    return (
        "Classify a sim-racing driver's question to his race engineer. tier: 'deep' "
        "when a good answer needs several data reads planned and cross-checked (a race "
        "review after the race, a detailed or comparative analysis, a setup problem "
        "with no obvious single cause); otherwise 'standard'. intent: what is asked. "
        "corner/lap/rival: as the driver named them, else null. wants_detail: he asked "
        "for detail.\n"
        f"State: {state}\n"
        + ("Last turns:\n" + "\n".join(turns) + "\n" if turns else "")
        + f"Question: {question}")


def route(question: str, *, setting: str = "auto", moving: bool = False,
          race_finished: bool = False, classifier: Callable | None = None,
          history: list[dict] | None = None, state: str = "",
          cmap=None) -> tuple[str, str | None, str, TokenUsage]:
    """(tier, intent, one line of context for the coach or "", what the classifier
    billed). `classifier` is a zero-argument callable returning the classifier Model
    (or None). It is built lazily -- only when no gate decided and the setting is auto
    -- and building it counts against CLASSIFY_TIMEOUT_S, after which the keyword
    floor answers. Every classification is logged (INFO)."""
    t0 = time.monotonic()
    if setting not in SETTINGS:
        setting = "auto"
    gated = _gate(question, setting, moving, race_finished)
    classified, usage, via = None, TokenUsage(), "gate" if gated else "rules"
    if gated is None and setting == "auto" and classifier is not None:
        prompt = _prompt(question, history or [], state)

        def call():
            model = classifier()
            return model.classify(prompt, SCHEMA) if model else (None, TokenUsage())
        # ponytail: 2 workers -- a burst of timed-out calls (each bounded by the
        # adapter's 0.8 s SDK timeout) can queue the next one past its budget.
        fut = _pool.submit(call)
        try:
            raw, usage = fut.result(timeout=CLASSIFY_TIMEOUT_S)
            classified = _valid(raw)
            via = "classifier" if classified else f"rules (bad classifier output {raw!r})"
        except concurrent.futures.TimeoutError:
            # ponytail: the late call still finishes (the adapter's own timeout bounds
            # it) and its tokens go unmetered -- a sub-cent call.
            via = "rules (classifier timeout)"
        except Exception as e:
            via = f"rules (classifier error {type(e).__name__}: {e})"
    tier = thinking_tier(question, setting=setting, moving=moving,
                         race_finished=race_finished, classified=classified)
    intent = classified["intent"] if classified else None
    corner = match_corner(classified.get("corner"), cmap) if classified else None
    parts = [f"question type: {intent.replace('_', ' ')}" if intent else "",
             f"about {corner}" if corner else "",
             "the driver wants detail" if classified and classified.get("wants_detail") is True
             else ""]
    hint = "; ".join(p for p in parts if p)
    _log.info("coach thinking %s tier=%s via=%s setting=%s moving=%s race_finished=%s "
              "classified=%s %.0f ms question=%r", TURN_TAG.get(), tier, via, setting,
              moving, race_finished, json.dumps(classified) if classified else None,

              (time.monotonic() - t0) * 1000, question[:300])
    return tier, intent, (f"ROUTER NOTE (a hint, not an instruction): {hint}." if hint
                          else ""), usage
