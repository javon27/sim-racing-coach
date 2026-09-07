// Rasterize the helm mark to transparent square PNGs for the brand/ folder.
//   light = plain colour mark (reads on light backgrounds)
//   dark  = white-outlined mark (separates on dark backgrounds)
// Usage:  cd site/tools && npm install && node render-marks.mjs
import sharp from "sharp";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const SRC = here("../assets/helm-color.svg");
const OUT = here("../assets/brand");
const TRANSPARENT = { r: 0, g: 0, b: 0, alpha: 0 };
const sizes = [256, 512, 1024];

const colorSvg = readFileSync(SRC, "utf8");

// Bake a uniform white ring (dilate the silhouette, flood white, merge under art)
// — the static equivalent of the app's dark-mode CSS outline.
const outlinedSvg = colorSvg.replace(
  /(<svg\b[^>]*>)([\s\S]*)<\/svg>\s*$/,
  (_m, open, body) =>
    `${open}<defs><filter id="ol" x="-25%" y="-25%" width="150%" height="150%" color-interpolation-filters="sRGB">` +
    `<feMorphology in="SourceAlpha" operator="dilate" radius="13" result="d"/>` +
    `<feFlood flood-color="#ffffff" flood-opacity="1" result="w"/>` +
    `<feComposite in="w" in2="d" operator="in" result="ring"/>` +
    `<feMerge><feMergeNode in="ring"/><feMergeNode in="SourceGraphic"/></feMerge>` +
    `</filter></defs><g filter="url(#ol)">${body}</g></svg>`
);

const variants = { light: Buffer.from(colorSvg), dark: Buffer.from(outlinedSvg) };

for (const [variant, svg] of Object.entries(variants)) {
  for (const size of sizes) {
    const inner = Math.round(size * 0.84); // small margin
    const mark = await sharp(svg, { density: 340 })
      .resize({ height: inner, fit: "contain", background: TRANSPARENT })
      .png()
      .toBuffer();
    const m = await sharp(mark).metadata();
    await sharp({ create: { width: size, height: size, channels: 4, background: TRANSPARENT } })
      .composite([{ input: mark, left: Math.round((size - m.width) / 2), top: Math.round((size - m.height) / 2) }])
      .png({ compressionLevel: 9 })
      .toFile(`${OUT}/helm-mark-${variant}-${size}.png`);
    console.log(`helm-mark-${variant}-${size}.png`);
  }
}
