// Rasterise one ascii-magic-art piece for the wall (see ascii_magic.py).
//   node ascii_magic.mjs <repo> meta <slug...>                    -> JSON {slug: {name, note}}
//   node ascii_magic.mjs <repo> render <slug> <frames> <fps> <fit|reflow>  -> raw rgb24 128x192 frames
// Each character cell becomes a block of its palette colour, scaled by how much ink its glyph covers:
// at 128 px wide a glyph is 1-2 px, so its shape is gone and only its weight and colour remain.
const [repo, cmd, ...args] = process.argv.slice(2);
const { cover } = await import(`${repo}/runtime/glyphs.js`);
const W = 128, H = 192, CELL = 1 / 0.602;   // a character cell is 1.66x taller than wide (runtime/canvas.js)
const load = (slug) => import(`${repo}/pieces/${slug}.js`);

if (cmd === "meta") {
  const out = {};
  for (const slug of args) { const { meta } = await load(slug); out[slug] = { name: meta.name, note: meta.note || "" }; }
  process.stdout.write(JSON.stringify(out));
} else {
  const [slug, frames, fps, mode] = args;
  const mod = await load(slug), meta = mod.meta;
  // reflow: most pieces lay out from meta.cols/rows, so a portrait grid redraws them to fill the wall
  if (mode === "reflow" && meta.cols / CELL / meta.rows > W / H) meta.rows = Math.round(meta.cols / CELL / (W / H));
  const draw = mod.default(), pal = meta.palette || {};
  const rgb = (h) => (h ? [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) : [228, 228, 231]);
  const cw = Math.min(W / meta.cols, H / (meta.rows * CELL)), ch = cw * CELL;   // fit, centred
  const ox = (W - cw * meta.cols) / 2, oy = (H - ch * meta.rows) / 2;
  const buf = Buffer.alloc(W * H * 3);
  for (let i = 0; i < +frames; i++) {
    const f = draw(i / fps), T = (f.text ?? f).split("\n").map((r) => [...r]);
    const C = f.color ? f.color.split("\n").map((r) => [...r]) : null;
    buf.fill(0);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const cx = Math.floor((x + 0.5 - ox) / cw), cy = Math.floor((y + 0.5 - oy) / ch);
      const g = T[cy]?.[cx];
      if (!g || g === " ") continue;
      const k = C?.[cy]?.[cx], c = rgb(k && k !== " " ? pal[k] : null), a = Math.sqrt(cover(g) / 100);
      const o = (y * W + x) * 3;
      buf[o] = c[0] * a; buf[o + 1] = c[1] * a; buf[o + 2] = c[2] * a;
    }
    if (!process.stdout.write(Buffer.from(buf))) await new Promise((r) => process.stdout.once("drain", r));
  }
}
