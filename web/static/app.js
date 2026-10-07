/* LED Poster app: vanilla JS, hash routing, no build step. Screens follow docs/ui-references.md. */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v; else if (k.startsWith("on")) el[k] = v;
    else if (k === "html") el.innerHTML = v; else if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) el.append(k.nodeType ? k : document.createTextNode(k));
  return el;
};
const icon = (id, cls = "") => { const t = document.createElement("template"); t.innerHTML = `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`; return t.content.firstChild; };
const fmtDur = (ms) => { const s = Math.round(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const throttle = (fn, ms) => { let t = 0, pend; return (...a) => { pend = a; const now = Date.now(); if (now - t >= ms) { t = now; fn(...pend); } else { clearTimeout(throttle._t); throttle._t = setTimeout(() => { t = Date.now(); fn(...pend); }, ms - (now - t)); } }; };
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function api(path, { method = "GET", body } = {}) {
  const isForm = body instanceof FormData;
  const r = await fetch(path, { method, headers: body && !isForm ? { "Content-Type": "application/json" } : {},
    body: body && !isForm ? JSON.stringify(body) : body });
  if (r.status === 401) { location.replace("/login"); throw new Error("Signed out"); }
  if (!r.ok) {
    let d = null; try { d = await r.json(); } catch {}
    const detail = d && d.detail;
    throw new Error(typeof detail === "string" ? detail : detail ? detail.map((e) => `${e.loc.slice(-1)[0]}: ${e.msg}`).join("; ") : r.statusText);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}

/* ---- toast + sheet ---- */
let toastT;
function toast(msg, error = false) {
  const t = $("#toast"); t.textContent = msg; t.className = error ? "error" : ""; clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.add("hidden"), error ? 5000 : 2500);
}
const sheet = $("#sheet");
function openSheet(...content) {
  const c = $("#sheet-content"); c.replaceChildren(...content);
  if (!sheet.open) sheet.showModal();
  return c;
}
function closeSheet() { if (sheet.open) sheet.close(); }
sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
sheet.addEventListener("close", () => { sheetRenderer = null; });
function confirmSheet(title, text, label, danger = true) {
  return new Promise((res) => {
    openSheet(h("h3", {}, title), h("p", { class: "muted" }, text),
      h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => { closeSheet(); res(false); } }, "Cancel"),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => { closeSheet(); res(true); } }, label)));
  });
}
function promptSheet(title, label, value = "", button = "Save") {
  return new Promise((res) => {
    const input = h("input", { class: "input", value, maxlength: 40 });
    const form = h("form", { onsubmit: (e) => { e.preventDefault(); closeSheet(); res(input.value.trim()); } },
      h("h3", {}, title), h("label", { class: "field" }, h("span", {}, label), input),
      h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: () => { closeSheet(); res(null); } }, "Cancel"),
        h("button", { class: "btn primary", type: "submit" }, button)));
    openSheet(form); input.focus();
  });
}
function switchEl(checked, onchange, label) {
  const i = h("input", { type: "checkbox", "aria-label": label, onchange: (e) => onchange(e.target.checked) }); i.checked = checked;
  return h("label", { class: "switch" }, i, h("i"));
}

/* ---- state ---- */
let status = null, media = [], playlists = {}, modes = [], schedule = null, settings = null;
let renderer, sheetRenderer = null, lastFrame = null, lastFrameStatus = null;
const canvasSize = () => (status && status.canvas) || { w: 128, h: 192 };
const mediaById = (id) => media.find((m) => m.id === id);

/* ---- router ---- */
const views = {};
function route() {
  const hash = location.hash || "#/home";
  const [, name, arg] = hash.split("/");
  const view = views[name] ? name : "home";
  $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === view));
  const tab = { item: "library", schedule: "more", settings: "more" }[view] || view;
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  $("#fab").classList.toggle("hidden", view !== "library");
  window.scrollTo(0, 0);
  if (views[view]) views[view](arg);
  if (view !== "item") stopItemPreview();
}
window.addEventListener("hashchange", route);

/* ---- live preview ---- */
function placePreview() {
  const card = $("#preview-card"), wide = matchMedia("(min-width: 900px)").matches;
  const target = wide ? $("#pinned") : $("#preview-slot");
  if (card.parentElement !== target) target.append(card);
}
matchMedia("(min-width: 900px)").addEventListener("change", placePreview);

function onStatusText(st) {
  lastFrameStatus = st;
  const strip = $("#status-strip");
  if (!st.streaming) {
    strip.replaceChildren(h("span", { class: "pill off" }, st.online ? "no preview stream" : "engine offline"));
    $("#preview-card").classList.add("offline"); $("#preview-offline").classList.remove("hidden");
    return;
  }
  $("#preview-card").classList.remove("offline"); $("#preview-offline").classList.add("hidden");
  const pills = [h("span", { class: "pill" }, modeLabel(st)), h("span", { class: "pill" }, `${Number(st.fps || 0).toFixed(0)} fps`),
    h("span", { class: "pill" }, `${Number(st.amps || 0).toFixed(1)} A`)];
  if (st.limited) pills.push(h("span", { class: "pill warn" }, "limiter"));
  strip.replaceChildren(...pills);
}
let frameN = 0;
function onFrame(bytes, st) {
  if (!st || !st.w) return;
  renderer.setSize(st.w, st.h); renderer.draw(bytes);
  lastFrame = bytes;
  if (sheetRenderer) { sheetRenderer.setSize(st.w, st.h); sheetRenderer.draw(bytes); }
  if ((frameN++ % 10) === 0) $("#preview-card").style.setProperty("--glow", renderer.glowColor());
}
let ws, wsDelay = 500;
function connectPreview() {
  if (document.hidden) return;
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/preview`);
  ws.binaryType = "arraybuffer";
  let pending = null;
  ws.onopen = () => { wsDelay = 500; };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") { pending = JSON.parse(e.data); onStatusText(pending); }
    else onFrame(new Uint8Array(e.data), pending);
  };
  ws.onclose = (e) => {
    ws = null;
    if (e.code === 4401) { location.replace("/login"); return; }
    onStatusText({ online: false, streaming: false });
    setTimeout(connectPreview, wsDelay); wsDelay = Math.min(wsDelay * 2, 8000);
  };
}
document.addEventListener("visibilitychange", () => { if (document.hidden) { ws && ws.close(); } else if (!ws) connectPreview(); });

/* ---- status polling + home ---- */
function modeLabel(st) {  // st: engine status with mode (kind) and mode_name
  if (!st || !st.mode) return "–";
  const kind = st.mode, name = st.mode_name || "";
  if (kind === "media") return (mediaById(name) || {}).name || "Media";
  if (kind === "playlist") return `Playlist · ${name}`;
  if (kind === "pattern") return (modes.find((x) => x.id === `pattern:${name}`) || {}).name || name;
  return name ? `${kind} · ${name}` : kind;
}
let briDragging = false;
function applyStatus(s) {
  status = s;
  const e = s.engine || {};
  $("#conn-dot").className = `dot ${s.online ? "on" : "off"}`;
  $("#conn-dot").title = s.online ? "Engine connected" : "Engine offline";
  $("#offline-banner").classList.toggle("hidden", s.online);
  $("#power").checked = e.power !== false;
  $("#power").disabled = !s.online;
  if (!briDragging) setSlider($("#brightness"), s.brightness, $("#bri-val"), `${s.brightness}%`);
  const auto = !!(e.brightness && e.brightness.auto_enabled);
  $("#auto").checked = auto;
  $("#brightness").disabled = auto && e.sensor !== "missing";
  const noSensor = e.sensor === "missing";
  $("#auto-sub").textContent = noSensor ? "No light sensor detected (uses the slider)" : auto && e.brightness && e.brightness.auto !== null && e.brightness.auto !== undefined ? `Sensor sets ${Math.round(e.brightness.auto)}% (${e.lux ?? "?"} lux)` : "Follows the room light sensor";
  const note = $("#bri-note");
  if (s.quiet_cap !== null && s.quiet_cap !== undefined) { note.textContent = `Quiet hours: capped at ${s.quiet_cap}% until they end.`; note.classList.remove("hidden"); }
  else if (auto && !noSensor) { note.textContent = "Set by the light sensor while auto-brightness is on."; note.classList.remove("hidden"); }
  else note.classList.add("hidden");
  $("#mode-name").textContent = s.online ? modeLabel(e) : "Engine offline";
  $("#more-version").textContent = `LED Poster ${s.version}`;
  $("#set-version").textContent = s.version;
  if (views.current === "modes") renderModes();
  if (views.current === "playlist") renderPlaylistChips();
}
function setSlider(input, v, label, text) { input.value = v; input.style.setProperty("--p", `${(v - input.min) / (input.max - input.min) * 100}%`); if (label) label.textContent = text; }
async function pollStatus() {
  try { applyStatus(await api("/api/status")); } catch (e) { if (e.message !== "Signed out") console.warn(e); }
  setTimeout(pollStatus, 3000);
}
const sendBrightness = throttle((v) => api("/api/brightness", { method: "POST", body: { value: v } }).catch((e) => toast(e.message, true)), 100);
$("#brightness").addEventListener("input", (e) => { briDragging = true; const v = +e.target.value; setSlider(e.target, v, $("#bri-val"), `${v}%`); sendBrightness(v); });
$("#brightness").addEventListener("change", () => { setTimeout(() => { briDragging = false; }, 500); });
$("#power").addEventListener("change", (e) => api("/api/power", { method: "POST", body: { on: e.target.checked } }).catch((err) => { toast(err.message, true); e.target.checked = !e.target.checked; }));
$("#auto").addEventListener("change", (e) => api("/api/auto_brightness", { method: "POST", body: { enabled: e.target.checked } }).then(async () => applyStatus(await api("/api/status"))).catch((err) => { toast(err.message, true); e.target.checked = !e.target.checked; }));
$$("input[name=pstyle]").forEach((r) => { r.checked = (localStorage.getItem("previewStyle") || "dots") === r.value; r.onchange = () => renderer.setStyle(r.value); });
views.home = () => { views.current = "home"; };

/* ---- modes ---- */
const modeValues = JSON.parse(localStorage.getItem("modeValues") || "{}");
function isActive(m) {
  const e = (status && status.engine) || {};
  return e.mode === m.apply.mode && (!m.apply.pattern || e.mode_name === m.apply.pattern);
}
async function applyMode(m, values = modeValues[m.id] || {}) {
  const body = { ...m.apply };
  for (const s of m.settings) { const v = values[s.key] ?? s.default; if (v !== undefined && v !== null && v !== "") body[s.key] = v; }
  for (const s of m.settings) if ((s.type === "media" || s.type === "select") && !body[s.key]) return modeSheet(m);
  try { await api("/api/mode", { method: "POST", body }); modeValues[m.id] = values; localStorage.setItem("modeValues", JSON.stringify(modeValues)); applyStatus(await api("/api/status")); }
  catch (e) { toast(e.message, true); }
}
function renderModes() {
  const groups = {};
  for (const m of modes) (groups[m.group] ||= []).push(m);
  $("#mode-groups").replaceChildren(...Object.entries(groups).map(([g, ms]) => h("div", {}, h("div", { class: "section-title" }, g),
    h("div", { class: "grid2" }, ms.map((m) => {
      const active = isActive(m);
      const card = h("button", { class: `mode-card${active ? " active" : ""}`, onclick: () => (active && m.settings.length ? modeSheet(m) : applyMode(m)) },
        h("div", { class: "art" }, modeArt(m)),
        h("span", { class: "name" }, m.name), h("span", { class: "sub" }, modeSub(m)),
        h("span", { class: "play" }, icon(active ? "stop" : "play")));
      const wrap = h("div", { style: "position:relative" }, card);
      if (m.settings.length) wrap.append(h("button", { class: "btn icon ghost mode-more", "aria-label": `${m.name} settings`, onclick: () => modeSheet(m) }, icon("dots")));
      return wrap;
    })))));
}
function modeSub(m) {
  const v = modeValues[m.id] || {};
  if (m.apply.mode === "media") return v.media ? (mediaById(v.media) || {}).name || "Pick an item" : "Pick an item";
  if (m.apply.mode === "playlist") return v.playlist || "Pick a playlist";
  if (m.apply.pattern === "solid") return v.color || "#ffffff";
  return "Test pattern";
}
function modeArt(m) {
  const v = modeValues[m.id] || {};
  if (m.apply.mode === "media" && v.media && (mediaById(v.media) || {}).status === "ready") return h("img", { src: `/api/media/${v.media}/thumb.png`, alt: "" });
  const c = h("canvas", { width: 8, height: 12 }), x = c.getContext("2d");
  const colors = { "panel-id": ["#1b1b22", "#FFB020"], solid: ["#000", v.color || "#fff"], cycle: ["#f33", "#3f3"], rows: ["#000", "#8cf"], cols: ["#000", "#fc8"], media: ["#222", "#444"], playlist: ["#222", "#1DB954"] };
  const [a, b] = colors[m.apply.pattern || m.apply.mode] || ["#222", "#555"];
  x.fillStyle = a; x.fillRect(0, 0, 8, 12); x.fillStyle = b;
  if (m.apply.pattern === "rows") x.fillRect(0, 5, 8, 1); else if (m.apply.pattern === "cols") x.fillRect(3, 0, 1, 12);
  else if (m.apply.pattern === "solid" || m.apply.pattern === "cycle") x.fillRect(0, 0, 8, 12); else x.fillRect(2, 3, 4, 6);
  return c;
}
function modeSheet(m) {
  const values = { ...(modeValues[m.id] || {}) };
  const set = (k, v) => { values[k] = v; applyMode(m, values); };
  const prev = h("canvas", { "aria-label": "Live preview" });
  const content = [h("h3", {}, m.name),
    h("div", { class: "preview-card sheet-preview" }, h("div", { class: "preview-wrap" }, prev))];
  for (const s of m.settings) {
    content.push(h("div", { class: "section-title" }, s.label));
    if (s.type === "select") content.push(h("div", { class: "chips" }, s.options.length ? s.options.map((o) => h("button", { class: `chip${values[s.key] === o ? " active" : ""}`, onclick: () => { set(s.key, o); closeSheet(); } }, o)) : h("span", { class: "muted" }, "Nothing to choose yet")));
    else if (s.type === "media") content.push(mediaPicker((id) => { set(s.key, id); closeSheet(); }, values[s.key]));
    else if (s.type === "color") {
      const presets = ["#ffffff", "#ffb020", "#ff4d4f", "#1db954", "#4da3ff", "#b05cff", "#ff66c4", "#00e5ff"];
      const input = h("input", { class: "color-input", type: "color", value: values[s.key] || s.default || "#ffffff", "aria-label": "Custom colour", onchange: (e) => set(s.key, e.target.value) });
      content.push(h("div", { class: "swatches" }, presets.map((c) => h("button", { class: `swatch${(values[s.key] || s.default) === c ? " active" : ""}`, style: `background:${c}`, "aria-label": c, onclick: () => { set(s.key, c); modeSheet(m); } })), input));
    } else if (s.type === "slider") {
      const val = h("span", { class: "num" }, values[s.key] ?? s.default);
      const r = h("input", { type: "range", min: s.min ?? 0, max: s.max ?? 100, value: values[s.key] ?? s.default ?? 50, "aria-label": s.label, oninput: throttle((e) => { val.textContent = e.target.value; set(s.key, +e.target.value); }, 150) });
      content.push(h("label", { class: "slider" }, h("span", { class: "top" }, h("b", {}, s.label), val), r));
    } else if (s.type === "text") content.push(h("input", { class: "input", value: values[s.key] ?? s.default ?? "", "aria-label": s.label, onchange: (e) => set(s.key, e.target.value) }));
    else if (s.type === "toggle") content.push(h("div", { class: "row", style: "padding:0" }, h("div", { class: "grow" }, s.label), switchEl(!!(values[s.key] ?? s.default), (v) => set(s.key, v), s.label)));
  }
  content.push(h("div", { class: "actions" }, h("button", { class: "btn ghost", onclick: () => { delete modeValues[m.id]; localStorage.setItem("modeValues", JSON.stringify(modeValues)); closeSheet(); if (isActive(m)) applyMode(m, {}); renderModes(); } }, "Reset to defaults"),
    h("button", { class: "btn primary", onclick: closeSheet }, "Done")));
  openSheet(...content);
  sheetRenderer = new DotRenderer(prev, canvasSize().w, canvasSize().h, gamma());
  if (lastFrame && lastFrameStatus) { sheetRenderer.setSize(lastFrameStatus.w, lastFrameStatus.h); sheetRenderer.draw(lastFrame); }
}
const gamma = () => (settings && settings.display && settings.display.gamma) || 2.2;
function mediaPicker(onpick, selected) {
  const ready = media.filter((m) => m.status === "ready");
  if (!ready.length) return h("p", { class: "muted" }, "No transcoded media yet. Upload something in the Library.");
  return h("div", { class: "media-grid" }, ready.map((m) => h("button", { class: `tile${m.id === selected ? " selected" : ""}`, onclick: () => onpick(m.id), "aria-label": m.name },
    h("img", { src: `/api/media/${m.id}/thumb.png`, alt: "" }), h("span", { class: "badge dur" }, fmtDur(m.info.duration_ms)))));
}
views.modes = async () => { views.current = "modes"; modes = await api("/api/modes"); if (!media.length) await loadMedia(); renderModes(); };

/* ---- library ---- */
let libFilter = "all", libPoll;
async function loadMedia() { media = await api("/api/media"); return media; }
const kindLabel = { video: "VID", gif: "GIF", image: "IMG", pixel: "PIX" };
function renderLibrary() {
  const items = media.filter((m) => libFilter === "all" || m.kind === libFilter);
  $("#lib-empty").classList.toggle("hidden", items.length > 0);
  $("#drop").classList.toggle("hidden", items.length > 0 && !matchMedia("(pointer: fine)").matches);
  $("#media-grid").replaceChildren(...items.map((m) => {
    const busy = m.status === "queued" || m.status === "transcoding" || m.status === "uploading";
    const tile = h("button", { class: `tile${busy ? " busy" : ""}`, "aria-label": `${m.name}, ${m.status}`, onclick: () => (m.status === "failed" ? failSheet(m) : (location.hash = `#/item/${m.id}`)) });
    if (m.status === "ready" || (m.info && m.status !== "failed")) tile.append(h("img", { src: `/api/media/${m.id}/thumb.png?${m.created}`, alt: "" }));
    tile.append(h("span", { class: "badge type" }, kindLabel[m.kind]));
    if (m.info) tile.append(h("span", { class: "badge dur" }, fmtDur(m.info.duration_ms)));
    if (busy) tile.append(h("span", { class: "state" }, m.status === "transcoding" ? "Converting…" : m.status === "queued" ? "Queued" : "Uploading"), h("div", { class: "progress indeterminate" }, h("i")));
    if (m.status === "failed") tile.append(h("span", { class: "badge fail", "aria-hidden": "true" }, "!"), h("span", { class: "state", style: "color:var(--danger)" }, "Failed"));
    return tile;
  }));
  clearTimeout(libPoll);
  if (media.some((m) => m.status === "queued" || m.status === "transcoding")) libPoll = setTimeout(async () => { await loadMedia(); if (views.current === "library") renderLibrary(); }, 1500);
}
function failSheet(m) {
  openSheet(h("h3", { style: "color:var(--danger)" }, "Conversion failed"), h("p", {}, m.name),
    h("p", { class: "muted", style: "word-break:break-word" }, m.error || "Unknown error"),
    h("div", { class: "actions" },
      h("button", { class: "btn danger", onclick: async () => { closeSheet(); await api(`/api/media/${m.id}`, { method: "DELETE" }); await loadMedia(); renderLibrary(); } }, "Delete"),
      h("button", { class: "btn", onclick: () => { closeSheet(); location.hash = `#/item/${m.id}`; } }, "Edit settings"),
      h("button", { class: "btn primary", onclick: async () => { closeSheet(); await api(`/api/media/${m.id}/retry`, { method: "POST" }); await loadMedia(); renderLibrary(); } }, "Retry")));
}
$$("#lib-filters .chip").forEach((c) => c.onclick = () => { libFilter = c.dataset.f; $$("#lib-filters .chip").forEach((x) => x.classList.toggle("active", x === c)); renderLibrary(); });
$("#fab").onclick = () => $("#file-input").click();
$("#drop").onclick = () => $("#file-input").click();
$("#file-input").onchange = (e) => { handleFiles([...e.target.files]); e.target.value = ""; };
const libView = $('[data-view="library"]');
libView.addEventListener("dragover", (e) => { e.preventDefault(); $("#drop").classList.add("over"); });
libView.addEventListener("dragleave", () => $("#drop").classList.remove("over"));
libView.addEventListener("drop", (e) => { e.preventDefault(); $("#drop").classList.remove("over"); handleFiles([...e.dataTransfer.files]); });

const MAX_BYTES = 200 * 1024 * 1024;
function handleFiles(files) {
  const ok = files.filter((f) => { if (f.size > MAX_BYTES) { toast(`${f.name} is over the 200MB limit`, true); return false; } return true; });
  if (!ok.length) return;
  if (ok.length === 1) uploadSheet(ok[0]); else uploadAll(ok.map((f) => ({ file: f, settings: {} })));
}
/* Pre-upload options with a client-side emulation of scale/filter/position on the dot renderer. */
function uploadSheet(file) {
  const s = { scale: "fit", filter: null, position: [0.5, 0.5], dither: false, speed: 1 };
  const prev = h("canvas", { "aria-label": "Preview with the chosen settings" });
  const src = sourceElement(URL.createObjectURL(file), file.type.startsWith("video") || /\.(mp4|webm|mov)$/i.test(file.name));
  const info = h("p", { class: "note" }, "Loading preview…");
  const seg = (name, opts, cur, onpick) => h("div", { class: "seg", role: "radiogroup" }, opts.map(([v, l]) => { const i = h("input", { type: "radio", name, value: v, onchange: () => onpick(v) }); i.checked = cur === v; return h("label", {}, i, h("span", {}, l)); }));
  let r, posRows;
  const redraw = () => { if (!src.ready) return; const c = document.createElement("canvas"); const f = emulateFrame(src.el, src.w, src.h, s, canvasSize().w, canvasSize().h, c); r.drawCanvas(c); info.textContent = `${src.w}×${src.h} source → ${canvasSize().w}×${canvasSize().h}, ${s.scale}, ${s.filter || `auto (${f})`}`; posRows.classList.toggle("hidden", s.scale === "stretch"); };
  const px = h("input", { type: "range", min: 0, max: 100, value: 50, "aria-label": "Horizontal position", oninput: (e) => { s.position[0] = e.target.value / 100; redraw(); } });
  const py = h("input", { type: "range", min: 0, max: 100, value: 50, "aria-label": "Vertical position", oninput: (e) => { s.position[1] = e.target.value / 100; redraw(); } });
  posRows = h("div", {}, h("label", { class: "slider" }, h("span", { class: "top" }, h("b", {}, "Horizontal position")), px), h("label", { class: "slider" }, h("span", { class: "top" }, h("b", {}, "Vertical position")), py));
  openSheet(h("h3", {}, "Upload"), h("p", { class: "muted", style: "margin-top:-8px" }, file.name),
    h("div", { class: "preview-card sheet-preview" }, h("div", { class: "preview-wrap" }, prev)), info,
    h("div", { class: "section-title" }, "Scaling"), seg("u-scale", [["fit", "Fit"], ["fill", "Fill"], ["stretch", "Stretch"]], s.scale, (v) => { s.scale = v; redraw(); }),
    h("div", { class: "section-title" }, "Filter"), seg("u-filter", [["", "Auto"], ["smooth", "Smooth"], ["pixel", "Pixel"]], "", (v) => { s.filter = v || null; redraw(); }),
    posRows,
    h("div", { class: "row", style: "padding:8px 0" }, h("div", { class: "grow" }, "Dithering"), switchEl(false, (v) => { s.dither = v; }, "Dithering")),
    h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => { closeSheet(); src.stop(); } }, "Cancel"),
      h("button", { class: "btn primary", onclick: () => { closeSheet(); src.stop(); uploadAll([{ file, settings: s }]); } }, "Upload")));
  r = new DotRenderer(prev, canvasSize().w, canvasSize().h, gamma());
  src.onready = () => { redraw(); if (src.video) src.tick = setInterval(redraw, 100); };
  src.onerror = () => { info.textContent = "This browser cannot preview the file; the wall's converter may still handle it."; };
}
function sourceElement(url, video) {
  const o = { video, ready: false, stop() { clearInterval(o.tick); if (video) o.el.pause(); URL.revokeObjectURL(url); } };
  if (video) {
    o.el = h("video", { src: url, muted: "", loop: "", playsinline: "", autoplay: "" }); o.el.muted = true;
    o.el.onloadeddata = () => { o.w = o.el.videoWidth; o.h = o.el.videoHeight; o.ready = true; o.el.play().catch(() => {}); o.onready && o.onready(); };
  } else {
    o.el = h("img", { src: url });
    o.el.onload = () => { o.w = o.el.naturalWidth; o.h = o.el.naturalHeight; o.ready = true; o.onready && o.onready(); };
  }
  o.el.onerror = () => o.onerror && o.onerror();
  return o;
}
async function uploadAll(jobs) {
  const banner = $("#upload-banner"), text = $("#upload-text"), bar = $("#upload-bar");
  banner.classList.remove("hidden");
  for (let i = 0; i < jobs.length; i++) {
    const { file, settings: s } = jobs[i];
    text.textContent = `Uploading ${i + 1} of ${jobs.length} · ${file.name}`;
    try {
      await new Promise((res, rej) => {
        const fd = new FormData();
        for (const [k, v] of Object.entries(s)) if (v !== null && v !== undefined) fd.append(k, Array.isArray(v) ? v.join(",") : String(v));
        fd.append("file", file, file.name);
        const x = new XMLHttpRequest(); x.open("POST", "/api/media");
        x.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.width = `${e.loaded / e.total * 100}%`; };
        x.onload = () => { if (x.status === 401) location.replace("/login"); else if (x.status >= 200 && x.status < 300) res(); else { let d; try { d = JSON.parse(x.responseText).detail; } catch {} rej(new Error(typeof d === "string" ? d : `Upload failed (${x.status})`)); } };
        x.onerror = () => rej(new Error("Upload failed: connection lost (is the file over 200MB?)"));
        x.send(fd);
      });
    } catch (e) { toast(e.message, true); }
    bar.style.width = "0";
    await loadMedia(); if (views.current === "library") renderLibrary();
  }
  banner.classList.add("hidden");
}
views.library = async () => { views.current = "library"; await loadMedia(); renderLibrary(); };

/* ---- item editor ---- */
let item = null, cur = null, itemRenderer, itemAnim = null, itemSrc = null, itemFrames = null;
const speedFromSlider = (v) => Math.round(Math.pow(2, +v) * 20) / 20;
const fmtSpeed = (s) => `${s < 1 ? s.toFixed(2).replace(/0$/, "") : s.toFixed(1)}x`;
function stopItemPreview() { clearTimeout(itemAnim); itemAnim = null; if (itemSrc) { itemSrc.stop(); itemSrc = null; } }
views.item = async (id) => {
  views.current = "item";
  stopItemPreview();
  try { item = await api(`/api/media/${id}`); } catch (e) { toast(e.message, true); location.hash = "#/library"; return; }
  cur = JSON.parse(JSON.stringify(item.settings));
  itemFrames = null;
  if (!itemRenderer) itemRenderer = new DotRenderer($("#item-preview"), canvasSize().w, canvasSize().h, gamma());
  itemRenderer.setSize(canvasSize().w, canvasSize().h); itemRenderer.clear();
  $("#item-title").textContent = item.name; $("#item-name").value = item.name;
  bindItemControls();
  $$("#item-tabs [role=tab]").forEach((t) => t.onclick = () => showPane(t.dataset.pane));
  showPane("fit");
  refreshItemPreview();
  pollItem();
};
function showPane(p) { $$("#item-tabs [role=tab]").forEach((t) => t.setAttribute("aria-selected", t.dataset.pane === p)); $$('[data-view="item"] .pane').forEach((x) => x.classList.toggle("active", x.dataset.pane === p)); if (p === "crop") renderCropBox(); }
function bindItemControls() {
  $$("#item-scale input").forEach((i) => { i.checked = i.value === cur.scale; i.onchange = () => { cur.scale = i.value; refreshItemPreview(); }; });
  $$("#item-filter input").forEach((i) => { i.checked = (cur.filter || "") === i.value; i.onchange = () => { cur.filter = i.value || null; refreshItemPreview(); }; });
  setSlider($("#posx"), Math.round(cur.position[0] * 100), $("#posx-val"), `${Math.round(cur.position[0] * 100)}%`);
  setSlider($("#posy"), Math.round(cur.position[1] * 100), $("#posy-val"), `${Math.round(cur.position[1] * 100)}%`);
  $("#posx").oninput = (e) => { cur.position[0] = e.target.value / 100; setSlider(e.target, +e.target.value, $("#posx-val"), `${e.target.value}%`); refreshItemPreview(); };
  $("#posy").oninput = (e) => { cur.position[1] = e.target.value / 100; setSlider(e.target, +e.target.value, $("#posy-val"), `${e.target.value}%`); refreshItemPreview(); };
  const sv = Math.log2(cur.speed || 1);
  setSlider($("#speed"), sv, $("#speed-val"), fmtSpeed(cur.speed || 1));
  $("#speed").oninput = (e) => { cur.speed = speedFromSlider(e.target.value); setSlider(e.target, +e.target.value, $("#speed-val"), fmtSpeed(cur.speed)); markDirty(); };
  $$("#speed-chips .chip").forEach((c) => c.onclick = () => { cur.speed = +c.dataset.v; setSlider($("#speed"), Math.log2(cur.speed), $("#speed-val"), fmtSpeed(cur.speed)); markDirty(); });
  $("#dither").checked = !!cur.dither; $("#dither").onchange = (e) => { cur.dither = e.target.checked; markDirty(); };
  for (const k of ["x", "y", "w", "h"]) $(`#crop-${k}`).onchange = () => { const v = ["x", "y", "w", "h"].map((q) => +$(`#crop-${q}`).value); cur.crop = v[2] > 0 && v[3] > 0 ? v : null; refreshItemPreview(); renderCropBox(); };
  $("#crop-reset").onclick = () => { cur.crop = null; refreshItemPreview(); renderCropBox(); };
  $("#crop-23").onclick = () => { const { source_w: sw, source_h: sh } = item.info || {}; if (!sw) return; let w = sw, hh = Math.round(sw * 1.5); if (hh > sh) { hh = sh; w = Math.round(sh / 1.5); } cur.crop = [Math.round((sw - w) / 2), Math.round((sh - hh) / 2), w, hh]; refreshItemPreview(); renderCropBox(); };
  $("#item-save").onclick = saveItem;
  $("#item-play").onclick = async () => { try { await api("/api/mode", { method: "POST", body: { mode: "media", media: item.id } }); toast("Playing on the wall"); } catch (e) { toast(e.message, true); } };
  $("#item-delete").onclick = async () => { if (await confirmSheet("Delete this item?", `${item.name} will be removed from the library and from any playlist.`, "Delete")) { await api(`/api/media/${item.id}`, { method: "DELETE" }); location.hash = "#/library"; } };
  $("#item-name").onchange = markDirty;
}
const isDirty = () => JSON.stringify(cur) !== JSON.stringify(item.settings) || $("#item-name").value.trim() !== item.name;
function markDirty() { $("#item-save").disabled = !isDirty(); $("#item-save").textContent = isDirty() ? "Save" : "Saved"; }
function refreshItemPreview() {
  markDirty();
  const dirtyVisual = ["scale", "filter", "crop", "position"].some((k) => JSON.stringify(cur[k]) !== JSON.stringify(item.settings[k]));
  if (!dirtyVisual && item.status === "ready") playCachedFrames(); else emulateItem();
  $("#item-error").textContent = item.status === "failed" ? item.error : "";
  const i = item.info;
  $("#item-info").textContent = i ? `${i.source_w}×${i.source_h} ${item.ext.slice(1).toUpperCase()} → ${i.w}×${i.h}, ${i.frames} frame${i.frames === 1 ? "" : "s"}, ${fmtDur(i.duration_ms)}, ${(item.size / 1048576).toFixed(1)} MB` : `${item.ext.slice(1).toUpperCase()}, ${(item.size / 1048576).toFixed(1)} MB`;
  $("#item-status").replaceChildren(h("span", { class: `pill${item.status === "failed" ? " off" : item.status === "ready" ? "" : " warn"}` }, item.status === "ready" ? (dirtyVisual ? "preview of new settings" : "as on the wall") : item.status === "transcoding" ? "converting…" : item.status));
}
async function playCachedFrames() {
  stopItemPreview();
  if (!itemFrames) {
    const r = await api(`/api/media/${item.id}/frames?count=12`);
    const buf = new Uint8Array(await r.arrayBuffer()), n = +r.headers.get("X-Frames"), w = +r.headers.get("X-Width"), hh = +r.headers.get("X-Height");
    itemFrames = { n, w, h: hh, fb: w * hh * 3, buf };
  }
  const f = itemFrames; let i = 0;
  itemRenderer.setSize(f.w, f.h);
  const step = () => { itemRenderer.draw(f.buf.subarray(i * f.fb, (i + 1) * f.fb)); i = (i + 1) % f.n; if (f.n > 1) itemAnim = setTimeout(step, Math.max(80, item.info.duration_ms / f.n)); };
  step();
}
function emulateItem() {
  const draw = () => { if (!itemSrc || !itemSrc.ready) return; const c = document.createElement("canvas"); emulateFrame(itemSrc.el, itemSrc.w, itemSrc.h, cur, canvasSize().w, canvasSize().h, c); itemRenderer.setSize(canvasSize().w, canvasSize().h); itemRenderer.drawCanvas(c); };
  if (itemSrc) { clearTimeout(itemAnim); itemAnim = null; draw(); return; }
  itemSrc = sourceElement(`/api/media/${item.id}/source`, /\.(mp4|webm|mov)$/i.test(item.ext));
  itemSrc.onready = () => { draw(); if (itemSrc.video) itemSrc.tick = setInterval(draw, 100); renderCropBox(); };
  itemSrc.onerror = () => { $("#item-status").replaceChildren(h("span", { class: "pill warn" }, "no browser preview for this format; save to see the result")); };
}
function renderCropBox() {
  const box = $("#crop-box");
  const { source_w: sw, source_h: sh } = item.info || (itemSrc && itemSrc.ready ? { source_w: itemSrc.w, source_h: itemSrc.h } : {});
  $$("#crop-x,#crop-y,#crop-w,#crop-h").forEach((i, k) => { i.value = cur.crop ? cur.crop[k] : [0, 0, sw || 0, sh || 0][k]; });
  if (!itemSrc) { emulateItemSource(); }
  if (!itemSrc || !itemSrc.ready) { box.replaceChildren(h("p", { class: "muted", style: "padding:24px" }, "Loading source…")); return; }
  const img = itemSrc.video ? snapshot(itemSrc.el) : h("img", { src: itemSrc.el.src, alt: "" });
  const rect = h("div", { class: "rect" });
  box.replaceChildren(img, rect);
  const place = () => { const W = img.clientWidth, H = img.clientHeight, [x, y, w, hh] = cur.crop || [0, 0, sw, sh]; const k = W / sw; rect.style.cssText = `left:${img.offsetLeft + x * k}px;top:${img.offsetTop + y * k}px;width:${w * k}px;height:${hh * k}px`; };
  img.onload ? (img.onload = place) : setTimeout(place, 0); setTimeout(place, 50);
  let drag = null;
  box.onpointerdown = (e) => { if (!cur.crop) return; drag = { x: e.clientX, y: e.clientY, c: [...cur.crop] }; box.setPointerCapture(e.pointerId); };
  box.onpointermove = (e) => { if (!drag) return; const k = sw / img.clientWidth; const [, , w, hh] = drag.c; cur.crop = [Math.round(Math.min(Math.max(0, drag.c[0] + (e.clientX - drag.x) * k), sw - w)), Math.round(Math.min(Math.max(0, drag.c[1] + (e.clientY - drag.y) * k), sh - hh)), w, hh]; $("#crop-x").value = cur.crop[0]; $("#crop-y").value = cur.crop[1]; place(); };
  box.onpointerup = () => { if (drag) { drag = null; refreshItemPreview(); } };
}
function emulateItemSource() { if (!itemSrc) { itemSrc = sourceElement(`/api/media/${item.id}/source`, /\.(mp4|webm|mov)$/i.test(item.ext)); itemSrc.onready = () => renderCropBox(); } }
function snapshot(video) { const c = document.createElement("canvas"); c.width = video.videoWidth; c.height = video.videoHeight; c.getContext("2d").drawImage(video, 0, 0); return c; }
async function saveItem() {
  const body = {};
  if ($("#item-name").value.trim() && $("#item-name").value.trim() !== item.name) body.name = $("#item-name").value.trim();
  if (JSON.stringify(cur) !== JSON.stringify(item.settings)) body.settings = cur;
  try { item = await api(`/api/media/${item.id}`, { method: "PATCH", body }); toast(body.settings ? "Saved. Converting with the new settings…" : "Saved"); $("#item-title").textContent = item.name; refreshItemPreview(); pollItem(); }
  catch (e) { toast(e.message, true); }
}
let itemPollT;
function pollItem() {
  clearTimeout(itemPollT);
  if (views.current !== "item" || !item || !(item.status === "queued" || item.status === "transcoding")) return;
  itemPollT = setTimeout(async () => { const fresh = await api(`/api/media/${item.id}`); const done = fresh.status !== item.status; item = fresh; if (done) { itemFrames = null; cur = JSON.parse(JSON.stringify(item.settings)); bindItemControls(); refreshItemPreview(); } pollItem(); }, 1000);
}

/* ---- playlist ---- */
let plName = localStorage.getItem("playlist") || null;
async function loadPlaylists() { playlists = await api("/api/playlists"); if (!playlists[plName]) plName = Object.keys(playlists)[0] || null; }
function renderPlaylistChips() {
  const names = Object.keys(playlists).sort();
  const e = (status && status.engine) || {};
  $("#pl-names").replaceChildren(...names.map((n) => h("button", { class: `chip${n === plName ? " active" : ""}`, onclick: () => { plName = n; localStorage.setItem("playlist", n); renderPlaylist(); } }, e.mode === "playlist" && e.mode_name === n ? "▶ " : "", n)));
  $("#pl-none").classList.toggle("hidden", names.length > 0);
  $("#pl-editor").classList.toggle("hidden", !plName);
}
async function savePlaylist(pl = playlists[plName]) {
  try { playlists[plName] = await api(`/api/playlists/${encodeURIComponent(plName)}`, { method: "PUT", body: pl }); } catch (e) { toast(e.message, true); await loadPlaylists(); }
  renderPlaylist();
}
function renderPlaylist() {
  renderPlaylistChips();
  const pl = playlists[plName];
  if (!pl) return;
  const total = pl.items.reduce((t, i) => { const m = mediaById(i.media); return t + (i.duration_s ? i.duration_s * 1000 : m && m.info ? (m.info.frames === 1 ? 10000 : m.info.duration_ms) : 0); }, 0);
  $("#pl-title").textContent = plName;
  $("#pl-meta").textContent = `${pl.items.length} item${pl.items.length === 1 ? "" : "s"} · ${fmtDur(total)}${pl.loop ? " loop" : ""}${pl.shuffle ? " · shuffle" : ""}`;
  $("#pl-shuffle").setAttribute("aria-pressed", pl.shuffle); $("#pl-loop").setAttribute("aria-pressed", pl.loop);
  $("#pl-empty").classList.toggle("hidden", pl.items.length > 0);
  $("#pl-rows").replaceChildren(...pl.items.map((it, idx) => {
    const m = mediaById(it.media);
    const row = h("div", { class: "pl-row", "data-idx": idx },
      m && m.status === "ready" ? h("img", { class: "thumb", src: `/api/media/${m.id}/thumb.png`, alt: "" }) : h("div", { class: "thumb" }),
      h("div", { class: "grow" }, h("div", { class: "name" }, m ? m.name : "(missing item)"), h("div", { class: "sub" }, m ? `${kindLabel[m.kind]} · ${m.info ? fmtDur(m.info.duration_ms) : m.status}` : it.media)),
      h("button", { class: "chip dur num", onclick: () => durationSheet(idx) }, it.duration_s ? `${it.duration_s} s` : "full"),
      h("button", { class: "btn icon ghost remove", "aria-label": "Remove from playlist", onclick: () => { pl.items.splice(idx, 1); savePlaylist(); } }, icon("x")),
      h("button", { class: "handle", "aria-label": "Drag to reorder" }, icon("drag")));
    dragHandle(row, $(".handle", row), idx);
    return row;
  }));
}
function dragHandle(row, handle, idx) {
  handle.onpointerdown = (e) => {
    e.preventDefault(); handle.setPointerCapture(e.pointerId);
    const rows = $$("#pl-rows .pl-row"), startY = e.clientY, rowH = row.offsetHeight; let to = idx;
    row.classList.add("lifted");
    handle.onpointermove = (ev) => {
      const dy = ev.clientY - startY; row.style.transform = `translateY(${dy}px)`;
      to = Math.min(rows.length - 1, Math.max(0, idx + Math.round(dy / rowH)));
      rows.forEach((r, i) => { if (r === row) return; r.style.transform = (i > idx && i <= to) ? `translateY(${-rowH}px)` : (i < idx && i >= to) ? `translateY(${rowH}px)` : ""; });
    };
    handle.onpointerup = handle.onpointercancel = () => {
      handle.onpointermove = handle.onpointerup = null;
      rows.forEach((r) => { r.style.transform = ""; }); row.classList.remove("lifted");
      if (to !== idx) { const items = playlists[plName].items; items.splice(to, 0, items.splice(idx, 1)[0]); savePlaylist(); }
    };
  };
}
function durationSheet(idx) {
  const it = playlists[plName].items[idx];
  const set = (v) => { it.duration_s = v; closeSheet(); savePlaylist(); };
  const custom = h("input", { class: "input num", type: "number", min: 1, step: 1, placeholder: "seconds", value: it.duration_s || "" });
  openSheet(h("h3", {}, "Show for"), h("div", { class: "chips" },
    h("button", { class: `chip${!it.duration_s ? " active" : ""}`, onclick: () => set(null) }, "Full clip"),
    ...[5, 10, 15, 30, 60].map((s) => h("button", { class: `chip${it.duration_s === s ? " active" : ""}`, onclick: () => set(s) }, `${s} s`))),
    h("p", { class: "note" }, "Full clip plays the item once (stills show for 10 s). A clip shorter than the time loops."),
    h("div", { class: "inline", style: "margin-top:12px" }, custom, h("button", { class: "btn primary", style: "flex:0 0 auto", onclick: () => { if (+custom.value > 0) set(+custom.value); } }, "Set")));
}
$("#pl-shuffle").onclick = () => { playlists[plName].shuffle = !playlists[plName].shuffle; savePlaylist(); };
$("#pl-loop").onclick = () => { playlists[plName].loop = !playlists[plName].loop; savePlaylist(); };
$("#pl-play").onclick = async () => { try { await api("/api/mode", { method: "POST", body: { mode: "playlist", playlist: plName } }); toast(`Playing ${plName}`); applyStatus(await api("/api/status")); } catch (e) { toast(e.message, true); } };
$("#pl-add").onclick = () => openSheet(h("h3", {}, "Add to playlist"), mediaPicker((id) => { playlists[plName].items.push({ media: id, duration_s: null }); closeSheet(); savePlaylist(); }));
$("#pl-new").onclick = async () => { const n = await promptSheet("New playlist", "Name", "", "Create"); if (!n) return; if (playlists[n]) { toast("That name is taken", true); return; } plName = n; localStorage.setItem("playlist", n); await savePlaylist({ items: [], loop: true, shuffle: false }); };
$("#pl-rename").onclick = async () => { const n = await promptSheet("Rename playlist", "Name", plName); if (!n || n === plName) return; const pl = playlists[plName]; const old = plName; plName = n; await savePlaylist(pl); if (playlists[n]) { await api(`/api/playlists/${encodeURIComponent(old)}`, { method: "DELETE" }); delete playlists[old]; localStorage.setItem("playlist", n); renderPlaylist(); } };
$("#pl-delete").onclick = async () => { if (await confirmSheet("Delete playlist?", `${plName} will be removed. Media items stay in the library.`, "Delete")) { await api(`/api/playlists/${encodeURIComponent(plName)}`, { method: "DELETE" }); await loadPlaylists(); renderPlaylist(); } };
views.playlist = async () => { views.current = "playlist"; await Promise.all([loadPlaylists(), loadMedia()]); renderPlaylist(); };

/* ---- more / schedule ---- */
$("#logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); location.replace("/login"); };
views.more = () => { views.current = "more"; };

const DAYS = ["M", "T", "W", "T", "F", "S", "S"];
const saveSchedule = debounce(async () => { try { schedule = await api("/api/schedule", { method: "PUT", body: schedule }); drawRing(); applyStatus(await api("/api/status")); } catch (e) { toast(e.message, true); } }, 400);
function bindSchedule() {
  const s = schedule;
  $("#sch-enabled").checked = s.enabled; $("#sch-on").value = s.on; $("#sch-off").value = s.off;
  $("#q-enabled").checked = s.quiet.enabled; $("#q-start").value = s.quiet.start; $("#q-end").value = s.quiet.end;
  setSlider($("#q-max"), s.quiet.max_brightness, $("#q-max-val"), `${s.quiet.max_brightness}%`);
  $("#sch-days").replaceChildren(...DAYS.map((d, i) => h("button", { class: `chip day${s.days.includes(i) ? " active" : ""}`, "aria-pressed": s.days.includes(i), "aria-label": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][i], onclick: () => { s.days = s.days.includes(i) ? s.days.filter((x) => x !== i) : [...s.days, i].sort(); bindSchedule(); saveSchedule(); } }, d)));
  $("#sch-tz").textContent = status ? `Times use the wall's clock (${status.timezone}, now ${status.time}).` : "";
  drawRing();
}
$("#sch-enabled").onchange = (e) => { schedule.enabled = e.target.checked; saveSchedule(); };
$("#sch-on").onchange = (e) => { if (e.target.value) { schedule.on = e.target.value; saveSchedule(); } };
$("#sch-off").onchange = (e) => { if (e.target.value) { schedule.off = e.target.value; saveSchedule(); } };
$("#q-enabled").onchange = (e) => { schedule.quiet.enabled = e.target.checked; saveSchedule(); };
$("#q-start").onchange = (e) => { if (e.target.value) { schedule.quiet.start = e.target.value; saveSchedule(); } };
$("#q-end").onchange = (e) => { if (e.target.value) { schedule.quiet.end = e.target.value; saveSchedule(); } };
$("#q-max").oninput = (e) => { schedule.quiet.max_brightness = +e.target.value; setSlider(e.target, +e.target.value, $("#q-max-val"), `${e.target.value}%`); saveSchedule(); };
function drawRing() {
  const s = schedule, mins = (t) => { const [hh, m] = t.split(":").map(Number); return hh * 60 + m; };
  const arc = (a, b, r, cls) => { // minutes → arc path, clockwise from 00:00 at the top
    if (a === b) return "";
    const span = ((b - a) % 1440 + 1440) % 1440 || 1440, ang = (m) => (m / 1440) * 2 * Math.PI - Math.PI / 2;
    const p = (m) => `${100 + r * Math.cos(ang(m))} ${100 + r * Math.sin(ang(m))}`;
    return `<path class="${cls}" d="M ${p(a)} A ${r} ${r} 0 ${span > 720 ? 1 : 0} 1 ${p(a + span - 0.01)}"/>`;
  };
  const now = status ? mins(status.time) : 0;
  $("#ring").innerHTML = `<circle class="track" cx="100" cy="100" r="80"/>` +
    (s.enabled ? arc(mins(s.on), mins(s.off), 80, "on") : "") + (s.quiet.enabled ? arc(mins(s.quiet.start), mins(s.quiet.end), 62, "quiet") : "") +
    `<circle cx="${100 + 80 * Math.cos(now / 1440 * 2 * Math.PI - Math.PI / 2)}" cy="${100 + 80 * Math.sin(now / 1440 * 2 * Math.PI - Math.PI / 2)}" r="5" fill="#fff"/>` +
    `<text x="100" y="96">${s.enabled ? `${s.on} – ${s.off}` : "Always on"}</text><text class="lbl" x="100" y="116">${s.quiet.enabled ? `quiet ${s.quiet.start}–${s.quiet.end} · max ${s.quiet.max_brightness}%` : "no quiet hours"}</text>`;
}
views.schedule = async () => { views.current = "schedule"; schedule = await api("/api/schedule"); bindSchedule(); };

/* ---- settings ---- */
async function loadSettings() { settings = await api("/api/settings"); }
function bindSettings() {
  const m = settings.matrix, p = settings.power, q = settings.mqtt;
  $("#m-mapping").value = m.hardware_mapping; $("#m-size").value = m.panel_size; $("#m-slow").value = m.gpio_slowdown; $("#m-pwm").value = m.pwm_bits;
  $("#p-ceiling").value = p.ceiling_amps; $("#p-perpanel").value = p.amps_full_white_per_panel;
  $("#mq-enabled").checked = q.enabled; $("#mq-host").value = q.host; $("#mq-port").value = q.port; $("#mq-user").value = q.username; $("#mq-pass").value = q.password;
  $("#ddp-enabled").checked = settings.ddp.enabled;
  renderPanels();
}
function renderPanels() {
  $("#m-panels").replaceChildren(h("div", { class: "grid4", style: "font-size:13px;color:var(--muted);margin-bottom:4px" }, h("span", {}, "#"), h("span", {}, "col"), h("span", {}, "row"), h("span", {}, "rotate")),
    ...settings.matrix.panels.map((pn, i) => h("div", { class: "grid4", style: "margin-bottom:6px;align-items:center" }, h("span", { class: "num" }, String(i + 1)),
      h("input", { class: "input", type: "number", min: 0, max: 7, value: pn.col, "aria-label": `Panel ${i + 1} column`, onchange: (e) => { pn.col = +e.target.value; } }),
      h("input", { class: "input", type: "number", min: 0, max: 7, value: pn.row, "aria-label": `Panel ${i + 1} row`, onchange: (e) => { pn.row = +e.target.value; } }),
      h("select", { class: "input", "aria-label": `Panel ${i + 1} rotation`, onchange: (e) => { pn.rotate = +e.target.value; } }, [0, 90, 180, 270].map((r) => { const o = h("option", { value: r }, `${r}°`); o.selected = (pn.rotate || 0) === r; return o; })))));
}
$("#m-panel-add").onclick = () => { settings.matrix.panels.push({ col: 0, row: settings.matrix.panels.length, rotate: 0 }); renderPanels(); };
$("#m-panel-del").onclick = () => { if (settings.matrix.panels.length > 1) { settings.matrix.panels.pop(); renderPanels(); } };
async function putSettings(okMsg) {
  try { const r = await api("/api/settings", { method: "PUT", body: settings }); settings = r; bindSettings(); toast(r.restart_required ? "Saved. Restart the engine to apply matrix options." : okMsg); applyStatus(await api("/api/status")); }
  catch (e) { toast(e.message, true); }
}
$("#matrix-save").onclick = () => { Object.assign(settings.matrix, { hardware_mapping: $("#m-mapping").value, panel_size: +$("#m-size").value, gpio_slowdown: +$("#m-slow").value, pwm_bits: +$("#m-pwm").value }); settings.power = { ceiling_amps: +$("#p-ceiling").value, amps_full_white_per_panel: +$("#p-perpanel").value }; putSettings("Saved"); };
$("#integrations-save").onclick = () => { settings.mqtt = { enabled: $("#mq-enabled").checked, host: $("#mq-host").value.trim(), port: +$("#mq-port").value || 1883, username: $("#mq-user").value, password: $("#mq-pass").value }; settings.ddp = { enabled: $("#ddp-enabled").checked }; putSettings("Saved. MQTT and DDP are used from milestone 7."); };
$("#panel-test").onclick = async () => { try { await api("/api/mode", { method: "POST", body: { mode: "pattern", pattern: "panel-id" } }); toast("Panel ID pattern is on the wall"); } catch (e) { toast(e.message, true); } };
$("#set-password").onclick = () => {
  const curp = h("input", { class: "input", type: "password", autocomplete: "current-password", required: "" }), np = h("input", { class: "input", type: "password", autocomplete: "new-password", minlength: 8, required: "" }), np2 = h("input", { class: "input", type: "password", autocomplete: "new-password", required: "" }), err = h("div", { class: "err" });
  openSheet(h("form", { onsubmit: async (e) => { e.preventDefault(); if (np.value !== np2.value) { err.textContent = "New passwords do not match"; return; } try { await api("/api/auth/password", { method: "POST", body: { current: curp.value, new: np.value } }); closeSheet(); toast("Password changed. Other devices need to sign in again."); } catch (x) { err.textContent = x.message; } } },
    h("h3", {}, "Change admin password"), h("label", { class: "field" }, h("span", {}, "Current password"), curp), h("label", { class: "field" }, h("span", {}, "New password (8+ characters)"), np), h("label", { class: "field" }, h("span", {}, "Repeat new password"), np2), err,
    h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: closeSheet }, "Cancel"), h("button", { class: "btn primary", type: "submit" }, "Change"))));
  curp.focus();
};
async function renderTokens() {
  const toks = await api("/api/tokens");
  const revoke = async (t) => {
    if (await confirmSheet("Revoke token?", `Anything using "${t.name}" will stop working.`, "Revoke")) { await api(`/api/tokens/${t.id}`, { method: "DELETE" }); renderTokens(); }
  };
  const create = async () => {
    const n = await promptSheet("New API token", "What will use it? (e.g. Home Assistant)", "", "Create");
    if (!n) return;
    const t = await api("/api/tokens", { method: "POST", body: { name: n } });
    renderTokens();
    openSheet(h("h3", {}, "Token created"), h("p", { class: "muted" }, "Copy it now; it is not shown again."),
      h("div", { class: "token-box mono" }, t.token),
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(t.token).then(() => toast("Copied")); } }, "Copy"),
        h("button", { class: "btn primary", onclick: closeSheet }, "Done")));
  };
  $("#token-list").replaceChildren(
    ...toks.map((t) => h("div", { class: "row" },
      h("div", { class: "grow" }, h("span", { class: "label" }, t.name), h("span", { class: "sub" }, `Created ${new Date(t.created * 1000).toLocaleDateString()}`)),
      h("button", { class: "btn sm danger", onclick: () => revoke(t) }, "Revoke"))),
    h("button", { class: "row link", onclick: create },
      h("div", { class: "grow" }, h("span", { class: "label", style: "color:var(--accent)" }, "+ Create token"))));
}
views.settings = async () => { views.current = "settings"; await loadSettings(); bindSettings(); renderTokens(); };

/* ---- init ---- */
(async () => {
  renderer = new DotRenderer($("#preview"), 128, 192, 2.2);
  placePreview();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try { applyStatus(await api("/api/status")); } catch { return; }
  renderer.setSize(status.canvas.w, status.canvas.h);
  modes = await api("/api/modes").catch(() => []);
  await loadMedia().catch(() => {});
  route();
  connectPreview();
  setTimeout(pollStatus, 3000);
})();
})();
