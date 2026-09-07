# Nestor brand assets (raster)

The **SVG** marks in `../helm-color.svg` / `../helm.svg` are the masters — use them
wherever SVG works (web, app). These PNGs are for raster-only surfaces (social
cards, avatars, app/store icons) where SVG or a live CSS outline isn't available.

| File | Use | Notes |
|------|-----|-------|
| `helm-mark-light-{256,512,1024}.png` | Icon/avatar on **light** backgrounds | Transparent, square, colour mark |
| `helm-mark-dark-{256,512,1024}.png`  | Icon/avatar on **dark** backgrounds  | Transparent, square, **white outline baked in** |
| `wordmark-light.png` | NEST·helm·R lockup on light backgrounds | Dark text, white ground |
| `wordmark-dark.png`  | NEST·helm·R lockup on dark backgrounds  | Light text + outlined helm, dark ground |
| `og-image.png` | Social / link-preview card | 1200×630, wired into `index.html` `og:image` |

Pick **light** vs **dark** by the background you're placing it on. On dark
surfaces the near-black helmet shell needs the white outline to separate; on
light surfaces use the plain (light) version.

Regenerate: `helm-mark-*` and the outlined SVG come from `helm-color.svg` via
sharp; the wordmark lockups and OG card are rendered from the page's own CSS
(Chakra Petch) so they always match the site.
