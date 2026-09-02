"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import math
from dataclasses import asdict

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, paths, secrets
from ..analysis import analyze_lap
from ..model.factory import make_model

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # port stripped before compare


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
        host = (request.headers.get("host") or "").split(":")[0]
        if host and host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

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
        except Exception:
            raise HTTPException(404, "lap not found")
        ref = store.best_lap(rec.track, rec.car, exclude_lap_id=lap_id)
        a = analyze_lap(rec, app.state.cmap, reference=ref)
        return _clean({
            "lap_time": a.lap_time,
            "corners": [asdict(c) for c in a.corners],
        })

    @app.get("/setup")
    def setup():
        # Phase 1: raw dict from LmuRest, unfiltered. Filtering to the
        # human-meaningful subset is deferred to the Setup-panel phase (Phase 4).
        return app.state.rest.get_setup() if app.state.rest else {}

    @app.get("/chats")
    def chats():
        return store.list_chats()

    @app.get("/chats/{chat_id}")
    def chat_messages(chat_id: str):
        return store.chat_messages(chat_id)

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
