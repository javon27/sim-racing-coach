# Nestor marketing site

A single static page (`index.html` + `assets/`) served by Caddy. No build step.

## Contents

- `index.html` — the page (inline CSS, no dependencies).
- `assets/` — `helm-color.svg` / `helm.svg` (brand marks), `lmu-logo.png`
  (public-domain LMU mark), and the three product screenshots
  (`coach-app.png`, `track-map.png`, `sessions-trend.png`) — all real captures of
  the actual app.
- `Dockerfile` + `Caddyfile` — static hosting for Railway (binds `$PORT`).

## Before it goes live

- **Patreon link:** `index.html` has a placeholder CTA marked
  `data-patreon-placeholder` (search for `TODO`). Swap `href="#"` for the real
  Patreon URL once the account exists, and drop the `soon` badges.

## Preview locally

```sh
cd site
python -m http.server 8080     # then open http://localhost:8080
# or, to test the real container:
docker build -t nestor-site . && docker run --rm -p 8080:80 nestor-site
```

## Deploy to Railway

1. **New service → Deploy from GitHub repo** (this repo).
2. In the service **Settings → Build**, set **Root Directory** to `site/`.
   Railway detects the `Dockerfile` and builds it (Caddy listens on `$PORT`).
3. Deploy. You’ll get a `*.up.railway.app` URL to sanity-check.

CLI alternative from this folder: `railway up`.

## Custom domain (CNAME under jaysontrades.com)

1. Railway → the service → **Settings → Networking → Custom Domain**.
2. Enter the subdomain you want, e.g. `nestor.jaysontrades.com`. Railway shows a
   **CNAME target** (looks like `<something>.up.railway.app`).
3. At your DNS provider for `jaysontrades.com`, add a **CNAME** record:
   `nestor` → the target Railway gave you. (Apex `jaysontrades.com` can’t be a
   CNAME — use a subdomain, or your DNS provider’s ALIAS/flattening.)
4. Wait for DNS to propagate; Railway issues the TLS cert automatically. Done.
