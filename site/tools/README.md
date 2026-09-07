# Site asset tools

Generators for the marketing site's images and the Claude Artifact preview. These
are **not** part of the deployed page — Railway serves only `index.html` + `assets/`.

`node_modules/` and `dist/` here are git-ignored.

## `render-marks.mjs` — helm mark PNGs

Rasterizes `../assets/helm-color.svg` into the transparent square icons in
`../assets/brand/` (light = plain, dark = white-outline baked in), at 256/512/1024.
The white ring is added in-script via an SVG dilate-and-flood filter — the static
equivalent of the app's dark-mode CSS outline.

```sh
cd site/tools
npm install        # pulls sharp (prebuilt binaries)
node render-marks.mjs
```

## `render.html` — wordmark lockups + OG card

The `NEST<helm>R` lockups and the 1200×630 social card, rendered from the site's own
Chakra Petch CSS so they always match the page. It's a browser render (real font), so
capture is a screenshot step:

```sh
cd site && python -m http.server 8080      # then open http://localhost:8080/tools/render.html
```

Screenshot each block to `../assets/brand/`: `#wm-dark` → `wordmark-dark.png`,
`#wm-light` → `wordmark-light.png`, `#og` → `og-image.png` (capture the element so
it's exactly 1200×630). Any element-screenshot tool works (browser devtools "capture
node", a headless driver, etc.).

## `inline-artifact.py` — self-contained HTML

Inlines every `assets/…` reference in `../index.html` as a `data:` URI and strips the
outer html/head/body, producing one file suitable for publishing as a Claude Artifact
(which can't reference local files).

```sh
python tools/inline-artifact.py            # -> tools/dist/nestor-artifact.html
```

## The app screenshots

`coach-app.png`, `coach-chat.png`, `track-map.png`, `sessions-trend.png` are captured
from the **running app**, not scripted here — see the parent `../README.md`
(“Refreshing the app screenshots”).
