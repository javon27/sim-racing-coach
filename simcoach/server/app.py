"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import asyncio
import math
import threading
import urllib.parse
from dataclasses import asdict

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config, paths, secrets
from ..analysis import analyze_lap
from ..coach_tools import CoachContext
from ..model.factory import CoachUserError, make_model
from ..tiers import legal_adjustments

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # port stripped before compare


def _bad_origin(origin: str | None) -> bool:
    """True if a browser Origin is present AND its host isn't allowed. A missing
    Origin (native/Tauri client, or a non-browser client) is allowed — only a
    browser sets Origin, and that's the cross-site-WS vector we're blocking.
    Port-blind by design (any localhost port is fine for a local desktop app)."""
    if not origin:
        return False
    host = (urllib.parse.urlparse(origin).hostname or "").lower()
    return host not in ALLOWED_HOSTS


def _clean(v):
    # CornerMetrics.min_spd et al. can be float('nan') on real data;
    # NaN/Inf are invalid JSON, so map non-finite floats to None.
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def create_app(store, coach, cmap=None, rest=None) -> FastAPI:
    app = FastAPI(title="SimCoach", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.coach = coach
    app.state.cmap = cmap
    app.state.rest = rest

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/status")
    def status():
        track = car = None
        try:
            from ..lmu.smm import read_session_track_car
            tc = read_session_track_car()
            if tc:
                track, car = tc
        except Exception:
            track = car = None
        return {
            "track": track, "car": car,
            "on_track": False,               # no live capture in the server yet
            "has_map": app.state.cmap is not None,
            "lmu_connected": app.state.rest is not None,
        }

    @app.get("/sessions")
    def sessions():
        return store.recent_race_sessions()

    @app.get("/sessions/{sid}/laps")
    def session_laps(sid: str):
        return store.session_laps(sid)

    @app.get("/laps/{lap_id}/analysis")
    def lap_analysis(lap_id: int):
        if app.state.cmap is None:
            raise HTTPException(404, "no corner map")
        try:
            rec = store.load(lap_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "lap not found")
        try:
            ref = store.best_lap(rec.track, rec.car, exclude_lap_id=lap_id)
            a = analyze_lap(rec, app.state.cmap, reference=ref)
        except Exception:
            raise HTTPException(422, "lap telemetry could not be analyzed")
        return _clean({
            "lap_time": a.lap_time,
            "corners": [asdict(c) for c in a.corners],
        })

    @app.get("/setup")
    def setup():
        # Phase 1: raw dict from LmuRest, unfiltered. Filtering to the
        # human-meaningful subset is deferred to the Setup-panel phase (Phase 4).
        if not app.state.rest:
            return {}
        try:
            return app.state.rest.get_setup()
        except Exception:
            return {}

    @app.get("/chats")
    def chats():
        return store.list_chats()

    @app.get("/chats/{chat_id}")
    def chat_messages(chat_id: str):
        return store.chat_messages(chat_id)

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
            record = None
            if lap_id is not None:
                try: record = app.state.store.load(lap_id)
                except Exception: record = None
            setup = {}
            if app.state.rest:
                try: setup = app.state.rest.get_setup()
                except Exception: setup = {}
            ctx = CoachContext(app.state.store, record=record, lap_id=lap_id, cmap=app.state.cmap,
                               setup=setup, on_track=record is not None,
                               allowed_adjustments=(legal_adjustments(setup) if setup else None),
                               session_id=session_id)
            car = ctx.car or "car"

            text = ""
            try:
                for ev in app.state.coach.ask_agentic_stream(
                        ctx, question, car=car, input_device=req.get("input_device")):
                    if ev["type"] == "text":
                        text += ev["text"]       # accumulate streamed deltas into the full answer
                    _emit(ev)
                terminal = {"type": "done"}
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
                    chat_id = req.get("chat_id") or app.state.store.create_chat()
                    app.state.store.save_chat(chat_id, ctx.session_id, ctx.track, ctx.car, lap_id,
                                              question, text, ctx.tool_calls)
                    terminal["chat_id"] = chat_id
                except Exception:
                    pass
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

    def _settings_view() -> dict:
        try:
            from importlib.metadata import version
            app_version = version("simcoach")
        except Exception:
            app_version = "dev"
        cfg = config.load_config()
        cfg.update({
            "api_key_set": secrets.has_api_key(),
            "data_dir": str(paths.app_data_dir()),
            "app_version": app_version,
            "lmu_build": None,
        })
        return cfg

    @app.get("/settings")
    def get_settings():
        return _settings_view()

    @app.put("/settings")
    def put_settings(body: dict = Body(...)):
        # Whitelist to config.DEFAULTS keys before saving — this is the
        # secret-leak guard: an injected "key"/"api_key" field can never
        # reach the JSON file on disk.
        config.save_config({k: body[k] for k in config.DEFAULTS if k in body})
        return _settings_view()

    @app.post("/settings/api-key")
    def set_api_key(body: dict = Body(...)):
        key = (body.get("key") or "").strip()
        if not key:
            raise HTTPException(400, "key is required")
        secrets.set_api_key(key)
        return {"api_key_set": True}

    @app.post("/settings/test-key")
    def test_key():
        try:
            cfg = config.load_config()
            model = make_model(model=cfg.get("model"))
            model.complete("ping", [{"role": "user", "content": "ping"}])
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/data")
    def delete_data(body: dict = Body(default={})):
        if not body.get("confirm"):
            raise HTTPException(400, "confirmation required")
        store.clear_all()
        return {"cleared": True}

    return app
