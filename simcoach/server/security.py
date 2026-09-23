"""Host/Origin guard for the localhost sidecar (DNS-rebinding + CSRF). Moved from app.py (#113)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

# Host-header allowlist (port stripped before compare) -- AND an input to
# ALLOWED_ORIGINS below, which is a cross product of these hostnames with our two
# local ports. Adding a hostname here to accept a new Host therefore also grants
# it two ORIGINS, widening the CSRF guard without anyone editing it: adding
# "tauri.localhost" (plausible -- it is already an allowed origin host) would
# auto-admit http://tauri.localhost:5173 and :8765. Widen this set deliberately.
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SIDECAR_PORT = 8765   # the port simcoach/server/__main__.py serves on (it imports this)
VITE_DEV_PORT = 5173  # `npm run dev` / src-tauri/tauri.conf.json devUrl
# #54: Tauri's webview origin is platform-dependent. Windows uses
# "http://tauri.localhost"; macOS/Linux use "https://tauri.localhost" and
# "tauri://localhost". Listing all three costs nothing on Windows and avoids a
# CORS-shaped "server not reachable" misdiagnosis on the other platforms.
FRONTEND_ORIGINS = ["http://localhost:5173", "http://tauri.localhost",
                     "https://tauri.localhost", "tauri://localhost"]

# FULL origins (scheme + host + port), never bare hostnames. Comparing the
# hostname alone let ANY app on ANY localhost port through the guard (#60) --
# another local dev server, or an XSS in one, could drive every guarded route,
# which is exactly what the guard exists to stop. Two sources, no invention:
#   - FRONTEND_ORIGINS: the Vite dev origin (src-tauri/tauri.conf.json devUrl)
#     and the three Tauri webview origins above (#54).
#   - our two local ports (sidecar + Vite dev) under BOTH names ALLOWED_HOSTS
#     accepts as a Host. 127.0.0.1 and localhost are the same machine and the
#     same server: accepting Host "localhost:8765" while refusing Origin
#     "http://localhost:8765" would be incoherent, and the same argument applies
#     to the dev port -- hence the cross product rather than four literals, so
#     the host list and the port list cannot drift apart.
# NOT auto-widened: if Vite falls back to 5174 (5173 already taken) or you use
# `vite preview` on 4173, that origin is refused with a 403 -- add the port here
# deliberately. Allowing a range would hand the guard back to any local app.
ALLOWED_ORIGINS = set(FRONTEND_ORIGINS) | {
    f"http://{h}:{p}" for h in ALLOWED_HOSTS for p in (SIDECAR_PORT, VITE_DEV_PORT)
}


def _bad_origin(origin: str | None) -> bool:
    """True if a browser Origin is present AND isn't an exact ALLOWED_ORIGINS
    match. A missing Origin (native/Tauri client, or a non-browser client) is
    allowed — only a browser sets Origin, and that's the cross-site-WS/CSRF
    vector we're blocking; no browser omits Origin on a mutating request.

    Compares scheme+host+port, not just the host (#60). Exact string match after
    lowercasing: scheme and host are case-insensitive, and a browser never sends
    a path, a trailing slash, or an explicit default port on an Origin — so
    anything that isn't the literal allowed origin is refused. That also refuses
    "null", "file://" and bare "http://[::1]" for free, as before."""
    if not origin:
        return False
    return origin.lower() not in ALLOWED_ORIGINS


def install(app) -> None:
    # add_middleware inserts at index 0, so this call must come BEFORE _host_guard
    # below to make _host_guard the OUTERMOST middleware -- it sees OPTIONS
    # preflights before CORSMiddleware does. Security doesn't depend on the
    # ordering (either order refuses), but the insert-at-0 semantics are easy to
    # get backwards in a later tidy-up.
    app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS,
                        allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        # Also the CSRF guard for every HTTP route, and the only one that doesn't
        # depend on a library default. CORSMiddleware decides whether the browser
        # hands the RESPONSE back to script -- a CORS-simple request is sent and
        # EXECUTED regardless, so CORS can never prevent a side effect. Refusing a
        # disallowed Origin here can. Deliberately NOT filtered to mutating
        # methods: any request that CARRIES a disallowed Origin (all mutating
        # methods, plus a cors-mode GET) has no legitimate caller and is refused.
        # BUT a browser does not attach Origin to a no-cors GET -- <img>,
        # <script>, <link>, <iframe>, and top-level navigation all send none --
        # so this guard does NOT stop a same-window `<img src="/setup">`-style
        # read; _bad_origin sees no Origin and allows it through by design (a
        # MISSING Origin must stay allowed so native/Tauri and non-browser
        # clients work). That used to matter because GET /setup wrote to the DB
        # (save_session_setup); fixed by #51 -- persistence now lives behind
        # POST /setup/capture, so a no-Origin GET here is read-only and has no
        # side effect for this guard to miss.
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)
