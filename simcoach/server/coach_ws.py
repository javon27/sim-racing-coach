"""The /coach websocket (#113): one question in, streamed coach events out, turn persisted."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config
from ..car_lookup import resolve_car
from ..car_notes import load_car_notes, load_setup_concepts
from ..coach import is_repeat_request
from ..coach_tools import (
    _PARTIAL_LIST_WORD,
    CoachContext,
    _lap_age,
    current_race_fact,
    race_turn_note,
    scrub_stored_answer,
    session_turn_note,
)
from ..driver import DriverProfile
from ..format import format_laptime
from ..model.base import TokenUsage
from ..model.factory import CoachUserError
from ..race import live_race
from ..thinking import MOVING_KPH, route
from ..tiers import legal_adjustments
from .security import ALLOWED_HOSTS, _bad_origin
from .serialize import _usage_view


def register(app, deps) -> None:
    store = app.state.store
    _ensure_rest = deps._ensure_rest
    _map_for = deps._map_for
    _live_session_id = deps._live_session_id

    @app.websocket("/coach")
    async def coach_ws(ws: WebSocket):
        # HTTP Host-guard middleware does NOT run for websockets — check here.
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)      # reject before accept
            return
        await ws.accept()

        # Read + validate the one request message. receive_json raises ValueError on
        # non-JSON; a non-dict body has no .get — guard both, don't crash the socket.
        try:
            req = await ws.receive_json()
        except WebSocketDisconnect:
            return
        except ValueError:
            await ws.send_json({"type": "error", "message": "invalid JSON"})
            await ws.close(code=1003);  return
        if not isinstance(req, dict):
            await ws.send_json({"type": "error", "message": "expected a JSON object"})
            await ws.close(code=1003);  return
        question = (req.get("question") or "").strip()
        if not question:
            await ws.send_json({"type": "error", "message": "empty question"})
            await ws.close(code=1003);  return

        voice = req.get("voice")
        voice = voice if voice in ("ptt", "conversation") else None

        # Coerce lap_id ONCE (used for load AND persisted context stamp).
        lap_id = req.get("lap_id")
        try: lap_id = int(lap_id) if lap_id is not None else None
        except (TypeError, ValueError): lap_id = None
        session_id = req.get("session_id")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()   # unbounded; localhost token rate is the ceiling
        _SENTINEL = object()

        def _emit(ev):
            # Worker may outlive a client disconnect / the test loop; swallow a
            # closed-loop RuntimeError so it dies quietly instead of tracebacking.
            try: loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError: pass

        def worker():
            # ponytail: no cancellation on client disconnect — worker runs the
            # (billed) call to completion regardless. Fine for a localhost
            # single-user app; a stop-flag through the tool loop is deferred.
            #
            # Blocking I/O (store.load, rest.get_setup) lives here, off the event
            # loop, so a hung LMU REST call can't freeze the whole server.
            eff_session_id = session_id
            setup = {}
            rest = _ensure_rest()
            if rest:
                try: setup = rest.get_setup()
                except Exception: setup = {}
            # Orient the coach to the CURRENT track/car/session/map WITHOUT handing it a
            # lap. A pinned lap made the coach fixate on the UI selection; instead it
            # fetches laps itself via list_recent_laps / get_lap_summary as the
            # conversation needs. We still resolve the environment so corner tools and
            # the reference lap work. Priority: live session -> most-recent lap ->
            # the lap the client is viewing (for track/car only, never pinned).
            track = car = cmap = None
            car_class = None
            on_track = None
            try:
                from ..lmu.smm import read_session_track_car
                live = read_session_track_car()          # (track, vehicle) | None
            except Exception:
                live = None
            race_snap = race_note = live_session = None
            live_meta = car_status = None
            live_status = None      # #175: {"state", "speed"} -- is the car moving?
            if live:
                on_track = True
                track, vehicle = live
                try:
                    # #160: the lap being driven and the car's live status, for the
                    # "this lap" reads and get_car_status. Never fails the turn.
                    # Only the LIVE session's buffer: one left from the previous
                    # session (no wrap since) never answers for this one.
                    from ..lmu.smm import read_live_session_id, read_live_status
                    live_status = read_live_status()
                    live_meta, car_status = app.state.live_lap.now(
                        live_status, live=(track, vehicle),
                        session_id=read_live_session_id())
                except Exception:
                    live_meta = car_status = None
                try:
                    # #152: only THIS race's snapshot -- never the last race's, from the
                    # garage or the next race's formation lap.
                    from ..lmu.live import read_field_snapshot
                    sess = live_session = read_field_snapshot().session
                    race_snap, race_note = live_race(
                        app.state.race.snapshot, track=track, car=vehicle,
                        phase=sess.phase, current_lap=sess.current_lap)
                except Exception:
                    race_snap = race_note = None
                try:
                    from ..track_lookup import resolve_context
                    cmap, car, _, car_class = resolve_context(
                        rest, str(app.state.tracks_dir), track, vehicle,
                        user_dir=str(Path(store.data_dir) / "tracks"))
                except Exception:
                    cmap, car = _map_for(track), vehicle
                # #108 item 5: REST didn't have a class (no REST, or no carClass on
                # the payload) -- fall back to the scraped result-log table, still
                # without a second REST call.
                if car_class is None:
                    try:
                        from ..car_lookup import resolve_car_class
                        car_class = resolve_car_class(vehicle)
                    except Exception:
                        car_class = None
            if track is None and app.state.latest is not None:
                lr = app.state.latest["record"]
                track, car = lr.track, resolve_car(lr.car)
                eff_session_id = eff_session_id or lr.session_id
                cmap, on_track = _map_for(track), False
            if track is None and lap_id is not None:
                try:
                    sr = app.state.store.load(lap_id)
                    track, car = sr.track, resolve_car(sr.car)
                    eff_session_id = eff_session_id or sr.session_id
                    cmap, on_track = _map_for(track), False
                except Exception:
                    pass
            # #137: no live SMM, no in-process latest lap, no client-supplied lap_id
            # either -- e.g. LMU sitting in the lobby after a sidecar restart, asked
            # for post-race analysis. Fall back to the store: the client's viewed
            # session first, then whatever lap is newest overall.
            if track is None:
                row = store.latest_lap_row(session_id) or store.latest_lap_row()
                if row is not None:
                    track, car = row["track"], resolve_car(row["car"])
                    eff_session_id = eff_session_id or row["session_id"]
                    cmap, on_track = _map_for(track), False
            # Backfill the current stint id from the latest lap when the client didn't
            # supply one (auto-anchor) — the live path above resolves track/car but not
            # the session stamp. Only when that lap still matches the live track/car
            # (same rule as _status_view's matches_live); on a track change the latest
            # lap is from the OLD track, so use the live session instead (#127).
            if eff_session_id is None and app.state.latest is not None:
                lr = app.state.latest["record"]
                if live is None or (lr.track, lr.car) == live:
                    eff_session_id = lr.session_id
                else:
                    eff_session_id = _live_session_id()
            # No lap captured yet (e.g. a question asked in the garage): stamp the
            # chat with the CURRENT live session so it isn't orphaned — the laps that
            # follow join the same instance and the chat stays resumable via Sessions.
            if eff_session_id is None:
                eff_session_id = _live_session_id()
            cfg = config.load_config()
            ctx = CoachContext(app.state.store, record=None, lap_id=None, cmap=cmap,
                               units=cfg.get("units", "metric"),
                               setup=setup, track=track, car=car, on_track=on_track,
                               allowed_adjustments=(legal_adjustments(setup) if setup else None),
                               session_id=eff_session_id,
                               car_notes=load_car_notes(
                                   car, user_dir=str(Path(store.data_dir) / "cars")),
                               setup_concepts=load_setup_concepts(),
                               car_class=car_class,
                               # #152: only the race being driven now (live_race).
                               race=race_snap, race_note=race_note,
                               live_lap=live_meta, car_status=car_status,
                               # #161: the race length for a pre-race plan.
                               live_session=live_session)
            car = ctx.car or "car"

            # Concise always-on profile tier: static prefs from settings + the active
            # driver_summary only — NOT the full memory list (that's a pull tool, later
            # task). Best-effort: a missing/empty summary just means no learned_notes.
            d = cfg.get("driver") or {}
            try:
                summary = app.state.store.get_driver_summary()
            except Exception:
                summary = {}
            notes = [summary["text"]] if summary.get("status") == "active" and summary.get("text") else []
            driver_profile = DriverProfile(
                braking_style=d.get("braking_style") or None,
                balance_preference=d.get("balance_preference") or None,
                risk_tolerance=d.get("risk_tolerance") or None,
                learned_notes=notes,
            )

            # Thread the chat's prior turns so the coach continues the conversation
            # instead of treating each question standalone (the system prompt tells it
            # "the messages above are what you and the driver already said").
            history: list[dict] = []
            prior_results = ""
            last_chat_ts: float | None = None   # #131: this chat's last-turn timestamp
            chat_id_in = req.get("chat_id")
            if chat_id_in:
                try:
                    messages = app.state.store.chat_messages(chat_id_in)
                except Exception:
                    messages = []
                try:
                    # #136: which race each earlier turn was about. A chat is meant
                    # to span the races of a sitting (they share one session id), so
                    # without this an answer about race 1 re-enters the context as
                    # the model's own prior reasoning about the race being driven now.
                    race_windows = ctx._race_windows()
                    # #167: a chat also spans sessions, tracks and weekends (it only
                    # ends on New chat) -- a turn from another session is labelled
                    # with that session instead. One lap query per other session.
                    # Unknown on either side (no session resolved, a pre-session
                    # row): nothing to compare, treated as this session.
                    def _other(row):
                        sid = row.get("session_id")
                        return bool(sid and eff_session_id and sid != eff_session_id)
                    other_laps: dict[str, list[dict]] = {}
                    for row in messages:
                        sid = row.get("session_id")
                        if _other(row):
                            if sid not in other_laps:
                                # A failed lap read costs that label its phase, never
                                # the rest of the history.
                                try:
                                    other_laps[sid] = app.state.store.session_laps(sid)
                                except Exception:
                                    other_laps[sid] = []
                            note = session_turn_note(row, other_laps[sid])
                        else:
                            note = race_turn_note(row["ts"], race_windows)
                        if row.get("question"):
                            history.append({"role": "user", "content": row["question"]})
                        if row.get("answer"):
                            # scrub_stored_answer, not the raw row: an older answer can
                            # assert a fabricated grip reading (#72) or a race start the
                            # coach could not know (#67) as established fact, and
                            # resuming the chat would re-feed it to the model as its own
                            # prior reasoning.
                            history.append({"role": "assistant",
                                            "content": note
                                            + scrub_stored_answer(row["answer"],
                                                                  row["ts"],
                                                                  row.get("track"))})
                    last_chat_ts = messages[-1]["ts"] if messages else None
                    # #159: what earlier turns' tools measured (not what the coach
                    # said) -- a figure repeated verbatim from these is grounded.
                    # Only THIS session's (#167): another session's tyre temps or
                    # lap times are not a known figure for the race being driven now.
                    prior_results = "\n".join(
                        str(c.get("result", "")) for row in messages
                        if not _other(row) and isinstance(row.get("tool_calls"), list)
                        for c in row["tool_calls"] if isinstance(c, dict))
                except Exception:
                    history = []
                    prior_results = ""

            # #131: "that lap"/"how was that" should resolve to a lap that landed
            # after this chat's last turn without the coach asking which one. Only
            # meaningful when there WAS a last turn (last_chat_ts set above) --
            # compares stored ts, no LLM guess.
            new_since_last_turn: str | None = None
            if eff_session_id and last_chat_ts is not None:
                sess_laps = app.state.store.session_laps(eff_session_id)
                if sess_laps:
                    latest = sess_laps[-1]
                    if latest["ts"] > last_chat_ts:
                        label_by_id, _ = ctx._lap_numbering()
                        label = label_by_id.get(latest["id"]) or f"lap {latest['id']}"
                        # A partial/out/in lap has no time: name its kind instead.
                        detail = (format_laptime(latest["lap_time"]) if latest.get("lap_time")
                                  else _PARTIAL_LIST_WORD.get(latest.get("partial"), "no time"))
                        new_since_last_turn = (
                            f"{label} ({detail}) landed {_lap_age(latest['ts'])}.")
            # The facts ride in the USER turn, not the system prompt: the system block is
            # prompt-cached and a per-turn value there would bust the cache every question.
            # The stored question (below) stays the driver's own words.
            # #136's CURRENT RACE line leads: it frames every number in the turn, including
            # the lap the NEW SINCE line names. Both are silent when there is nothing to say.
            windows = ctx._race_windows()
            facts = [f for f in (current_race_fact(windows),
                                 f"NEW SINCE YOUR LAST ANSWER: {new_since_last_turn}"
                                 if new_since_last_turn else "") if f]

            text = ""
            # What this turn billed. One usage event arrives per model call, so a
            # tool-using question contributes several — summing them here is what
            # makes the ×N tool-loop re-send visible instead of invisible.
            turn_usage = TokenUsage()
            used_model: str | None = None
            # #159: a bare "say again" / "what?" replays the last answer verbatim -- no
            # model call, so no chance to re-word (or re-invent) a figure.
            # (history is non-empty only when `messages` was read above; the stored
            # answer, not the scrubbed history copy.)
            last_answer = (messages[-1]["answer"] if history and history[-1]["role"] == "assistant"
                           and is_repeat_request(question) else None)
            # #175: how hard to think -- code gates, then the classifier (<= 800 ms),
            # then keyword rules. Its tokens bill with this turn. ponytail: priced at
            # the coach model's rate (one model per chat row) -- overstates a sub-cent
            # classifier call; split it into its own row if that ever matters.
            tier, intent, hint = "standard", None, ""
            if not last_answer:
                coach_cfg = cfg.get("coach") or {}
                moving = bool(live_status and live_status.get("state") == "track"
                              and (live_status.get("speed") or 0) >= MOVING_KPH)
                race_live = ctx.race is not None or bool(ctx.race_note)
                race_finished = bool(windows) and not race_live
                where = ("on track, moving" if moving else "on track, stationary"
                         if live_status and live_status.get("state") == "track"
                         else (live_status or {}).get("state") or "not in a session")
                classifier = None
                classifier_for = getattr(app.state.coach, "classifier_for", None)
                if classifier_for is not None:
                    try:
                        classifier = classifier_for(coach_cfg.get("classifier_model"))
                    except Exception as e:
                        logging.getLogger(__name__).info("no classifier: %s", e)
                try:
                    laps_n = len(store.session_laps(eff_session_id)) if eff_session_id else 0
                except Exception:
                    laps_n = 0
                state = (f"{where}; session phase {getattr(live_session, 'phase', '') or 'unknown'}; "
                         f"race {'live' if race_live else 'finished' if race_finished else 'none'}; "
                         f"voice {voice or 'typed'}; {laps_n} laps this session")
                tier, intent, hint, classify_usage = route(
                    question, setting=coach_cfg.get("thinking", "auto"), moving=moving,
                    race_finished=race_finished, classifier=classifier, history=history,
                    state=state, cmap=ctx.cmap)
                turn_usage = turn_usage + classify_usage
            model_question = "\n\n".join([*facts, *([hint] if hint else []), question])
            try:
                for ev in ([{"type": "text", "text": last_answer}] if last_answer else
                           app.state.coach.ask_agentic_stream(
                        ctx, model_question, car=car, input_device=req.get("input_device"),
                        history=history, units=ctx.units, driver_profile=driver_profile,
                        voice=voice, prior_results=prior_results,
                        thinking=tier, intent=intent)):
                    if ev["type"] == "usage":
                        # Keep the per-turn breakdown server-side (noise on the wire);
                        # the client gets one cumulative figure on `done`. The model id
                        # comes from the event, i.e. what actually billed — NOT from
                        # config, which may have changed since the model was resolved.
                        turn_usage = turn_usage + TokenUsage(**ev["usage"])
                        used_model = ev.get("model") or used_model
                        continue
                    if ev["type"] == "text":
                        text += ev["text"]       # accumulate streamed deltas into the full answer
                    _emit(ev)
                terminal = {"type": "done", "answer": text}
            except Exception as e:
                # Surface our own curated user-facing errors verbatim; hide any
                # other internal error text (and generic RuntimeError/ValueError)
                # from the client.
                msg = str(e) if isinstance(e, CoachUserError) else "coach error"
                terminal = {"type": "error", "message": msg}
            # Persist best-effort, but only when there's a real turn to keep —
            # skip the row when an error produced no text, so failed turns don't
            # pollute history with an empty answer.
            if terminal["type"] == "done" or text:
                try:
                    chat_id = chat_id_in or app.state.store.create_chat()
                    stored_question = ("🎤 " + question) if voice else question
                    app.state.store.save_chat(chat_id, ctx.session_id, ctx.track, ctx.car, ctx.lap_id,
                                              stored_question, text, ctx.tool_calls,
                                              usage=turn_usage.as_dict(), model=used_model)
                    terminal["chat_id"] = chat_id
                    # Report the CHAT cumulative total, read back from the store after
                    # the insert: the number the driver sees is then the persisted
                    # truth rather than a drifting in-memory counter, and it survives
                    # a sidecar restart. Still inside the swallowing try — a pricing
                    # or aggregate failure costs the meter, never the answer.
                    terminal.update(_usage_view(app.state.store.chat_usage(chat_id)))
                    terminal["model"] = used_model
                    # Soft long-chat nudge: the core decides, the UI only renders it.
                    # A SOFT signal — nothing is summarized, windowed or truncated;
                    # the driver chooses whether to start a New chat. 0 disables it.
                    soft = cfg.get("chat_token_soft_limit") or 0
                    terminal["long_chat"] = bool(soft) and terminal["usage"]["total"] >= soft
                except Exception as e:
                    # Still best-effort (the answer must reach the driver either way),
                    # but no longer silent: a failure here loses the turn from history
                    # AND leaves the meter showing the pre-turn total with no sign
                    # anything went wrong (#5). Log it, and tell the client so the UI
                    # can show "usage unavailable" instead of a stale number.
                    logging.getLogger(__name__).warning("coach turn not persisted", exc_info=True)
                    terminal["usage_error"] = str(e)
            _emit(terminal)
            _emit(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()
        try:
            while True:
                ev = await q.get()
                if ev is _SENTINEL:
                    break
                await ws.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass          # client left mid-stream; worker still finishes + persists (known ceiling)
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try: await ws.close()
                except Exception: pass
