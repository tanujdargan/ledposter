/* DotRenderer: draws a w*h RGB frame as the wall would look: soft round dots on
   black (or flat pixels). Frames from the engine and the media cache are stored
   gamma-corrected (linear PWM duty), so an inverse-gamma LUT is applied for the
   screen; client-side emulations of a source file pass gammaCorrected=false.
   Cheap enough for 15fps on a phone: one putImageData at wall size, one scaled
   drawImage, one pattern-filled mask. */
class DotRenderer {
  constructor(canvas, w, h, gamma = 2.2) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false });
    this.off = document.createElement("canvas");
    this.mask = document.createElement("canvas");
    this.style = localStorage.getItem("previewStyle") || "dots";
    this.setGamma(gamma);
    this.setSize(w, h);
    this.avg = [0, 0, 0];
    this.last = null;
    new ResizeObserver(() => this._fit()).observe(canvas);
  }
  setGamma(g) {
    this.lut = new Uint8ClampedArray(256);
    for (let i = 0; i < 256; i++) this.lut[i] = Math.round(Math.pow(i / 255, 1 / g) * 255);
  }
  setSize(w, h) {
    if (this.w === w && this.h === h) return;
    this.w = w; this.h = h;
    this.off.width = w; this.off.height = h;
    this.octx = this.off.getContext("2d", { alpha: false });
    this.img = this.octx.createImageData(w, h);
    this.cell = 0;
    this._fit();
  }
  setStyle(s) { this.style = s; localStorage.setItem("previewStyle", s); if (this.last) this._blit(); }
  _fit() {
    const css = this.canvas.clientWidth;
    if (!css) return;
    const cell = Math.max(1, Math.floor(css * (window.devicePixelRatio || 1) / this.w));
    if (cell === this.cell) return;
    this.cell = cell;
    this.useMask = cell >= 4;  // smaller cells cannot hold a round dot: fall back to flat pixels
    this.canvas.width = cell * this.w; this.canvas.height = cell * this.h;
    // One dot tile, repeated: black with a soft round hole.
    const t = document.createElement("canvas");
    t.width = t.height = cell;
    const c = t.getContext("2d");
    c.fillStyle = "#000"; c.fillRect(0, 0, cell, cell);
    if (this.useMask) {
      c.globalCompositeOperation = "destination-out";
      const r = cell / 2, g = c.createRadialGradient(r, r, 0, r, r, r * 0.95);
      g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(0.55, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
      c.fillStyle = g; c.fillRect(0, 0, cell, cell);
    }
    this.mask.width = this.canvas.width; this.mask.height = this.canvas.height;
    const m = this.mask.getContext("2d");
    m.fillStyle = m.createPattern(t, "repeat"); m.fillRect(0, 0, this.mask.width, this.mask.height);
    if (this.last) this._blit();
  }
  /* rgb: Uint8Array of w*h*3 */
  draw(rgb, gammaCorrected = true) {
    const d = this.img.data, lut = this.lut, n = this.w * this.h;
    let r = 0, g = 0, b = 0;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const R = gammaCorrected ? lut[rgb[j]] : rgb[j], G = gammaCorrected ? lut[rgb[j + 1]] : rgb[j + 1],
        B = gammaCorrected ? lut[rgb[j + 2]] : rgb[j + 2];
      d[i * 4] = R; d[i * 4 + 1] = G; d[i * 4 + 2] = B; d[i * 4 + 3] = 255;
      if ((i & 7) === 0) { r += R; g += G; b += B; }
    }
    const k = 8 / n;
    this.avg = [r * k, g * k, b * k];
    this.last = rgb;
    this._blit();
  }
  /* Draw from an already-rendered canvas (client-side emulation) */
  drawCanvas(src) {
    this.octx.drawImage(src, 0, 0, this.w, this.h);
    const px = this.octx.getImageData(0, 0, this.w, this.h).data, rgb = new Uint8Array(this.w * this.h * 3);
    for (let i = 0, j = 0; j < rgb.length; i += 4, j += 3) { rgb[j] = px[i]; rgb[j + 1] = px[i + 1]; rgb[j + 2] = px[i + 2]; }
    this.draw(rgb, false);
  }
  _blit() {
    if (!this.cell) return;
    this.octx.putImageData(this.img, 0, 0);
    const ctx = this.ctx;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(this.off, 0, 0, this.canvas.width, this.canvas.height);
    if (this.style === "dots" && this.useMask) ctx.drawImage(this.mask, 0, 0);
  }
  clear() { this.ctx.fillStyle = "#000"; this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height); this.last = null; }
  glowColor() { const [r, g, b] = this.avg; return `rgb(${r | 0},${g | 0},${b | 0})`; }
}

/* Emulates the transcoder's scale/filter/crop/position on a browser image or video
   frame (bilinear instead of area averaging, close enough for a preview). */
function emulateFrame(src, sw, sh, s, W, H, out) {
  out.width = W; out.height = H;
  const ctx = out.getContext("2d");
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
  let [cx, cy, cw, ch] = s.crop && s.crop[2] > 0 && s.crop[3] > 0 ? s.crop : [0, 0, sw, sh];
  cw = Math.min(cw, sw - cx); ch = Math.min(ch, sh - cy);
  const filter = s.filter || (((sw === W && sh === H) || (sw === W / 2 && sh === H / 2)) ? "pixel" : "smooth");
  ctx.imageSmoothingEnabled = filter !== "pixel"; ctx.imageSmoothingQuality = "high";
  const [px, py] = s.position || [0.5, 0.5];
  if (s.scale === "stretch") ctx.drawImage(src, cx, cy, cw, ch, 0, 0, W, H);
  else if (s.scale === "fill") {
    const k = Math.max(W / cw, H / ch), vw = W / k, vh = H / k;
    ctx.drawImage(src, cx + (cw - vw) * px, cy + (ch - vh) * py, vw, vh, 0, 0, W, H);
  } else {
    const k = Math.min(W / cw, H / ch), dw = cw * k, dh = ch * k;
    ctx.drawImage(src, cx, cy, cw, ch, (W - dw) * px, (H - dh) * py, dw, dh);
  }
  return filter;
}
