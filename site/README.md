# Nestor marketing site

A single static page (`index.html` + `assets/`) served by Caddy. No build step, no
runtime dependencies — the page is plain HTML with inline CSS and Google Fonts.

## Contents

```
site/
  index.html              the page (inline CSS, no JS deps)
  assets/
    helm-color.svg        primary helm mark (colour)
    helm.svg              one-ink helm silhouette (favicon)
    lmu-logo.png          Le Mans Ultimate mark (public domain, Wikimedia)
    coach-app.png         hero — full app, live coaching session
    coach-chat.png        the coaching conversation (crop of the hero)
    track-map.png         enlarged track map with numbered corners
    sessions-trend.png    Sessions screen — best-lap trend
    brand/                raster brand kit (marks, wordmark lockups, OG card) + its own README
  Dockerfile              Caddy static server for Railway (binds $PORT)
  Caddyfile
  tools/                  asset generators (not deployed) — see tools/README.md
```

The four app screenshots are **real captures of the running app**, not mockups.

## Preview locally

```sh
cd site
python -m http.server 8080          # open http://localhost:8080
# or test the real container:
docker build -t nestor-site . && docker run --rm -p 8080:80 nestor-site
```

## Edit before launch

- **OG image URL** — the `og:image` / `twitter:image` metas are relative. For
  reliable link previews, make them absolute once the domain is set, e.g.
  `https://nestor.jaysontrades.com/assets/brand/og-image.png`.

## Refreshing the app screenshots

These come from the **running desktop app**, so recapture them whenever the UI
changes (or to catch a live, LMU-connected session — the status pill reads
`● ON TRACK` only while LMU is running).

1. Start the app in dev so the FastAPI sidecar (`127.0.0.1:8765`) and the Svelte
   frontend (`localhost:5173`) are up — `cargo tauri dev` from the repo root does
   both. The frontend **must** be on port **5173**: the sidecar's CORS only allows
   that origin.
2. Open `http://localhost:5173` in a browser at **1440×900**, dark theme
   (`localStorage.setItem('nestor-theme','dark')`).
3. Capture, saving into `site/assets/`:
   - **coach-app.png** — Sessions → pick a session → select the best lap (corners +
     map populate); a chat transcript in the session fills the right pane.
   - **coach-chat.png** — the chat pane only: crop `coach-app.png` to `x ≥ 398`.
   - **track-map.png** — click the map's magnifier (“Enlarge track map”).
   - **sessions-trend.png** — Sessions screen with a track selected (draws the
     best-lap trend). No status pill here, so it needn't be recaptured for “live”.

## Regenerating the brand assets

Deterministic — see `tools/README.md`. In short:

```sh
cd site/tools && npm install
node render-marks.mjs                       # helm-mark-*.png (light + dark, 256/512/1024)
# wordmark lockups + OG card: serve the site and screenshot tools/render.html
cd .. && python -m http.server 8080         # open /tools/render.html, shoot #wm-dark/#wm-light/#og
```

## Republishing the Claude Artifact preview

The shareable preview (mobile-friendly) is a single self-contained file:

```sh
python tools/inline-artifact.py             # -> tools/dist/nestor-artifact.html
```

Publish that file as an Artifact (Claude Code’s Artifact tool). Republishing the same
file path keeps the same URL.

## Deploy to Railway

The **nestor** service is connected to this GitHub repo and deploys itself — no
manual step per change:

- **Source:** `javon27/sim-racing-coach`, branch `main`, **Root Directory** `/site`.
  Railway builds `site/Dockerfile` (Caddy, listening on `$PORT`).
- **Watch paths:** `/site/**` — a push to `main` deploys **only** when files under
  `site/` change. Commits that touch just the app (`simcoach/`, `frontend/`, …) are
  skipped, so the site isn't rebuilt for unrelated work.
- **App sleep:** off, so the public page has no cold-start delay.

**To ship a site change:** commit under `site/` and push to `main`. Railway builds
and deploys automatically. Live at `https://nestor-production-83cb.up.railway.app`.

Manual deploy (rarely needed — e.g. redeploy without a commit): from this folder,
`railway up --service nestor --ci`. Reconfigure source/root/watch-paths via the
service's **Settings → Source**, or `railway link` then the Railway MCP/CLI.

## Custom domain (CNAME under jaysontrades.com)

1. Railway → the service → **Settings → Networking → Custom Domain**.
2. Enter a subdomain, e.g. `nestor.jaysontrades.com`. Railway shows a **CNAME
   target** (`<something>.up.railway.app`).
3. At your DNS provider for `jaysontrades.com`, add a **CNAME**: `nestor` → that
   target. (An apex like `jaysontrades.com` can't be a CNAME — use a subdomain, or
   your DNS provider's ALIAS/flattening.)
4. Wait for DNS to propagate; Railway issues the TLS cert automatically.
