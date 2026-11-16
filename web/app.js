"use strict";

const DAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const DAY_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
const MODE_TEXT = { manual: "Manuell", auto: "Automatik", boost: "Boost", holiday: "Urlaub", standby: "Standby",
                    off: "Aus", boost_off: "Boost beendet" };
const MODE_BUTTONS = [["manual", "Manuell"], ["auto", "Automatik"], ["boost", "Boost 30 min"], ["off", "Aus"]];
const BOOST_MINUTES = 30;
const START_TIMES = [];
for (let m = 0; m < 24 * 60; m += 15) START_TIMES.push(hhmm(m));
const END_TIMES = START_TIMES.slice(1).concat("23:59");

const WEEK = 7 * 1440;                 // minutes
const SETUP_IDLE_MS = 10 * 60 * 1000;  // the setup closes itself after 10 minutes without a touch

let state = null;
let stateAt = 0;         // Date.now() when state arrived (state.now is the server's local time then)
let view = sessionStorage.getItem("heizung.view") === "setup" ? "setup" : "home";  // "home" | "setup"
let dialog = null;       // open dialog: {title, text, fields, buttons, onSubmit, error, resolve}
let pinPromise = null;   // a PIN dialog that is already open
let setupIdleTimer = null;
let tab = localStorage.getItem("heizung.tab") || "rooms";
let editing = null;      // plan in the editor: {id|null, name, days}
let roomForm = null;     // room form in "Räume": {mode: "new"|"edit", id, name, heaters: [ids]}
let deviceForm = null;   // heater form in "Geräte": {mode: "new"|"edit", id, name, host, token, hasToken, removeToken}
let deviceFilter = localStorage.getItem("heizung.deviceFilter") || "all";  // "all" | "none" | room id
let calibForm = null;    // calibration in "Geräte": {id, measured}
let scan = null;         // network search in "Geräte": {running} | {data} | {error}
let scanNet = "";        // network typed for "Anderes Netz"
const pending = {};      // debounced setpoint changes: cardKey -> {temp, timer}
const busy = new Set();  // cards with a running action

// ------------------------------------------------------------------ helpers
function hhmm(min) { return String(Math.floor(min / 60)).padStart(2, "0") + ":" + String(min % 60).padStart(2, "0"); }
function minutes(t) { const [h, m] = t.split(":").map(Number); return h * 60 + m; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function deg(t) { return t == null || Number.isNaN(t) ? "–" : t.toFixed(1).replace(".", ",") + " °C"; }
function avg(list) { return list.reduce((a, b) => a + b, 0) / list.length; }
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–";
}
function tempColor(t) { const x = Math.max(0, Math.min(1, (t - 15) / 10)); return `hsl(${210 - 190 * x}, 72%, ${50 - 8 * x}%)`; }

async function api(method, url, body, retried = false) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  let data = {};
  try { data = await res.json(); } catch (_) { /* no JSON body */ }
  if (res.status === 403 && data.pin_required && !retried) {   // setup locked (again): ask for the PIN, then retry once
    if (await unlockSetup(true)) return api(method, url, body, true);
    throw new Error("Abgebrochen – ohne PIN lässt sich die Einrichtung nicht ändern.");
  }
  if (!res.ok) throw new Error(data.error || `Fehler ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg, error = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = error ? "error" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), error ? 9000 : 4000);
}

function report(title, data) {
  const results = (data && data.results) || [];
  const failed = results.filter(r => !r.ok);
  if (!results.length) toast(`${title}: nichts zu tun`);
  else if (failed.length) toast(failed.map(r => `${r.name}: ${r.message}`).join(" · "), true);
  else toast(`${title}: ${results.map(r => r.name).join(", ")} – ${results[0].message}`);
}

async function act(cardKey, fn) {
  if (cardKey) busy.add(cardKey);
  render();
  try { await fn(); } catch (e) { toast(e.message, true); invalidate(); }
  finally { if (cardKey) busy.delete(cardKey); await refresh(); }
}

function invalidate() {  // force a redraw, e.g. to undo a select the server refused
  for (const el of document.querySelectorAll(".tab, .tiles")) el._html = null;
}

// ------------------------------------------------------------------ data + rendering
async function refresh() {
  try {
    state = await api("GET", "api/state");
    stateAt = Date.now();
    const on = state.heaters.filter(h => h.online).length;
    setStatus(`${on}/${state.heaters.length} Heizung(en) online · ${new Date().toLocaleTimeString("de-DE")}`);
    setBanner(null);
    showAppInfo(state.app);
    if (view === "setup" && !state.setup.unlocked && !pinPromise) showView("home");  // e.g. the app was restarted
  } catch (e) {
    setStatus("keine Verbindung zur App", true);
    setBanner("Keine Verbindung zur Heizungs-App. Es wird gleich noch einmal versucht.");
  }
  render();
}

function showAppInfo(app) {  // footer of the setup: name, version, licence, source code
  if (!app) return;
  setHTML(document.getElementById("app-info"),
    `${esc(app.name)} ${esc(app.version)} · Lizenz ${esc(app.license)} · ` +
    `<a href="${esc(app.repo)}" target="_blank" rel="noopener">Quellcode auf GitHub</a>`);
}

function setBanner(text) {
  const el = document.getElementById("home-banner");
  el.textContent = text || "";
  el.hidden = !text;
}

function showView(name) {
  view = name;
  if (name === "setup") sessionStorage.setItem("heizung.view", "setup");
  else sessionStorage.removeItem("heizung.view");
  document.body.className = "view-" + name;
  touchSetup();
  window.scrollTo(0, 0);
}

function setHTML(el, html) {  // only touch the DOM if something changed (keeps open dropdowns and focus)
  if (el._html === html) return;
  el._html = html;
  el.innerHTML = html;
}

function setStatus(text, error = false) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = error ? "error-text" : "muted";
}

function render() {
  document.getElementById("home").hidden = view !== "home";
  document.getElementById("setup").hidden = view !== "setup";
  if (view === "home") return renderHome();
  if (state) {
    document.getElementById("pin-button").textContent = state.setup.pin ? "PIN ändern" : "PIN festlegen";
    document.getElementById("pin-warning").hidden = state.setup.pin;
  }
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const name of ["rooms", "plans", "devices"]) document.getElementById("tab-" + name).hidden = name !== tab;
  if (!state) return;
  const el = document.getElementById("tab-" + tab);
  const focus = document.activeElement;
  if (el.contains(focus) && focus.matches("input, select")) return;  // never disturb typing
  if (tab === "rooms") renderRooms(el);
  else if (tab === "plans") { if (!editing) renderPlans(el); }       // the editor renders itself
  else renderDevices(el);
}

// ------------------------------------------------------------------ simple view for residents
function degShort(t) {  // targets come in 0.5 steps: 21 -> "21 °C", 21.5 -> "21,5 °C"
  if (t == null || Number.isNaN(t)) return "–";
  const r = Math.round(t * 10) / 10;
  return (Number.isInteger(r) ? String(r) : r.toFixed(1).replace(".", ",")) + " °C";
}
function tempWord(t) { return t == null ? "aus" : degShort(t); }   // null = gap in the plan (frost protection)
function uhr(min) { const h = Math.floor(min / 60) % 24, m = min % 60; return m ? `${h}:${String(m).padStart(2, "0")} Uhr` : `${h} Uhr`; }

function serverNow() {  // the server's local time (the heaters' clocks follow it), advanced since the last refresh
  const m = state && /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)/.exec(state.now);
  if (!m) return new Date();
  return new Date(new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() + (Date.now() - stateAt));
}
function weekMinute(d) { return ((d.getDay() + 6) % 7) * 1440 + d.getHours() * 60 + d.getMinutes(); }  // Monday 0:00 = 0

function weekWindows(plan) {
  // the whole week as [{from, to, temp}] in minutes, window by window; temp null = gap (frost protection)
  const segs = [];
  for (let d = 0; d < 7; d++) {
    const ws = (plan.days[String(d + 1)] || []).map(w => ({ s: minutes(w.start), e: w.end === "23:59" ? 1440 : minutes(w.end), temp: w.temp }))
      .sort((a, b) => a.s - b.s);
    let cursor = 0;
    for (const w of ws) {
      if (w.s > cursor) segs.push({ from: d * 1440 + cursor, to: d * 1440 + w.s, temp: null });
      segs.push({ from: d * 1440 + w.s, to: d * 1440 + w.e, temp: w.temp });
      cursor = Math.max(cursor, w.e);
    }
    if (cursor < 1440) segs.push({ from: d * 1440 + cursor, to: (d + 1) * 1440, temp: null });
  }
  return segs;
}

function weekSegments(windows) {  // neighbours with the same temperature merged (also across midnight)
  const merged = [];
  for (const s of windows) {
    const last = merged[merged.length - 1];
    if (last && last.temp === s.temp && last.to === s.from) last.to = s.to; else merged.push(Object.assign({}, s));
  }
  return merged;
}

function planNow(plan, at) {
  // what the plan does now and when that changes: {temp, until, next, windowEnd} | {constant, temp}
  // windowEnd: end of the current time window – there a changed setpoint falls back to the plan
  const windows = weekWindows(plan);
  const segs = weekSegments(windows);
  const i = segs.findIndex(s => at >= s.from && at < s.to);
  if (i < 0) return null;
  const cur = segs[i];
  if (segs.length === 1) return { constant: true, temp: cur.temp };
  let until = cur.to, next = segs[i + 1];
  if (!next) {                      // Sunday night: continue with Monday
    next = segs[0];
    if (next.temp === cur.temp) { until = WEEK + next.to; next = segs[1]; }
  }
  const win = windows.find(s => at >= s.from && at < s.to);
  return { temp: cur.temp, until, next: next.temp, windowEnd: win ? win.to : until };
}

function whenText(at, until) {  // "22 Uhr", "Mitternacht", "morgen 6 Uhr", "Mittwoch 6:30 Uhr"
  const days = Math.floor(until / 1440) - Math.floor(at / 1440);
  const minute = until % 1440;
  if (days === 1 && minute === 0) return "Mitternacht";
  if (days === 0) return uhr(minute);
  if (days === 1) return "morgen " + uhr(minute);
  return DAY_LONG[Math.floor(until / 1440) % 7] + " " + uhr(minute);
}

function planOfGroup(g) {  // the app's plan, if the room's heaters run exactly that one
  if (g.kind !== "room" || !g.room.plan || g.room.plan_outdated) return null;
  if (g.heaters.some(h => h.online && h.plan_slot && h.plan_slot !== 1)) return null;
  return state.plans.find(p => p.id === g.room.plan) || null;
}

function planText(g, online, target) {
  const plan = planOfGroup(g);
  if (!plan) return "Läuft nach dem Heizplan der Heizung.";
  const at = weekMinute(serverNow());
  const p = planNow(plan, at);
  if (!p) return "Läuft nach Heizplan.";
  if (p.constant) return `Nach Plan: immer ${tempWord(p.temp)}.`;
  const frost = (online.find(h => h.antifrost != null) || {}).antifrost ?? 5;
  const near = (a, b) => a != null && Math.abs(a - b) < 0.25;
  const when = whenText(at, p.until);
  if (target == null || near(target, p.temp ?? frost)) {
    return p.temp == null ? `Nach Plan aus bis ${when}, danach ${tempWord(p.next)}.`
                          : `Nach Plan: ${tempWord(p.temp)} bis ${when}, danach ${tempWord(p.next)}.`;
  }
  // the heater starts early to be warm in time ("Smart Start")
  if (near(target, p.next ?? frost) && p.until - at <= 180) return `Nach Plan: ab ${when} ${tempWord(p.next)}.`;
  return `Bis ${whenText(at, p.windowEnd)} auf ${degShort(target)} gestellt, danach wieder nach Plan.`;
}

function homeStatus(g, online, mode, target, writable) {
  if (!online.length) return { text: "Keine Verbindung zur Heizung.", tone: "bad" };
  if (online.some(h => h.window_open)) return { text: "Fenster offen – die Heizung macht eine Pause.", tone: "warn" };
  let text = "";
  if (mode === "off" || mode === "standby") text = "Ausgeschaltet – schützt nur vor Frost.";
  else if (mode === "holiday") text = "Urlaubsmodus – heizt nur wenig.";
  else if (mode === "boost") {
    const h = online.find(x => x.mode === "boost" && x.boost_remaining);
    const min = h ? parseInt(String(h.boost_remaining), 10) : NaN;
    text = "Heizt mit voller Kraft" + (Number.isFinite(min) ? ` – noch ${min} ${min === 1 ? "Minute" : "Minuten"}.` : ".");
  } else if (mode === "manual") text = planOfGroup(g) ? "Fest eingestellt – der Heizplan pausiert." : "Hält die eingestellte Temperatur.";
  else if (mode === "auto") text = planText(g, online, target);
  else if (mode === "mixed") text = "Die Heizungen in diesem Raum sind unterschiedlich eingestellt.";
  if (!writable) text += " (Nur Anzeige.)";
  return { text, tone: "" };
}

function homeTile(g) {
  const key = keyOf(g);
  const online = g.heaters.filter(h => h.online);
  const temps = online.map(h => h.room_temp).filter(t => t != null);
  const writable = online.some(h => h.writable);
  const p = pending[key];
  const target = p ? p.temp : currentTarget(g);
  const mode = groupMode(g);
  const heating = online.some(h => h.heating);
  const isOff = ["off", "standby", "holiday"].includes(mode);
  const say = homeStatus(g, online, mode, target, writable);
  const dis = writable ? "" : "disabled";
  const actions = [];
  if (online.length) {
    if (isOff) {
      actions.push(`<button class="big-btn primary" data-action="home-on" ${dis}>${mode === "holiday" ? "Wieder normal heizen" : "Einschalten"}</button>`);
    } else {
      if (mode === "boost") actions.push(`<button class="big-btn" data-action="home-boost-off" ${dis}>Volle Kraft beenden</button>`);
      if (mode === "manual" && planOfGroup(g)) actions.push(`<button class="big-btn" data-action="home-plan" ${dis}>Wieder nach Plan</button>`);
      actions.push(`<button class="big-btn quiet" data-action="home-off" ${dis}>Ausschalten</button>`);
    }
  }
  const setter = isOff || !online.length ? "" : `<div class="tile-set ${p ? "pending" : ""}">
      <button class="step cold" data-action="home-sp" data-delta="-0.5" ${dis} aria-label="Kälter"><span class="sym">−</span><span class="word">kälter</span></button>
      <div class="set-val"><span class="set-temp">${degShort(target)}</span><span class="set-label">eingestellt</span></div>
      <button class="step warm" data-action="home-sp" data-delta="0.5" ${dis} aria-label="Wärmer"><span class="sym">+</span><span class="word">wärmer</span></button>
    </div>`;
  return `<article class="tile ${isOff ? "is-off" : ""} ${busy.has(key) ? "busy" : ""}" data-kind="${g.kind}" data-id="${esc(g.id)}">
    <div class="tile-head"><h2>${esc(g.name)}</h2>${heating && !isOff ? '<span class="heat-badge">heizt gerade</span>' : ""}</div>
    <div class="tile-now"><span class="now-temp">${deg(temps.length ? avg(temps) : null)}</span><span class="now-label">im Raum</span></div>
    ${setter}
    <p class="say ${say.tone}">${esc(say.text)}</p>
    ${actions.length ? `<div class="tile-actions">${actions.join("")}</div>` : ""}
  </article>`;
}

function renderHome() {
  const now = new Date();
  document.getElementById("clock").textContent =
    now.toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" }) + " · " +
    now.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) + " Uhr";
  if (!state) return;
  const gs = groups().filter(g => g.heaters.length);
  setHTML(document.getElementById("tiles"), gs.length ? gs.map(homeTile).join("")
    : '<p class="home-empty">Noch keine Heizung eingerichtet. Das geht über „Einrichtung“ oben rechts.</p>');
}

function homeReport(g, okText, data) {
  const failed = ((data && data.results) || []).filter(r => !r.ok);
  if (failed.length) toast(`${g.name}: Das hat nicht geklappt (${failed.map(r => r.message).join("; ")}).`, true);
  else toast(`${g.name}: ${okText}.`);
}

function homeMode(g, mode, okText) {
  if (!g) return;
  act(keyOf(g), async () => homeReport(g, okText, await api("POST", apiBase(g) + "/mode", { mode })));
}

async function homeOff(g) {
  if (!g) return;
  const yes = await ask({
    title: `${g.name} ausschalten?`,
    text: "Die Heizung schützt dann nur noch vor Frost. Mit „Einschalten“ heizt sie später wieder wie vorher.",
    buttons: [{ label: "Nein", value: null }, { label: "Ja, ausschalten", value: "yes", cls: "danger-solid" }],
  });
  if (yes) homeMode(g, "off", "ausgeschaltet");
}

// ------------------------------------------------------------------ dialog (confirmations and PIN)
function ask(opts) {  // resolves with the chosen button's value (null = cancelled) or, after onSubmit, the field values
  if (dialog) closeDialog(null);
  return new Promise(resolve => {
    dialog = Object.assign({ text: "", fields: [], error: "", busy: false }, opts, { resolve });
    renderDialog();
  });
}

function renderDialog() {
  const el = document.getElementById("dialog");
  if (!dialog) { el.hidden = true; el.innerHTML = ""; return; }
  const d = dialog;
  el.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
    <h2 id="dialog-title">${esc(d.title)}</h2>
    ${d.text ? `<p>${esc(d.text)}</p>` : ""}
    ${d.fields.map(f => `<label>${esc(f.label)}<input data-dialog-field="${esc(f.id)}" type="password" inputmode="numeric"
      pattern="[0-9]*" maxlength="8" autocomplete="off" ${d.busy ? "disabled" : ""}></label>`).join("")}
    <p class="dialog-error" ${d.error ? "" : "hidden"}>${esc(d.error)}</p>
    <div class="dialog-buttons">${d.buttons.map((b, i) =>
      `<button data-dialog-button="${i}" class="${b.cls || ""}" ${d.busy ? "disabled" : ""}>${esc(b.label)}</button>`).join("")}</div>
  </div>`;
  el.hidden = false;
  const first = el.querySelector("input");
  if (first) first.focus();
}

function closeDialog(value) {
  const d = dialog;
  dialog = null;
  renderDialog();
  if (d) d.resolve(value);
}

async function dialogButton(i) {
  const d = dialog;
  if (!d || d.busy) return;
  const b = d.buttons[i];
  if (b.value !== "submit" || !d.onSubmit) return closeDialog(b.value);
  const values = {};
  for (const input of document.querySelectorAll("#dialog [data-dialog-field]")) values[input.dataset.dialogField] = input.value.trim();
  d.busy = true;
  d.error = "";
  renderDialog();
  let error = null;
  try { error = await d.onSubmit(values); } catch (e) { error = e.message; }
  if (dialog !== d) return;
  d.busy = false;
  if (error) { d.error = error; renderDialog(); return; }
  closeDialog(values);
}

// ------------------------------------------------------------------ setup: PIN, open / close
function unlockSetup(force) {
  if (!force && state && state.setup.unlocked) return Promise.resolve(true);
  if (pinPromise) return pinPromise;
  pinPromise = ask({
    title: "Einrichtung",
    text: "Bitte die PIN eingeben.",
    fields: [{ id: "pin", label: "PIN" }],
    buttons: [{ label: "Abbrechen", value: null }, { label: "Öffnen", value: "submit", cls: "primary" }],
    onSubmit: async v => {
      if (!v.pin) return "Bitte die PIN eingeben.";
      try { await api("POST", "api/setup/unlock", { pin: v.pin }, true); return null; } catch (e) { return e.message; }
    },
  }).then(v => v != null).finally(() => { pinPromise = null; });
  return pinPromise;
}

async function openSetup() {
  if (!(await unlockSetup(false))) return;
  showView("setup");
  await refresh();
}

async function closeSetup(auto) {
  if ((editing || roomForm || deviceForm || calibForm) && !auto) {
    const yes = await ask({ title: "Einrichtung schließen?", text: "Nicht gespeicherte Änderungen gehen dabei verloren.",
      buttons: [{ label: "Weiter bearbeiten", value: null }, { label: "Schließen", value: "yes", cls: "primary" }] });
    if (!yes) return;
  }
  editing = roomForm = deviceForm = calibForm = null;
  invalidate();
  showView("home");
  render();
  try { await api("POST", "api/setup/lock", undefined, true); } catch (_) { /* offline: the session runs out anyway */ }
  await refresh();
}

function touchSetup() {  // the setup closes itself after SETUP_IDLE_MS without a touch (not while something is being edited)
  clearTimeout(setupIdleTimer);
  if (view !== "setup") return;
  setupIdleTimer = setTimeout(() => {
    if (view !== "setup") return;
    if (editing || roomForm || deviceForm || calibForm || dialog) return touchSetup();
    closeSetup(true);
  }, SETUP_IDLE_MS);
}

async function pinSettings() {
  const has = state && state.setup.pin;
  const buttons = [{ label: "Abbrechen", value: null }];
  if (has) buttons.push({ label: "PIN entfernen", value: "remove", cls: "danger" });
  buttons.push({ label: "Speichern", value: "submit", cls: "primary" });
  const result = await ask({
    title: has ? "PIN ändern" : "PIN festlegen",
    text: "4 bis 8 Ziffern. Die PIN schützt nur die Einrichtung – die einfache Ansicht bleibt für alle bedienbar.",
    fields: [{ id: "pin", label: "Neue PIN" }, { id: "again", label: "Neue PIN wiederholen" }],
    buttons,
    onSubmit: async v => {
      if (!/^\d{4,8}$/.test(v.pin)) return "Die PIN muss aus 4 bis 8 Ziffern bestehen.";
      if (v.pin !== v.again) return "Die beiden Eingaben sind verschieden.";
      try { toast((await api("PUT", "api/setup/pin", { pin: v.pin })).message); return null; } catch (e) { return e.message; }
    },
  });
  if (result === "remove") {
    const yes = await ask({ title: "PIN entfernen?", text: "Danach kann jeder die Einrichtung öffnen.",
      buttons: [{ label: "Nein", value: null }, { label: "Ja, entfernen", value: "yes", cls: "danger-solid" }] });
    if (yes) { try { toast((await api("DELETE", "api/setup/pin")).message); } catch (e) { toast(e.message, true); } }
  }
  await refresh();
}

// ------------------------------------------------------------------ tab: rooms
function groups() {
  const byId = Object.fromEntries(state.heaters.map(h => [h.id, h]));
  const rooms = state.rooms.map(r => ({ kind: "room", id: r.id, name: r.name, room: r,
                                        heaters: r.heaters.map(id => byId[id]).filter(Boolean) }));
  const loose = state.heaters.filter(h => !h.room).map(h => ({ kind: "heater", id: h.id, name: h.name, heaters: [h] }));
  return rooms.concat(loose);
}
function keyOf(g) { return g.kind + ":" + g.id; }
function groupFor(card) { return card && groups().find(g => g.kind === card.dataset.kind && g.id === card.dataset.id); }
function apiBase(g) { return (g.kind === "room" ? "api/rooms/" : "api/heaters/") + encodeURIComponent(g.id); }
function currentTarget(g) { const h = g.heaters.find(h => h.online && h.setpoint != null); return h ? h.setpoint : null; }
function groupMode(g) {
  const modes = new Set(g.heaters.filter(h => h.online && h.mode).map(h =>
    h.mode === "manual" && h.antifrost != null && h.setpoint != null && h.setpoint <= h.antifrost ? "off" : h.mode));
  return modes.size === 1 ? [...modes][0] : modes.size ? "mixed" : null;
}

function renderRooms(el) {
  el.className = "tab";
  const gs = groups();
  const adding = roomForm && roomForm.mode === "new";
  const head = `<div class="tab-tools"><button class="small primary" data-action="room-new" ${adding ? "disabled" : ""}>+ Raum</button></div>`
    + (adding ? `<article class="card"><div class="card-head"><h2>Neuer Raum</h2></div>${roomFormHTML(roomForm)}</article>` : "");
  if (!gs.length) { setHTML(el, head + '<p class="empty">Keine Heizungen konfiguriert – unter „Geräte“ über „+ Heizung“ anlegen.</p>'); return; }
  setHTML(el, head + gs.map(roomCard).join("") + (state.rooms.length || adding ? "" :
    '<p class="empty">Tipp: Über „+ Raum“ einen Raum anlegen und die Heizungen ankreuzen, die dazugehören. ' +
    'So lassen sich mehrere Heizungen gemeinsam steuern und Heizpläne senden.</p>'));
}

function roomFormHTML(f) {
  const saving = busy.has("roomform");
  const roomName = Object.fromEntries(state.rooms.map(r => [r.id, r.name]));
  const heaters = state.heaters.map(h => {
    const elsewhere = h.room && h.room !== f.id ? roomName[h.room] : null;
    return `<label class="check"><input type="checkbox" data-heater="${esc(h.id)}" ${f.heaters.includes(h.id) ? "checked" : ""}>
      <span>${esc(h.name)}${elsewhere ? ` <span class="muted">· bisher in „${esc(elsewhere)}“</span>` : ""}</span></label>`;
  }).join("");
  return `<div class="room-form">
    <label>Name<input data-field="name" value="${esc(f.name)}" placeholder="z. B. Wohnzimmer" autocomplete="off"></label>
    <fieldset><legend>Heizungen in diesem Raum</legend>${heaters || '<span class="muted">Noch keine Heizungen angelegt.</span>'}</fieldset>
    <div class="row">
      <button class="primary" data-action="room-save" ${saving ? "disabled" : ""}>${saving ? "speichere …" : f.mode === "new" ? "Anlegen" : "Speichern"}</button>
      <button data-action="room-cancel">Abbrechen</button>
      ${f.mode === "edit" ? '<button class="danger" data-action="room-delete">Raum löschen</button>' : ""}
    </div>
  </div>`;
}

function roomCard(g) {
  if (g.kind === "room" && roomForm && roomForm.mode === "edit" && roomForm.id === g.id) {
    return `<article class="card" data-kind="room" data-id="${esc(g.id)}">
      <div class="card-head"><h2>${esc(g.name)}</h2><span class="tag">bearbeiten</span></div>${roomFormHTML(roomForm)}</article>`;
  }
  const key = keyOf(g);
  const online = g.heaters.filter(h => h.online);
  const temps = online.map(h => h.room_temp).filter(t => t != null);
  const writable = online.some(h => h.writable);
  const p = pending[key];
  const target = p ? p.temp : currentTarget(g);
  const mode = groupMode(g);
  const heating = online.some(h => h.heating);
  const boosting = online.find(h => h.mode === "boost" && h.boost_remaining);
  const dis = writable ? "" : "disabled";
  const buttons = MODE_BUTTONS.map(([m, label]) => {
    const text = m === "boost" && boosting ? `Boost ${boosting.boost_remaining}` : label;
    return `<button data-action="mode" data-mode="${m}" class="${mode === m ? "active" : ""}" ${dis}>${esc(text)}</button>`;
  }).join("");
  return `<article class="card ${busy.has(key) ? "busy" : ""}" data-kind="${g.kind}" data-id="${esc(g.id)}">
    <div class="card-head">
      <h2>${esc(g.name)}</h2>
      ${g.kind === "heater" ? '<span class="tag">ohne Raum</span>' : ""}
      <span class="state ${heating ? "heating" : ""}">${online.length ? (heating ? "heizt" : "heizt nicht") : "offline"}</span>
      ${g.kind === "room" ? '<button class="small" data-action="room-edit">Bearbeiten</button>' : ""}
    </div>
    <div class="temps">
      <div><span class="big">${deg(temps.length ? avg(temps) : null)}</span><span class="label">Raumtemperatur</span></div>
      <div class="target ${p ? "pending" : ""}">
        <button class="round" data-action="sp" data-delta="-0.5" ${dis} aria-label="kälter">−</button>
        <div><span class="big">${deg(target)}</span><span class="label">${mode === "auto" ? "Soll bis Fensterende" : "Solltemperatur"}</span></div>
        <button class="round" data-action="sp" data-delta="0.5" ${dis} aria-label="wärmer">+</button>
      </div>
    </div>
    <div class="modes">${buttons}</div>
    ${g.kind === "room" ? planRow(g, writable) : '<p class="hint">Für Heizpläne diese Heizung unter „Geräte“ einem Raum zuordnen.</p>'}
    <ul class="heater-list">${g.heaters.map(heaterLine).join("")}</ul>
    ${!writable && online.length ? '<p class="hint">Nur Anzeige: In heizungen.conf fehlt der Token.</p>' : ""}
  </article>`;
}

function planRow(g, writable) {
  const r = g.room;
  const options = ['<option value="">– kein Heizplan –</option>'].concat(state.plans.map(p =>
    `<option value="${esc(p.id)}" ${p.id === r.plan ? "selected" : ""}>${esc(p.name)}</option>`)).join("");
  let hint = "Kein Heizplan zugeordnet.";
  if (r.plan) hint = r.plan_outdated ? "⚠︎ Plan ist noch nicht (vollständig) auf den Heizungen – „Senden“ tippen."
                                     : `Auf den Heizungen seit ${fmtTime(r.plan_sent_at)}.`;
  return `<div class="plan-row">
      <select data-action="room-plan" aria-label="Heizplan">${options}</select>
      <button data-action="send-plan" ${r.plan && writable ? "" : "disabled"}>Senden</button>
    </div>
    <p class="hint">${esc(hint)}</p>`;
}

function heaterLine(h) {
  const dot = !h.online ? "offline" : h.heating ? "on" : "";
  const tags = [];
  if (h.window_open) tags.push('<span class="tag warn">Fenster offen</span>');
  if (h.mode && h.mode !== "manual") tags.push(`<span class="tag">${esc(MODE_TEXT[h.mode] || h.mode)}</span>`);
  if (!h.writable) tags.push('<span class="tag">nur lesen</span>');
  if (h.online && h.clock_drift != null && Math.abs(h.clock_drift) > 60) tags.push('<span class="tag bad">Uhr falsch</span>');
  return `<li><span class="dot ${dot}"></span><span class="hname">${esc(h.name)}</span>
    <span class="hvals">${h.online ? `${deg(h.room_temp)} → ${deg(h.setpoint)}` : "offline"}</span>${tags.join("")}</li>`;
}

function changeSetpoint(card, delta) {
  const g = groupFor(card);
  if (!g) return;
  const key = keyOf(g);
  const start = pending[key] ? pending[key].temp : (currentTarget(g) ?? 20);
  const temp = Math.min(30, Math.max(5, Math.round((start + delta) * 2) / 2));
  if (pending[key]) clearTimeout(pending[key].timer);
  pending[key] = { temp, timer: setTimeout(() => commitSetpoint(g, temp), 1000) };
  render();
}

function commitSetpoint(g, temp) {
  const key = keyOf(g);
  act(key, async () => {
    try {
      const data = await api("POST", apiBase(g) + "/setpoint", { temp });
      if (view === "home") homeReport(g, `${degShort(temp)} eingestellt`, data);
      else report("Solltemperatur", data);
    } finally { delete pending[key]; }
  });
}

function setMode(card, mode) {
  const g = groupFor(card);
  if (!g) return;
  if (mode === "boost" && groupMode(g) === "boost") mode = "boost_off";
  if (mode === "auto") {
    const fresh = g.kind === "room" && g.room.plan && !g.room.plan_outdated;
    if (!fresh && !confirm("Auf den Heizungen ist kein aktueller Heizplan aus der App. Im Automatikbetrieb halten sie " +
        "außerhalb der Zeitfenster nur die Frostschutztemperatur (meist 5 °C).\n\nTrotzdem auf Automatik schalten?")) return;
  }
  act(keyOf(g), async () => report(MODE_TEXT[mode], await api("POST", apiBase(g) + "/mode", { mode, minutes: BOOST_MINUTES })));
}

function sendPlan(card) {
  const g = groupFor(card);
  if (!g) return;
  act(keyOf(g), async () => report("Heizplan", await api("POST", apiBase(g) + "/send-plan")));
}

function assignPlan(card, planId) {
  const g = groupFor(card);
  if (!g) return;
  act(keyOf(g), async () => {
    await api("PATCH", apiBase(g), { plan: planId || null });
    toast(planId ? "Heizplan zugeordnet – zum Übertragen „Senden“ tippen." : "Heizplan entfernt.");
  });
}

// ------------------------------------------------------------------ tab: plans
function bar(windows) {
  return `<div class="bar">${windows.map(w => {
    const s = minutes(w.start), e = minutes(w.end) + (w.end === "23:59" ? 1 : 0);
    const width = (e - s) / 14.4;
    const label = String(w.temp).replace(".", ",");
    const fits = width >= label.length * 2 + 1.5;   // "17" from ~5.5 %, "21,5" from ~9.5 % of the day
    return `<span class="seg" style="left:${s / 14.4}%;width:${width}%;background:${tempColor(w.temp)}"
      title="${w.start}–${w.end} · ${deg(w.temp)}">${fits ? label : ""}</span>`;
  }).join("")}</div>`;
}
function hourScale() {
  return `<div class="hours" aria-hidden="true">${[0, 3, 6, 9, 12, 15, 18, 21, 24].map(h =>
    `<span style="left:${h / 24 * 100}%">${h}</span>`).join("")}</div>`;
}
function weekBars(days) {
  return `<div class="week">${DAY_SHORT.map((d, i) =>
    `<div class="week-row"><span>${d}</span>${bar(days[String(i + 1)] || [])}</div>`).join("")}
    <div class="week-row"><span></span>${hourScale()}</div></div>`;
}

function renderPlans(el) {
  el.className = "tab";
  const roomName = Object.fromEntries(state.rooms.map(r => [r.id, r.name]));
  setHTML(el, `<article class="card">
      <div class="card-head"><h2>Heizpläne</h2><button class="primary" data-action="plan-new">Neuer Heizplan</button></div>
      <p class="hint">Pläne werden hier verwaltet, unter „Räume“ einem Raum zugeordnet und an dessen Heizungen gesendet.
      Die Heizungen führen den Plan im Modus „Automatik“ selbst aus – auch wenn diese App nicht läuft.</p>
    </article>` + state.plans.map(p => `<article class="card" data-plan="${esc(p.id)}">
      <div class="card-head"><h2>${esc(p.name)}</h2></div>
      ${weekBars(p.days)}
      <p class="hint">${p.rooms.length ? "Verwendet in: " + esc(p.rooms.map(id => roomName[id]).join(", ")) : "Noch keinem Raum zugeordnet."}
        ${p.source ? `<br>Aus der Heizung ausgelesen (Plan ${esc(p.source.slot)}) am ${fmtTime(p.source.at)}.` : ""}</p>
      <div class="row"><button data-action="plan-edit">Bearbeiten</button><button data-action="plan-copy">Duplizieren</button>
        <button class="danger" data-action="plan-delete">Löschen</button></div>
    </article>`).join(""));
}

function emptyDays() { const d = {}; for (let i = 1; i <= 7; i++) d[String(i)] = []; return d; }

function startEdit(plan) {
  editing = plan ? { id: plan.id, name: plan.name, days: Object.assign(emptyDays(), clone(plan.days)) }
                 : { id: null, name: "", days: emptyDays() };
  renderEditor();
  window.scrollTo(0, 0);
}

function timeOptions(list, value) {
  // plans read from a heater may use any minute: show such a value too instead of silently changing it
  const all = value && !list.includes(value) ? list.concat(value).sort() : list;
  return all.map(t => `<option ${t === value ? "selected" : ""}>${t}</option>`).join("");
}

function renderEditor() {
  const el = document.getElementById("tab-plans");
  el.className = "tab single";
  const days = DAY_LONG.map((name, i) => {
    const d = String(i + 1);
    const ws = editing.days[d];
    return `<section class="day" data-day="${d}">
      <div class="day-head"><strong>${name}</strong>${bar(ws)}</div>
      ${ws.map((w, idx) => `<div class="win" data-idx="${idx}">
        <select data-field="start" aria-label="Start">${timeOptions(START_TIMES, w.start)}</select> –
        <select data-field="end" aria-label="Ende">${timeOptions(END_TIMES, w.end)}</select>
        <input type="number" data-field="temp" min="5" max="30" step="0.5" value="${w.temp}" aria-label="Temperatur"> °C
        <button class="small" data-action="win-del" aria-label="Zeitfenster löschen">✕</button>
      </div>`).join("")}
      <div class="day-tools">
        <button class="small" data-action="win-add" ${ws.length >= 10 ? "disabled" : ""}>+ Zeitfenster</button>
        <span>kopieren nach</span>
        <button class="small" data-action="copy-day" data-to="1,2,3,4,5">Mo–Fr</button>
        <button class="small" data-action="copy-day" data-to="6,7">Sa–So</button>
        <button class="small" data-action="copy-day" data-to="1,2,3,4,5,6,7">alle Tage</button>
      </div>
    </section>`;
  }).join("");
  setHTML(el, `<article class="card">
      <div class="editor-head"><input id="plan-name" placeholder="Name, z. B. Wohnzimmer werktags" value="${esc(editing.name)}"></div>
      <p class="hint">Zwischen den Zeitfenstern hält die Heizung ihre Frostschutztemperatur (meist 5 °C). Für eine Absenkung,
      z. B. nachts 17 °C, ein eigenes Zeitfenster anlegen oder die Lücken füllen lassen. Raster: Viertelstunden, Tagesende 23:59,
      bis zu 10 Zeitfenster pro Tag.</p>
      <div class="fill">Lücken füllen mit <input type="number" id="fill-temp" min="5" max="30" step="0.5" value="17"> °C
        <button class="small" data-action="fill-gaps">an allen Tagen</button></div>
      <div class="day-head scale-row"><span class="muted">Uhrzeit</span>${hourScale()}</div>
      ${days}
      <div class="actions"><button class="primary" data-action="plan-save">Speichern</button>
        <button data-action="plan-cancel">Abbrechen</button></div>
    </article>`);
}

function addWindow(d) {
  const ws = editing.days[d];
  const last = ws[ws.length - 1];
  if (last && last.end === "23:59") { toast("Dieser Tag ist schon bis 23:59 belegt.", true); return; }
  const s = last ? Math.ceil(minutes(last.end) / 15) * 15 : 6 * 60;
  const e = Math.min(s + 120, 23 * 60 + 45);
  ws.push({ start: hhmm(s), end: e > s ? hhmm(e) : "23:59", temp: 20 });
  renderEditor();
}

function copyDay(from, targets) {
  for (const d of targets) if (d !== from) editing.days[d] = clone(editing.days[from]);
  renderEditor();
  toast(`${DAY_LONG[Number(from) - 1]} kopiert`);
}

function fillGaps(temp) {
  if (!(temp >= 5 && temp <= 30)) { toast("Temperatur zwischen 5 und 30 °C angeben.", true); return; }
  const tooMany = [];
  for (let i = 1; i <= 7; i++) {
    const d = String(i);
    const ws = editing.days[d].slice().sort((a, b) => minutes(a.start) - minutes(b.start));
    const out = [];
    let cursor = 0;
    for (const w of ws) {
      if (minutes(w.start) > cursor) out.push({ start: hhmm(cursor), end: w.start, temp });
      out.push(w);
      cursor = Math.max(cursor, w.end === "23:59" ? 1440 : minutes(w.end));
    }
    if (cursor < 1440) out.push({ start: hhmm(cursor), end: "23:59", temp });
    if (out.length > 10) tooMany.push(DAY_LONG[i - 1]); else editing.days[d] = out;
  }
  renderEditor();
  if (tooMany.length) toast(`Mehr als 10 Zeitfenster nötig an: ${tooMany.join(", ")} – dort bitte von Hand anpassen.`, true);
}

async function savePlan() {
  editing.name = document.getElementById("plan-name").value.trim();
  const body = { name: editing.name, days: editing.days };
  try {
    if (editing.id) await api("PUT", `api/plans/${encodeURIComponent(editing.id)}`, body);
    else await api("POST", "api/plans", body);
    const inUse = editing.id && (state.plans.find(p => p.id === editing.id) || { rooms: [] }).rooms.length;
    editing = null;
    toast(inUse ? "Gespeichert. Zum Übertragen unter „Räume“ beim Raum „Senden“ tippen." : "Gespeichert.");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}

async function deletePlan(p) {
  if (!confirm(`Heizplan „${p.name}“ löschen?` + (p.rooms.length ?
      "\nEr wird auch aus den Räumen entfernt. Auf den Heizungen bleibt er gespeichert, bis ein anderer Plan gesendet wird." : ""))) return;
  try { await api("DELETE", `api/plans/${encodeURIComponent(p.id)}`); toast("Heizplan gelöscht."); }
  catch (e) { toast(e.message, true); }
  await refresh();
}

// ------------------------------------------------------------------ tab: devices
function clockText(h) {
  if (h.clock_drift == null) return "–";
  const a = Math.abs(h.clock_drift);
  if (a <= 5) return "stimmt";
  const amount = a < 120 ? `${a} s` : a < 7200 ? `${Math.round(a / 60)} min` : a < 172800 ? `${Math.round(a / 3600)} h`
                                                                                   : `${Math.round(a / 86400)} Tage`;
  return `geht ${amount} ${h.clock_drift > 0 ? "vor" : "nach"}`;
}

function deviceFormHTML(f) {
  const saving = busy.has("devform");
  const tokenHint = f.mode === "edit" && f.hasToken ? "hinterlegt – leer lassen = unverändert"
                                                     : "24 Zeichen (Feld „pop“) oder ganzen QR-Inhalt einfügen";
  return `<div class="device-form">
    ${f.mode === "new" ? "<h3>Neue Heizung</h3>" : ""}
    <label>Name<input data-field="name" value="${esc(f.name)}" placeholder="${esc(f.placeholderName || "z. B. Salon Fenster")}"
      autocomplete="off"></label>
    <label>IP-Adresse<input data-field="host" value="${esc(f.host)}" placeholder="z. B. 192.168.5.81" autocomplete="off"
      autocapitalize="off" spellcheck="false"></label>
    <label>Token<input data-field="token" value="${esc(f.token)}" placeholder="${esc(tokenHint)}" autocomplete="off"
      autocapitalize="off" spellcheck="false"></label>
    ${f.mode === "edit" && f.hasToken ? `<label class="check"><input type="checkbox" data-field="removeToken"
      ${f.removeToken ? "checked" : ""}> Token entfernen – Heizung danach nur anzeigen</label>` : ""}
    ${f.mode === "new" ? tokenGuideHTML() : `<p class="hint">Den Token zeigt die Heizung als QR-Code unter Einstellungen ›
      Verbindungen › Verbinden. Ohne Token wird die Heizung nur angezeigt. Beim Speichern prüft die App Adresse und Token
      direkt an der Heizung.</p>`}
    <div class="row">
      <button class="primary" data-action="dev-save" ${saving ? "disabled" : ""}>${saving ? "prüfe …" : f.mode === "new" ? "Hinzufügen" : "Speichern"}</button>
      <button data-action="dev-cancel">Abbrechen</button>
      ${f.mode === "edit" ? '<button class="danger" data-action="dev-delete">Heizung entfernen</button>' : ""}
    </div>
  </div>`;
}

function tokenGuideHTML() {
  return `<div class="guide">
    <strong>So kommst du an den Token</strong>
    <ol>
      <li>An der Heizung <b>Einstellungen › Verbindungen › Verbinden</b> wählen. Steht dort nur <b>Trennen</b>
        (die Heizung ist schon mit SmartHeat4U verbunden): erst <b>Trennen</b>, dann <b>Verbinden</b>.</li>
      <li>Den QR-Code auf dem Display mit der Handykamera scannen und den Text oben ins Feld „Token“ einfügen –
        oder abfotografieren und die 24 Zeichen hinter „pop“ abtippen.</li>
      <li>Nutzt du SmartHeat4U: die Heizung dort mit <b>demselben QR-Code</b> neu koppeln, damit beide Apps
        funktionieren.</li>
    </ol>
    <p class="hint">Ohne Token wird die Heizung nur angezeigt. Beim Speichern prüft die App Adresse und Token direkt an
      der Heizung; ist sie gerade nicht erreichbar, speichert die App trotzdem.</p>
  </div>`;
}

// ------------------------------------------------------------------ network search ("Im Netz suchen")
function scanPanelHTML() {
  const s = scan;
  if (s.running) return `<div class="scan"><p class="scan-wait">Suche im Heimnetz … das dauert ein paar Sekunden.</p></div>`;
  const known = Object.fromEntries(state.heaters.filter(h => h.serial).map(h => [String(h.serial), h]));
  const other = `<div class="row scan-other"><span class="muted">Anderes Netz:</span>
      <input data-field="scan-network" value="${esc(scanNet)}" placeholder="z. B. 192.168.178" autocomplete="off"
        autocapitalize="off" spellcheck="false" inputmode="decimal">
      <button class="small" data-action="scan-other">Durchsuchen</button></div>`;
  if (s.error) {
    return `<div class="scan"><div class="scan-head"><strong>Suche im Netz</strong>
      <button class="small" data-action="scan-close">Schließen</button></div>
      <p class="error-text">${esc(s.error)}</p>${other}</div>`;
  }
  const d = s.data;
  const items = d.heaters.map(f => {
    const h = known[String(f.serial)];
    const where = [f.address, f.room_temp != null ? deg(f.room_temp) : null].filter(Boolean).join(" · ");
    const status = h ? `<span class="tag">in der App: ${esc(h.name)}</span>`
      : `<button class="small primary" data-action="scan-add" data-address="${esc(f.address)}"
           data-hostname="${esc(f.hostname || "")}">Hinzufügen</button>`;
    return `<li><span class="dot ${h ? "ok" : "on"}"></span><span class="hname"><b>${esc(f.hostname || f.serial)}</b>
      <span class="muted">${esc(where)}</span></span>${status}</li>`;
  }).join("");
  const moved = d.moved.map(m => `<p class="hint">Neue Adresse übernommen: ${esc(m.name)} ${esc(m.old)} → ${esc(m.new)}</p>`).join("");
  const newCount = d.heaters.filter(f => !known[String(f.serial)]).length;
  const summary = !d.heaters.length ? "keine Heizung gefunden"
    : `${d.heaters.length} Heizung${d.heaters.length === 1 ? "" : "en"} gefunden${newCount ? `, davon ${newCount} neu` : ""}`;
  return `<div class="scan">
    <div class="scan-head"><strong>Suche im Netz</strong>
      <span class="muted">${esc(d.networks.join(", "))} · ${String(d.seconds).replace(".", ",")} s · ${summary}</span>
      <button class="small" data-action="scan-run">Erneut suchen</button>
      <button class="small" data-action="scan-close">Schließen</button></div>
    ${items ? `<ul class="heater-list scan-list">${items}</ul>` : `<p class="hint">Sind die Heizungen eingeschaltet und im
      WLAN? Ausgeschaltete Heizungen antworten nicht. Läuft die App in Docker oder in einem anderen Netz, hier das Heimnetz
      angeben.</p>`}
    ${moved}${other}
  </div>`;
}

async function runScan(network) {
  scan = { running: true };
  invalidate();
  render();
  try {
    scan = { data: await api("POST", "api/discover", network ? { network } : {}) };
    if (scan.data.moved.length) toast(`Neue Adresse übernommen: ${scan.data.moved.map(m => m.name).join(", ")}`);
  } catch (e) {
    scan = { error: e.message };
  }
  invalidate();
  await refresh();
}

function deviceFilterBar() {
  const counts = { all: state.heaters.length, none: state.heaters.filter(h => !h.room).length };
  for (const r of state.rooms) counts[r.id] = r.heaters.length;
  const chip = (value, label) => `<button class="chip ${deviceFilter === value ? "active" : ""}" data-action="dev-filter"
    data-filter="${esc(value)}" aria-pressed="${deviceFilter === value}">${esc(label)}<span class="count">${counts[value] ?? 0}</span></button>`;
  return `<div class="filter" role="group" aria-label="Heizungen nach Raum filtern">${chip("all", "Alle")}
    ${state.rooms.map(r => chip(r.id, r.name)).join("")}${chip("none", "Nicht zugeordnet")}</div>`;
}

function renderDevices(el) {
  el.className = "tab single";
  if (deviceFilter !== "all" && deviceFilter !== "none" && !state.rooms.some(r => r.id === deviceFilter)) {
    deviceFilter = "all";  // the filtered room was deleted
  }
  const roomOptions = sel => ['<option value="">– kein Raum –</option>'].concat(state.rooms.map(r =>
    `<option value="${esc(r.id)}" ${r.id === sel ? "selected" : ""}>${esc(r.name)}</option>`)).join("");
  const visible = state.heaters.filter(h => deviceFilter === "all" || (deviceFilter === "none" ? !h.room : h.room === deviceFilter));
  const empty = !visible.length && state.heaters.length ? `<p class="empty">${deviceFilter === "none"
    ? "Alle Heizungen sind einem Raum zugeordnet." : "Diesem Raum ist keine Heizung zugeordnet."}</p>` : "";
  const heaters = visible.map(h => {
    const editingThis = deviceForm && deviceForm.mode === "edit" && deviceForm.id === h.id;
    const calibThis = !editingThis && calibForm && calibForm.id === h.id;
    return `<div class="device ${busy.has("dev:" + h.id) ? "busy" : ""}" data-heater="${esc(h.id)}">
      <div class="device-head"><span class="dot ${h.online ? "ok" : "offline"}"></span>
        <strong class="dname">${esc(h.name)}</strong>
        ${h.custom_name && h.hostname ? `<span class="muted">${esc(h.hostname)}</span>` : ""}
        ${editingThis || calibThis ? "" : '<button class="small" data-action="dev-edit">Bearbeiten</button>'}</div>
      ${editingThis ? deviceFormHTML(deviceForm) : calibThis ? calibFormHTML(h) : `<dl>
        <dt>Raum</dt><dd><select data-action="heater-room" aria-label="Raum">${roomOptions(h.room)}</select></dd>
        <dt>Adresse</dt><dd>${esc(h.host)}${h.hostname ? " · " + esc(h.hostname) : ""}</dd>
        <dt>Status</dt><dd>${h.online ? "online" : "offline"}${h.error ? ` <span class="error-text">${esc(h.error)}</span>` : ""}</dd>
        <dt>Steuerung</dt><dd>${h.writable ? "möglich (Token hinterlegt)" : "nur lesen – kein Token (unter „Bearbeiten“ eintragen)"}</dd>
        <dt>Uhr</dt><dd>${esc(clockText(h))}${h.device_time && Math.abs(h.clock_drift ?? 0) > 5 ? ` <span class="muted">(Heizung: ${esc(h.device_time)})</span>` : ""}
          ${h.writable && h.online ? '<button class="small" data-action="clock-one">stellen</button>' : ""}</dd>
        <dt>Kalibrierung</dt><dd>${h.temp_corr == null ? "–" : `Korrektur ${fmtDelta(h.temp_corr)}`}
          ${h.writable && h.online ? '<button class="small" data-action="calib-open">Kalibrieren</button>' : ""}</dd>
        <dt>Heizpläne</dt><dd>${h.plan_slot ? `Plan ${h.plan_slot} ausgewählt${h.mode === "auto" ? ", Automatik läuft" : ""}` : "–"}
          <button class="small" data-action="import-plans" ${h.online ? "" : "disabled"}>${busy.has("dev:" + h.id)
            ? "lese aus …" : "Heizplan aus Heizung auslesen"}</button></dd>
        <dt>Seriennr.</dt><dd>${esc(h.serial ?? "–")}</dd>
        <dt>Firmware</dt><dd>${esc(h.firmware ?? "–")}</dd>
      </dl>`}</div>`;
  }).join("");
  setHTML(el, `<article class="card"><div class="card-head"><h2>Heizungen</h2>
      <button class="small" data-action="clock-all">Alle Uhren stellen</button>
      <button class="small" data-action="scan-run" ${scan && scan.running ? "disabled" : ""}>Im Netz suchen</button>
      <button class="small primary" data-action="dev-new" ${deviceForm && deviceForm.mode === "new" ? "disabled" : ""}>+ Heizung</button></div>
      <p class="hint">Die App stellt die Uhren selbst: beim Start, bei mehr als 1 Minute Abweichung und täglich um 3 Uhr
      (deckt auch Sommer-/Winterzeit ab). Ist eine Heizung länger nicht erreichbar, sucht sie sie im Netz und übernimmt
      eine neue Adresse selbst. Räume legst du im Tab „Räume“ über „+ Raum“ an.</p>
      ${scan ? scanPanelHTML() : ""}
      ${state.heaters.length ? deviceFilterBar() : ""}
      ${deviceForm && deviceForm.mode === "new" ? `<div class="device">${deviceFormHTML(deviceForm)}</div>` : ""}
      ${heaters}${empty}
    </article>`);
}

function openDeviceForm(heaterId, found = null) {  // found: a heater from the network search {address, hostname}
  calibForm = null;
  if (!heaterId) {
    deviceForm = { mode: "new", name: "", host: found ? found.address : "", token: "",
                   placeholderName: found && found.hostname ? found.hostname : "" };
  } else {
    const h = state.heaters.find(x => x.id === heaterId);
    if (!h) return;
    deviceForm = { mode: "edit", id: h.id, name: h.custom_name, placeholderName: h.hostname || h.host, host: h.host,
                   token: "", hasToken: h.writable, removeToken: false };
  }
  render();
  const first = document.querySelector('.device-form input[data-field="' + (heaterId ? "host" : found ? "token" : "name") + '"]');
  if (first) { first.focus(); first.scrollIntoView({ block: "center", behavior: "smooth" }); }
}

async function saveDevice() {
  const f = deviceForm;
  if (!f || busy.has("devform")) return;
  const body = { name: f.name, host: f.host };
  if (String(f.token || "").trim()) body.token = String(f.token).trim();
  if (f.removeToken) body.remove_token = true;
  busy.add("devform");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  try {
    const res = f.mode === "new" ? await api("POST", "api/heaters", body)
                                 : await api("PATCH", `api/heaters/${encodeURIComponent(f.id)}`, body);
    deviceForm = null;
    toast(res.message || "Gespeichert.", Boolean(res.warning));
  } catch (e) {
    toast(e.message, true);
    invalidate();
  } finally {
    busy.delete("devform");
  }
  await refresh();
}

function fmtDelta(k) { return (k > 0 ? "+" : k < 0 ? "−" : "±") + Math.abs(k).toFixed(1).replace(".", ",") + " °C"; }

function calibBasis(h) {  // what the app compares the measurement with: 30-min average, else the current value
  return h.room_temp_avg != null ? { temp: h.room_temp_avg, text: `Ø der letzten ${h.room_temp_avg_minutes} min` }
                                 : { temp: h.room_temp, text: "aktueller Wert" };
}

function calibPreview(h, measuredText) {
  const measured = parseFloat(String(measuredText || "").replace(",", "."));
  if (!(measured >= 0 && measured <= 40) || h.room_temp == null || h.temp_corr == null) {
    return "Gemessenen Wert eingeben – die neue Korrektur erscheint hier.";
  }
  const basis = calibBasis(h);
  const next = Math.round((h.temp_corr + measured - basis.temp) * 10) / 10;
  if (Math.abs(next) > 9.9) return `Nötig wären ${fmtDelta(next)} – mehr als die möglichen ±9,9 °C. Bitte Messung prüfen.`;
  return `Neue Korrektur: <b>${fmtDelta(next)}</b> (bisher ${fmtDelta(h.temp_corr)}) – die Heizung zeigt danach etwa ${deg(measured)}.`;
}

function calibFormHTML(h) {
  const basis = calibBasis(h);
  const saving = busy.has("calib");
  return `<div class="device-form calib-form">
    <h3>Kalibrieren</h3>
    <p>Die Heizung misst gerade <b>${deg(h.room_temp)}</b>${h.room_temp_avg != null
      ? `, im Mittel der letzten ${h.room_temp_avg_minutes} min <b>${deg(h.room_temp_avg)}</b>` : ""}.
      Aktuelle Korrektur: ${fmtDelta(h.temp_corr ?? 0)}.</p>
    <label>Gemessene Raumtemperatur in °C<input data-field="measured" type="number" step="0.1" min="0" max="40"
      inputmode="decimal" value="${esc(calibForm.measured)}" placeholder="z. B. 21,0"></label>
    <p class="calib-preview">${calibPreview(h, calibForm.measured)}</p>
    <p class="hint">Verglichen wird mit dem ${esc(basis.text)} der Heizung. So misst du richtig: Thermometer in der
      Raummitte auf 1–1,3 m Höhe, nicht am Fenster oder in der Sonne; der Raum hat seit 1–2 Stunden seine
      Solltemperatur, Türen und Fenster sind zu. Über 30–60 Minuten mehrmals ablesen und den Durchschnitt eintragen.</p>
    <div class="row">
      <button class="primary" data-action="calib-save" ${saving ? "disabled" : ""}>${saving ? "übernehme …" : "Übernehmen"}</button>
      <button data-action="calib-cancel">Abbrechen</button>
      ${h.temp_corr ? '<button data-action="calib-reset">Korrektur auf 0 setzen</button>' : ""}
    </div>
  </div>`;
}

function openCalib(heaterId) {
  calibForm = { id: heaterId, measured: "" };
  deviceForm = null;
  render();
  const input = document.querySelector('.calib-form input[data-field="measured"]');
  if (input) input.focus();
}

async function saveCalib(body) {
  const f = calibForm;
  if (!f || busy.has("calib")) return;
  if (!body) {
    const measured = parseFloat(String(f.measured || "").replace(",", "."));
    if (!(measured >= 0 && measured <= 40)) { toast("Bitte die gemessene Raumtemperatur (0–40 °C) eingeben.", true); return; }
    body = { measured };
  }
  busy.add("calib");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  try {
    toast((await api("POST", `api/heaters/${encodeURIComponent(f.id)}/calibrate`, body)).message);
    calibForm = null;
  } catch (e) {
    toast(e.message, true);
    invalidate();
  } finally {
    busy.delete("calib");
  }
  await refresh();
}

async function deleteDevice() {
  const f = deviceForm;
  const h = f && state.heaters.find(x => x.id === f.id);
  if (!h || !confirm(`Heizung „${h.name}“ aus der App entfernen?\n\nSie wird aus heizungen.conf gelöscht, Name und ` +
                     "Raumzuordnung gehen verloren. An der Heizung selbst ändert sich nichts.")) return;
  try {
    toast((await api("DELETE", `api/heaters/${encodeURIComponent(h.id)}`)).message);
    deviceForm = null;
  } catch (e) {
    toast(e.message, true);
  }
  await refresh();
}

async function patch(url, body, message) {
  try { await api("PATCH", url, body); toast(message); } catch (e) { toast(e.message, true); invalidate(); }
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  await refresh();
}

function openRoomForm(roomId) {
  if (!roomId) {
    roomForm = { mode: "new", name: "", heaters: [] };
  } else {
    const r = state.rooms.find(x => x.id === roomId);
    if (!r) return;
    roomForm = { mode: "edit", id: r.id, name: r.name, heaters: r.heaters.slice() };
  }
  render();
  const input = document.querySelector('.room-form input[data-field="name"]');
  if (input) input.focus();
}

async function saveRoom() {
  const f = roomForm;
  if (!f || busy.has("roomform")) return;
  const name = String(f.name || "").trim();
  if (!name) { toast("Bitte einen Namen für den Raum angeben.", true); return; }
  busy.add("roomform");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  try {
    if (f.mode === "new") await api("POST", "api/rooms", { name, heaters: f.heaters });
    else await api("PATCH", `api/rooms/${encodeURIComponent(f.id)}`, { name, heaters: f.heaters });
    toast(f.mode === "new" ? `Raum „${name}“ angelegt.` : "Raum gespeichert.");
    roomForm = null;
  } catch (e) {
    toast(e.message, true);
    invalidate();
  } finally {
    busy.delete("roomform");
  }
  await refresh();
}

async function deleteRoom(id) {
  const r = state.rooms.find(x => x.id === id);
  if (!r || !confirm(`Raum „${r.name}“ löschen?\n\nDie Heizungen bleiben erhalten und sind danach keinem Raum zugeordnet. ` +
                     "Ein bereits gesendeter Heizplan bleibt auf den Heizungen gespeichert.")) return;
  try {
    await api("DELETE", `api/rooms/${encodeURIComponent(id)}`);
    toast(`Raum „${r.name}“ gelöscht.`);
    roomForm = null;
  } catch (e) {
    toast(e.message, true);
  }
  await refresh();
}

async function importPlans(heaterId) {
  const key = "dev:" + heaterId;
  busy.add(key);
  render();
  let result = null;
  try {
    result = await api("POST", `api/heaters/${encodeURIComponent(heaterId)}/import-plans`);
    toast(result.message || "Fertig.");
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  if (result && result.created.length) {  // show the new plans right away
    tab = "plans";
    localStorage.setItem("heizung.tab", tab);
    editing = null;
  }
  await refresh();
}

async function syncClock(heaterId) {
  try { report("Uhr", await api("POST", heaterId ? `api/heaters/${encodeURIComponent(heaterId)}/clock` : "api/clock-sync")); }
  catch (e) { toast(e.message, true); }
  await refresh();
}

// ------------------------------------------------------------------ events
function planOf(t) { return state.plans.find(p => p.id === t.closest("[data-plan]").dataset.plan); }
function dayOf(t) { return t.closest(".day").dataset.day; }

document.addEventListener("click", e => {
  if (e.target.id === "dialog") return closeDialog(null);   // tap next to the dialog = cancel
  const t = e.target.closest("button");
  if (!t || t.disabled) return;
  if (t.dataset.dialogButton !== undefined) return dialogButton(Number(t.dataset.dialogButton));
  const tile = t.closest("article.tile");
  switch (t.dataset.action) {
    case "open-setup": return openSetup();
    case "close-setup": return closeSetup(false);
    case "pin-settings": return pinSettings();
    case "home-sp": return changeSetpoint(tile, parseFloat(t.dataset.delta));
    case "home-on": return homeMode(groupFor(tile), "on", "eingeschaltet");
    case "home-off": return homeOff(groupFor(tile));
    case "home-plan": return homeMode(groupFor(tile), "auto", "läuft wieder nach Plan");
    case "home-boost-off": return homeMode(groupFor(tile), "boost_off", "volle Kraft beendet");
  }
  if (t.dataset.tab) {
    tab = t.dataset.tab;
    localStorage.setItem("heizung.tab", tab);
    if (tab === "plans" && editing) renderEditor();
    render();
    return;
  }
  const card = t.closest("article.card");
  switch (t.dataset.action) {
    case "sp": return changeSetpoint(card, parseFloat(t.dataset.delta));
    case "mode": return setMode(card, t.dataset.mode);
    case "send-plan": return sendPlan(card);
    case "plan-new": return startEdit(null);
    case "plan-edit": return startEdit(planOf(t));
    case "plan-copy": { const p = planOf(t); return startEdit({ id: null, name: `${p.name} (Kopie)`, days: p.days }); }
    case "plan-delete": return deletePlan(planOf(t));
    case "plan-save": return savePlan();
    case "plan-cancel": editing = null; return render();
    case "win-add": return addWindow(dayOf(t));
    case "win-del": editing.days[dayOf(t)].splice(Number(t.closest(".win").dataset.idx), 1); return renderEditor();
    case "copy-day": return copyDay(dayOf(t), t.dataset.to.split(","));
    case "fill-gaps": return fillGaps(parseFloat(String(document.getElementById("fill-temp").value).replace(",", ".")));
    case "room-new": return openRoomForm(null);
    case "room-edit": return openRoomForm(card.dataset.id);
    case "room-cancel": roomForm = null; return render();
    case "room-save": return saveRoom();
    case "room-delete": return deleteRoom(roomForm && roomForm.id);
    case "clock-one": return syncClock(t.closest(".device").dataset.heater);
    case "clock-all": return syncClock(null);
    case "import-plans": return importPlans(t.closest(".device").dataset.heater);
    case "dev-filter":
      deviceFilter = t.dataset.filter;
      localStorage.setItem("heizung.deviceFilter", deviceFilter);
      return render();
    case "dev-new": return openDeviceForm(null);
    case "scan-run": return runScan(null);
    case "scan-other": return runScan(scanNet.trim() || null);
    case "scan-close": scan = null; return render();
    case "scan-add": return openDeviceForm(null, { address: t.dataset.address, hostname: t.dataset.hostname });
    case "dev-edit": return openDeviceForm(t.closest(".device").dataset.heater);
    case "dev-cancel": deviceForm = null; return render();
    case "dev-save": return saveDevice();
    case "dev-delete": return deleteDevice();
    case "calib-open": return openCalib(t.closest(".device").dataset.heater);
    case "calib-cancel": calibForm = null; return render();
    case "calib-save": return saveCalib(null);
    case "calib-reset": return saveCalib({ correction: 0 });
  }
});

document.addEventListener("change", e => {
  const t = e.target;
  if (editing && t.closest(".win")) {
    const w = editing.days[dayOf(t)][Number(t.closest(".win").dataset.idx)];
    w[t.dataset.field] = t.dataset.field === "temp" ? parseFloat(String(t.value).replace(",", ".")) : t.value;
    return renderEditor();
  }
  if (deviceForm && t.closest(".device-form")) return updateDeviceForm(t);
  if (roomForm && t.closest(".room-form")) return updateRoomForm(t);
  switch (t.dataset.action) {
    case "room-plan": t.blur(); return assignPlan(t.closest("article.card"), t.value);
    case "heater-room":
      return patch(`api/heaters/${encodeURIComponent(t.closest(".device").dataset.heater)}`, { room: t.value || null }, "Raum zugeordnet.");
  }
});

function updateDeviceForm(t) {
  const field = t.dataset.field;
  if (field) deviceForm[field] = t.type === "checkbox" ? t.checked : t.value;
}

function updateRoomForm(t) {
  if (t.dataset.field === "name") roomForm.name = t.value;
  if (t.dataset.heater) {
    const id = t.dataset.heater;
    roomForm.heaters = roomForm.heaters.filter(x => x !== id).concat(t.checked ? [id] : []);
  }
}

document.addEventListener("input", e => {
  if (editing && e.target.id === "plan-name") editing.name = e.target.value;
  if (calibForm && e.target.closest(".calib-form")) {
    calibForm.measured = e.target.value;
    const h = state && state.heaters.find(x => x.id === calibForm.id);
    const preview = document.querySelector(".calib-preview");
    if (h && preview) preview.innerHTML = calibPreview(h, calibForm.measured);  // live, without re-rendering the form
    return;
  }
  if (e.target.dataset.field === "scan-network") scanNet = e.target.value;
  if (deviceForm && e.target.closest(".device-form")) updateDeviceForm(e.target);
  if (roomForm && e.target.closest(".room-form")) updateRoomForm(e.target);
});
document.addEventListener("keydown", e => {
  if (dialog) {
    if (e.key === "Escape") return closeDialog(null);
    if (e.key === "Enter" && e.target.matches("#dialog input")) {
      e.preventDefault();
      const i = dialog.buttons.findIndex(b => b.value === "submit");
      if (i >= 0) dialogButton(i);
    }
    return;
  }
  if (e.key === "Enter" && calibForm && e.target.closest(".calib-form")) return saveCalib(null);
  if (e.key === "Enter" && e.target.dataset.field === "scan-network") { e.target.blur(); return runScan(scanNet.trim() || null); }
  if (e.key === "Enter" && deviceForm && e.target.closest(".device-form")) saveDevice();
  if (e.key === "Enter" && roomForm && e.target.closest(".room-form") && e.target.dataset.field === "name") saveRoom();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
document.addEventListener("pointerdown", touchSetup, true);
document.addEventListener("keydown", touchSetup, true);

(async () => {
  const wanted = view;   // after a reload: back into the setup only if its session is still open
  showView("home");
  await refresh();
  if (wanted === "setup" && state && state.setup.unlocked) { showView("setup"); render(); }
})();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
