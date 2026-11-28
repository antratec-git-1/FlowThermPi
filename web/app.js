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
let connTab = localStorage.getItem("heizung.connTab") || "wifi";   // sub-tab of "Verbindungen": "wifi" | "zigbee" | "remote"
if (["wifi", "remote", "zigbee"].includes(tab)) {   // until 0.4.14 each had a tab of its own
  connTab = tab;
  tab = "connections";
  localStorage.setItem("heizung.tab", tab);
  localStorage.setItem("heizung.connTab", connTab);
}
let shownTab = null;     // tab last brought into view in the tab bar
let editing = null;      // plan in the editor: {id|null, name, days}
let roomForm = null;     // room form in "Räume": {mode: "new"|"edit", id, name, heaters: [ids]}
let roomView = roomFromHash();   // "Räume": the room on its own page – a room id, "none" (the devices without a room) or
                                 // null (the list); in the address as #raum=<id>, so the back button and a reload keep it
let roomSections = (() => {      // the parts of the room view that are closed: {heat|sensors|plugs|light: false}
  try { const v = JSON.parse(localStorage.getItem("heizung.roomSections") || "{}"); return v && typeof v === "object" ? v : {}; }
  catch (e) { return {}; }
})();
let deviceForm = null;   // heater form in "Geräte": {mode: "new"|"edit", id, name, host, token, hasToken, removeToken}
let deviceFilter = localStorage.getItem("heizung.deviceFilter") || "all";  // "all" | "none" | room id
let calibForm = null;    // calibration in "Geräte": {id, measured}
let scan = null;         // network search in "Geräte": {running} | {data} | {error}
let scanNet = "";        // network typed for "Anderes Netz"
let sensorSearch = null; // „Fühler suchen“ in "Geräte": {running, waiting, data: {networks, seconds, pills}, error}
let plugSearch = null;   // „Steckdosen suchen“ in "Geräte": {running, data: {networks, seconds, plugs}, error}
let sensorForm = null;   // sensor form in "Geräte": {mode: "new"|"edit", id, pill, channel, name, room, placeholder}
let addFlow = null;      // „+ Gerät hinzufügen“ in "Geräte" or in a room: {step: "what"|"find"|"where", kind, before: Set
                         // of Zigbee addresses before the search, where: {type: "heater"|"plug"|"light", id}, room, name,
                         // heater, home: the room it was opened in (chosen at „Wohin?“; „Fertig“ leads back there)}
let pillForm = null;     // „Einrichten“ of a Pill: {pill, at: "search"|sensor id, mode}
const calibHours = {};   // Kalibrierlauf: sensor id -> duration (hours) chosen before „starten“
let deviceTab = ["sensors", "plugs", "lights"].includes(localStorage.getItem("heizung.deviceTab"))   // sub-tab of "Geräte"
  ? localStorage.getItem("heizung.deviceTab") : "heaters";
const pillRestart = {};  // Pill id -> true while it restarts after „Einrichten“
const UPDATE_MAX_MS = 6 * 60 * 1000;   // stop waiting for an update after 6 minutes
let updateCheck = null;  // "Nach Updates suchen" in "Über": {running} | {result} | {error}
let updateRun = loadUpdateRun();   // update started in this browser: {from: build, at}; survives the reload
let loadedBuild = null;  // version|commit this page came with – another one answering means: reload
const pending = {};      // debounced setpoint changes: cardKey -> {temp, timer}
const busy = new Set();  // cards with a running action
const sentAt = {};       // cardKey -> Date.now() when every heater took the new temperature: „✓ gesendet“ for a moment
const SENT_MS = 2000;

// ------------------------------------------------------------------ helpers
function hhmm(min) { return String(Math.floor(min / 60)).padStart(2, "0") + ":" + String(min % 60).padStart(2, "0"); }
function minutes(t) { const [h, m] = t.split(":").map(Number); return h * 60 + m; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function deg(t) { return t == null || Number.isNaN(t) ? "–" : t.toFixed(1).replace(".", ",") + " °C"; }
function avg(list) { return list.reduce((a, b) => a + b, 0) / list.length; }
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function localTime(iso) {  // "2026-11-21T20:06:59" – the server's local time, as the API sends it – as a Date
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)/.exec(iso || "");
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}
function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–";
}
function tempColor(t) { const x = Math.max(0, Math.min(1, (t - 15) / 10)); return `hsl(${210 - 190 * x}, 72%, ${50 - 8 * x}%)`; }

const OFFLINE = "Keine Verbindung zur Heizungs-App – ist das Handy im Heim-WLAN?";
async function api(method, url, body, retried = false) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(url, opt);
  } catch (_) {   // no answer at all (Safari: "Load failed") – phone in another WLAN, Pi restarting, connection cut
    throw Object.assign(new Error(OFFLINE), { offline: true });
  }
  let data = {};
  try { data = await res.json(); } catch (_) { /* no JSON body */ }
  if (res.status === 403 && data.pin_required && !retried) {   // setup locked (again): ask for the PIN, then retry once
    if (await unlockSetup(true)) return api(method, url, body, true);
    throw new Error("Abgebrochen – ohne PIN lässt sich die Einrichtung nicht ändern.");
  }
  if (!res.ok) throw Object.assign(new Error(data.error || `Fehler ${res.status}`), { status: res.status });
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
    if (watchUpdate()) return;   // a new version answers: the page reloads
    const on = state.heaters.filter(h => h.online).length;
    setStatus(`${on}/${state.heaters.length} Heizung(en) online · ${new Date().toLocaleTimeString("de-DE")}`);
    setBanner(updating() ? "Die Heizungs-App wird gerade aktualisiert und ist gleich kurz nicht erreichbar. " +
      "Die Heizungen heizen weiter." : null, true);
    showAppInfo(state.app);
    searchAgain();
    powerAgain();
    if (view === "setup" && !state.setup.unlocked && !pinPromise) showView("home");  // e.g. the app was restarted
  } catch (e) {
    setStatus("keine Verbindung zur App", true);
    if (updating()) setBanner("Die Heizungs-App startet nach dem Update neu – gleich wieder da. Die Heizungen heizen weiter.", true);
    else setBanner("Keine Verbindung zur Heizungs-App. Es wird gleich noch einmal versucht.");
  }
  render();
}

// ------------------------------------------------------------------ updates (button in "Über")
function buildOf(app) { return app ? `${app.version}|${app.commit || ""}` : ""; }
function loadUpdateRun() {
  try { const r = JSON.parse(localStorage.getItem("heizung.update") || "null"); return r && r.from ? r : null; }
  catch (_) { return null; }
}
function updating() {  // an update is under way – started here or in another browser
  return !!updateRun || !!(state && state.update && ["waiting", "running"].includes(state.update.state));
}

function watchUpdate() {  // after every refresh; true = the page reloads
  const build = buildOf(state.app);
  if (loadedBuild === null) loadedBuild = build;
  else if (build !== loadedBuild) { location.reload(); return true; }   // new version: load its page and scripts
  if (!updateRun) return false;
  const u = state.update;
  if (build !== updateRun.from) endUpdateRun(`Update fertig – jetzt läuft FlowThermPi ${state.app.version}.`);
  else if (u && u.state === "failed") endUpdateRun(`Update fehlgeschlagen: ${u.message || "unbekannter Fehler"}`, true);
  else if (u && u.state === "done") endUpdateRun(u.message || "Update fertig.");
  else if (Date.now() - updateRun.at > UPDATE_MAX_MS)
    endUpdateRun("Das Update dauert ungewöhnlich lange. Meldungen auf dem Pi: journalctl -u flowthermpi-update", true);
  return false;
}

function endUpdateRun(message, error = false) {
  updateRun = null;
  localStorage.removeItem("heizung.update");
  toast(message, error);
}

async function checkUpdates() {
  updateCheck = { running: true };
  render();
  try { updateCheck = { result: await api("POST", "api/update/check") }; }
  catch (e) { updateCheck = { error: e.message }; }
  render();
}

async function startUpdate() {
  const latest = updateCheck && updateCheck.result && updateCheck.result.latest;
  const yes = await ask({
    title: "Jetzt aktualisieren?",
    text: `FlowThermPi holt ${latest ? "Version " + latest.version : "die neueste Version"} von GitHub und startet neu. ` +
      "Das dauert meist ein bis zwei Minuten, die Heizungen heizen in der Zeit ganz normal weiter. " +
      "Danach lädt diese Seite von selbst neu.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Jetzt aktualisieren", value: "yes", cls: "primary" }],
  });
  if (!yes) return;
  try {
    const res = await api("POST", "api/update/start");
    updateRun = { from: buildOf(state.app), at: Date.now() };
    localStorage.setItem("heizung.update", JSON.stringify(updateRun));
    state.update = res.update;
    updateCheck = null;
  } catch (e) { toast(e.message, true); }
  await refresh();
}

function fmtDate(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleDateString("de-DE", { day: "numeric", month: "long", year: "numeric" }) : "";
}

function updateCardHTML(app) {
  const since = fmtDate(app.installed);
  const u = state.update;
  let body;
  if (updating()) {
    body = `<p class="searching"><b>Update läuft …</b></p>
      <p class="muted">Das dauert meist ein bis zwei Minuten. Die App startet dabei neu, danach lädt diese Seite von selbst neu.
        Die Heizungen heizen in der Zeit ganz normal weiter.</p>`;
  } else if (updateCheck && updateCheck.running) {
    body = `<p class="searching">Frage bei GitHub nach …</p>`;
  } else {
    const r = updateCheck && updateCheck.result;
    const l = r && r.latest;
    const failed = u && u.state === "failed" && !r &&
      !(app.installed && Date.parse(u.finished || u.started) < Date.parse(app.installed));  // not if installed since
    body = `${failed ? `<p class="error-text">Das letzte Update hat nicht geklappt: ${esc(u.message || "unbekannter Fehler")}</p>` : ""}
      ${updateCheck && updateCheck.error ? `<p class="error-text">${esc(updateCheck.error)}</p>` : ""}
      ${r && !r.available ? `<p class="update-ok">✓ Das ist die neueste Version.</p>` : ""}
      ${r && r.available ? `<div class="update-new">
        <p><b>Neu: Version ${esc(l.version)}</b>${l.date ? ` vom ${esc(fmtDate(l.date))}` : ""}</p>
        ${l.message ? `<p class="muted">„${esc(l.message)}“</p>` : ""}
        ${app.updates ? `<button class="primary" data-action="update-start">Jetzt aktualisieren</button>`
          : `<p class="muted">Auf diesem Gerät gibt es keinen Update-Knopf. Den gibt es auf dem Raspberry Pi,
              wenn FlowThermPi mit dem Installationsbefehl eingerichtet ist (Anleitung im Quellcode).</p>`}
      </div>` : `<button data-action="update-check">Nach Updates suchen</button>`}`;
  }
  return `<div class="card about updates">
    <h2>Updates</h2>
    <p>Installiert: <b>Version ${esc(app.version)}</b>${since ? ` · seit ${esc(since)}` : ""}</p>
    ${body}
  </div>`;
}

function showAppInfo(app) {  // footer of the setup: name, version, licence, source code
  if (!app) return;
  setHTML(document.getElementById("app-info"),
    `${esc(app.name)} ${esc(app.version)} · Lizenz ${esc(app.license)} · ` +
    `<a href="${esc(app.repo)}" target="_blank" rel="noopener">Quellcode auf GitHub</a>`);
}

function setBanner(text, info = false) {  // info: blue note instead of a red warning
  const el = document.getElementById("home-banner");
  el.textContent = text || "";
  el.className = info ? "banner info" : "banner";
  el.hidden = !text;
}

function showView(name) {
  closeHistory();   // a Verlauf from the setup shows the costs: it does not stay open on the home page
  closeLightSheet();
  view = name;
  if (name === "setup") sessionStorage.setItem("heizung.view", "setup");
  else sessionStorage.removeItem("heizung.view");
  document.body.className = "view-" + name;
  if (name === "setup") openTab();
  touchSetup();
  window.scrollTo(0, 0);
}

function openTab() {   // what the chosen tab needs fresh from the Pi
  if (tab === "connections") openConnTab();
  if (tab === "power") loadPower();
  if ((tab === "devices" || (tab === "rooms" && roomView != null)) && state && state.app.zigbee) {
    loadZigbee();   // the Zigbee devices and whether Zigbee is ready
  }
}

// „Verbindungen“: how the Pi itself is connected – each part only on the Pi, set up with the installer
const CONN_PARTS = [["wifi", "Heimnetz"], ["zigbee", "Zigbee-Funk"], ["remote", "Fernwartung"]];
function connTabs() { return state ? CONN_PARTS.map(([id]) => id).filter(id => state.app[id]) : []; }

function openConnTab() {
  const parts = connTabs();
  if (parts.length && !parts.includes(connTab)) connTab = parts[0];
  if (connTab === "wifi") openWifiTab();
  else if (connTab === "remote") openRemoteTab();
  else if (connTab === "zigbee") openZigbeeTab();
}

function setHTML(el, html) {  // only touch the DOM if something changed (keeps open dropdowns and focus)
  if (el._html === html) return;
  el._html = html;
  el.innerHTML = html;
  linkAddresses(el);
}

// Every IP address on the page is a link: a tap opens the device's own web page (Shelly, Pill, router …) in a new tab.
// Not inside buttons, lists to choose from or form fields – and not a network like „192.168.1.0/24“.
const IP_RE = /(https?:\/\/)?((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(:\d{1,5})?/g;
const NO_LINK = "a, button, select, option, textarea, label, summary, script, style, svg, [contenteditable]";
function ipMatches(text) {   // [start, end, href] of every IP address in a text
  const out = [];
  IP_RE.lastIndex = 0;
  for (let m; (m = IP_RE.exec(text)); ) {
    const before = text[m.index - 1] || "", after = text.slice(IP_RE.lastIndex, IP_RE.lastIndex + 2);
    if (/[\w.:\/-]/.test(before) || /^(\.?\d|\w)/.test(after) || (!m[1] && after[0] === "/")) continue;
    out.push([m.index, IP_RE.lastIndex, `${m[1] || "http://"}${m[2]}${m[3] || ""}/`]);
  }
  return out;
}
function linkAddresses(root) {
  const found = [], walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n; (n = walk.nextNode()); ) {
    if (!/\d\.\d/.test(n.nodeValue) || !n.parentElement || n.parentElement.closest(NO_LINK)) continue;
    const hits = ipMatches(n.nodeValue);
    if (hits.length) found.push([n, hits]);
  }
  for (const [n, hits] of found) {   // after the walk: changing the text nodes during it would confuse the walker
    const text = n.nodeValue, frag = document.createDocumentFragment();
    let at = 0;
    for (const [a, b, href] of hits) {
      const link = document.createElement("a");
      Object.assign(link, { href, target: "_blank", rel: "noopener noreferrer", className: "ip", textContent: text.slice(a, b) });
      frag.append(text.slice(at, a), link);
      at = b;
    }
    frag.append(text.slice(at));
    n.replaceWith(frag);
  }
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
  const parts = connTabs();   // „Verbindungen“ only on the Pi, set up with the installer
  if (state) document.querySelector('#tabs [data-tab="connections"]').hidden = !parts.length;
  if (state && tab === "connections" && !parts.length) tab = "rooms";
  if (parts.length && !parts.includes(connTab)) connTab = parts[0];
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  if (tab !== shownTab) {   // a tab bar that scrolls (phone): bring the chosen tab into view – only when it changes
    shownTab = tab;
    const b = document.querySelector('#tabs button[aria-selected="true"]'), nav = b && b.parentElement;
    if (nav && (b.offsetLeft < nav.scrollLeft || b.offsetLeft + b.offsetWidth > nav.scrollLeft + nav.clientWidth))
      nav.scrollLeft = b.offsetLeft - 8;
  }
  for (const name of ["rooms", "plans", "devices", "power", "connections", "about"]) {
    const sec = document.getElementById("tab-" + name);
    sec.hidden = name !== tab;
    // the room view shows the same device cards as "Geräte": a hidden one is emptied, so a form is only once on the page
    if (sec.hidden && (name === "rooms" || name === "devices") && sec.firstChild) setHTML(sec, "");
  }
  if (!state) return;
  const el = document.getElementById("tab-" + tab);
  const focus = document.activeElement;
  if (lampDrag || (el.contains(focus) && focus.matches("input, select"))) return;  // never disturb typing or a slider
  if (tab === "rooms") renderRooms(el);
  else if (tab === "plans") { if (!editing) renderPlans(el); }       // the editor renders itself
  else if (tab === "about") renderAbout(el);
  else if (tab === "connections") renderConnections(el);
  else if (tab === "power") renderPower(el);
  else renderDevices(el);
}

function renderConnections(el) {   // sub-tabs Heimnetz · Zigbee-Funk · Fernwartung – the ones this Pi has
  const parts = connTabs();
  const label = Object.fromEntries(CONN_PARTS);
  const subtabs = parts.length > 1 ? `<div class="subtabs" role="tablist" aria-label="Verbindung">${parts.map(id =>
    `<button data-conntab="${id}" role="tab" aria-selected="${connTab === id}">${label[id]}</button>`).join("")}</div>` : "";
  setHTML(el, subtabs + (connTab === "wifi" ? wifiHTML() : connTab === "remote" ? remoteHTML() : zigbeeHTML()));
  if (connTab === "wifi" && wifiFocus) {
    wifiFocus = false;
    const input = el.querySelector('.wifi-form input[data-wfield="password"]');
    if (input) input.focus();
  }
}

function renderAbout(el) {  // "Über": logo, what the app does, version, licence, source code
  const app = state.app || {};
  setHTML(el, `<div class="card about">
    <img class="about-logo" src="logo.jpg" width="800" height="706" alt="FlowThermPi-Logo">
    <p class="about-lead"><b>${esc(app.name)}</b> steuert Thermotec-Heizungen (AeroFlow mit FlexiSmart-Pro-Regler)
      direkt im eigenen WLAN – ohne Cloud und ohne Konto beim Hersteller. Alle Daten bleiben im Haus.</p>
    <dl class="about-facts">
      <dt>Version</dt><dd>${esc(app.version)}</dd>
      ${app.device ? `<dt>Gerätenummer</dt><dd>${esc(app.device)}</dd>` : ""}
      <dt>Lizenz</dt><dd>${esc(app.license)} – freie Software, ${esc(app.copyright)}</dd>
      <dt>Quellcode</dt><dd><a href="${esc(app.repo)}" target="_blank" rel="noopener">${esc(String(app.repo).replace(/^https:\/\//, ""))}</a></dd>
    </dl>
    <p class="muted">FlowThermPi ist ein unabhängiges Projekt und kein Produkt der Thermotec AG.</p>
  </div>
  ${updateCardHTML(app)}
  ${logsCardHTML()}`);
}

// ------------------------------------------------------------------ „Protokoll speichern“ (residents' view and Über)
let logsBusy = false;   // the file is being put together
let logsEmail = null;   // the e-mail address being typed in Einrichtung › Über (null: as saved)

function fmtBytes(n) {
  if (n == null) return "–";
  return n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1).replace(".", ",")} MB`;
}
function fmtLocal(iso) {  // "2026-11-01T08:00:01" (the server's local time) -> "01.11.2026, 08:00"
  const d = localTime(iso);
  return d ? d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";
}

function renderLogFoot() {
  setHTML(document.getElementById("log-foot"), `<button class="quiet" data-action="logs-save" ${logsBusy ? "disabled" : ""}>${
    logsBusy ? "Protokoll wird zusammengestellt …" : "Protokoll speichern …"}</button>`);
}

function logsCardHTML() {
  const l = state.logs || {};
  const hours = l.detail_hours || 24;
  const email = logsEmail ?? (l.email || "");
  return `<div class="card about logs-card">
    <h2>Protokoll</h2>
    <p>Die App schreibt mit, was passiert: welche Heizung nicht antwortet, was wann eingestellt wurde, wie WLAN,
      Fernwartung und Zigbee laufen. „Protokoll speichern“ – auch unten in der Heizungsansicht – macht daraus eine
      Datei, die die Bewohner dir schicken können. Passwörter und Tokens stehen nicht darin.</p>
    <p class="muted">Gespeichert: ${esc(fmtBytes(l.size))}${l.oldest ? ` · ältester Eintrag ${esc(fmtLocal(l.oldest))}` : ""}${
      l.system ? " · dazu die Systemprotokolle des Pi" : ""}</p>
    <label class="logs-email">E-Mail-Adresse, an die die Bewohner das Protokoll schicken
      <span><input type="email" data-lfield="email" value="${esc(email)}" placeholder="name@beispiel.de" autocomplete="email"
        maxlength="120"><button data-action="logs-email">Speichern</button></span></label>
    <p class="hint">Steht hier eine Adresse, bietet die Heizungsansicht nach dem Speichern an, eine E-Mail an sie
      vorzubereiten. Die Datei hängen die Bewohner dann selbst an.</p>
    <div class="logs-detail">
      <p><b>Ausführliches Protokoll:</b> ${l.detail_until ? `an bis ${esc(fmtTime(l.detail_until))} Uhr` : "aus"}</p>
      <p class="hint">Schreibt zusätzlich jede Abfrage der Heizungen und Fühler und jeden Aufruf der Seite mit – für
        Fehler, die nur ab und zu auftreten. Endet nach ${hours} Stunden von selbst.</p>
      ${l.detail_until ? `<button data-action="logs-detail" data-on="0">Ausschalten</button>`
        : `<button data-action="logs-detail" data-on="1">Für ${hours} Stunden einschalten</button>`}
    </div>
    <button class="primary" data-action="logs-save" ${logsBusy ? "disabled" : ""}>${logsBusy ? "Wird zusammengestellt …"
      : "Protokoll speichern …"}</button>
  </div>`;
}

async function logsSave() {
  const l = (state && state.logs) || {};
  const intro = "Das Protokoll hilft herauszufinden, was nicht geklappt hat. Passwörter und Tokens stehen nicht darin, " +
    "wohl aber die Namen der Räume und Geräte und Adressen im Heimnetz." +
    (l.system ? " Das Zusammenstellen dauert ein paar Sekunden, manchmal bis zu einer Minute." : "");
  let res = null;
  const go = await ask({
    title: "Protokoll speichern?",
    text: intro,
    buttons: [{ label: "Abbrechen", value: null }, { label: "Zusammenstellen", value: "submit", cls: "primary" }],
    onSubmit: async () => {
      const d = dialog;
      d.text = "Einen Moment bitte – das Protokoll wird zusammengestellt …";
      renderDialog();
      logsBusy = true;
      render();
      try { res = await api("POST", "api/logs/prepare"); return null; } catch (e) { d.text = intro; return e.message; }
      finally { logsBusy = false; render(); }
    },
  });
  if (!go || !res) return;
  const save = await ask({
    title: "Das Protokoll ist fertig",
    text: `Jetzt die Datei „${res.name}“ (${fmtBytes(res.size)}) speichern – meist landet sie im Ordner „Downloads“.` +
      (res.system ? "" : ` Die Systemprotokolle des Pi fehlen darin: ${res.missing}`),
    buttons: [{ label: "Abbrechen", value: null }, { label: "Datei speichern", value: "save", cls: "primary" }],
  });
  if (!save) return;
  const a = document.createElement("a");   // right after the tap: browsers allow the download
  a.href = "api/logs/" + encodeURIComponent(res.id);
  a.download = res.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (!res.mail) {
    await ask({ title: "Protokoll gespeichert", text: "Bitte die Datei an die Person schicken, die bei der Heizung hilft.",
      buttons: [{ label: "OK", value: null, cls: "primary" }] });
    return;
  }
  const mail = await ask({
    title: "E-Mail vorbereiten?",
    text: `Dann öffnet sich eine E-Mail an ${res.mail.to}. Die Datei „${res.name}“ bitte selbst anhängen ` +
      "(Büroklammer, Ordner „Downloads“) und die E-Mail abschicken.",
    buttons: [{ label: "Nein, danke", value: null }, { label: "E-Mail vorbereiten", value: "mail", cls: "primary" }],
  });
  if (mail) location.href = mailtoHref(res.mail);
}

function mailtoHref(mail) {  // opens the e-mail program with address, subject and text („@“ stays readable)
  return `mailto:${encodeURIComponent(mail.to).replace(/%40/g, "@")}?subject=${encodeURIComponent(mail.subject)}` +
    `&body=${encodeURIComponent(mail.body)}`;
}

async function logsSettings(body) {
  try {
    const res = await api("PUT", "api/logs/settings", body);
    state.logs = res.logs;
    if ("email" in body) logsEmail = null;
    if (res.message) toast(res.message);
  } catch (e) { toast(e.message, true); }
  invalidate();
  render();
}
function logsEmailSave() {
  const input = document.querySelector('#tab-about [data-lfield="email"]');
  return logsSettings({ email: (input ? input.value : logsEmail ?? "").trim() });
}

// ------------------------------------------------------------------ simple view for residents
function degShort(t) {  // targets come in 0.5 steps: 21 -> "21 °C", 21.5 -> "21,5 °C"
  if (t == null || Number.isNaN(t)) return "–";
  const r = Math.round(t * 10) / 10;
  return (Number.isInteger(r) ? String(r) : r.toFixed(1).replace(".", ",")) + " °C";
}
function degBig(t) { return t == null || Number.isNaN(t) ? "–" : t.toFixed(1).replace(".", ",") + "°"; }  // the tiles' numbers
function tempWord(t) { return t == null ? "aus" : degShort(t); }   // null = gap in the plan (frost protection)

// A tile takes on the warmth of its room: blue-grey at 12 °C, warm paper at 20 °C, apricot at 25 °C. Straight lines
// between these points, computed here (old iPads know no CSS color-mix). Every text colour on the tiles keeps its
// contrast on all of them (checked for 10 to 27 °C in half degrees; the weakest is the grey text at 25 °C, 5 : 1).
const TINTS = [[12, [0xe3, 0xec, 0xf2]], [17, [0xf1, 0xee, 0xe6]], [20, [0xfd, 0xeb, 0xdd]], [22, [0xfc, 0xe0, 0xcb]],
               [25, [0xf9, 0xd2, 0xba]]];
const TINT_OFFLINE = "#f1eee6";   // no connection: neutral, no warmth to show
function tileTint(t) {
  let i = 1;
  while (i < TINTS.length - 1 && t > TINTS[i][0]) i++;
  const [t0, c0] = TINTS[i - 1], [t1, c1] = TINTS[i];
  const f = Math.max(0, Math.min(1, (t - t0) / (t1 - t0)));
  return "#" + c0.map((v, k) => Math.round(v + (c1[k] - v) * f).toString(16).padStart(2, "0")).join("");
}
function boostLeft(online) {  // share of „volle Kraft“ still to go – only when it fits a start from the app (30 minutes)
  const h = online.find(x => x.mode === "boost" && x.boost_remaining);
  const min = h ? parseInt(String(h.boost_remaining), 10) : NaN;
  return Number.isFinite(min) && min >= 0 && min <= BOOST_MINUTES ? min / BOOST_MINUTES : null;
}
const WAVE = '<svg class="wave" viewBox="0 0 400 16" preserveAspectRatio="none" aria-hidden="true">' +
  '<path d="M0 8Q25 0 50 8T100 8T150 8T200 8T250 8T300 8T350 8T400 8V16H0Z" fill="currentColor"/></svg>';
function uhr(min) { const h = Math.floor(min / 60) % 24, m = min % 60; return m ? `${h}:${String(m).padStart(2, "0")} Uhr` : `${h} Uhr`; }

function serverNow() {  // the server's local time (the heaters' clocks follow it), advanced since the last refresh
  const t = state && localTime(state.now);
  return t ? new Date(t.getTime() + (Date.now() - stateAt)) : new Date();
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

function planLabel(g, target) {
  // under the target in Automatik (Einrichtung › Räume): from the plan – or set by hand until its time window ends
  const plan = planOfGroup(g);
  const at = weekMinute(serverNow());
  const p = plan && planNow(plan, at);
  if (!p || p.constant || target == null) return "Soll laut Heizplan";
  const frost = (g.heaters.find(h => h.online && h.antifrost != null) || {}).antifrost ?? 5;
  const near = (a, b) => a != null && Math.abs(a - b) < 0.25;
  if (near(target, p.temp ?? frost) || (near(target, p.next ?? frost) && p.until - at <= 180)) return "Soll laut Heizplan";
  return `Soll bis ${whenText(at, p.windowEnd)}, dann Heizplan`;
}

function homeStatus(g, online, mode, target, writable) {
  if (!online.length) {
    const thermostats = g.heaters.every(h => h.kind === "zigbee");
    return { text: !thermostats ? "Keine Verbindung zur Heizung." : g.heaters.length > 1 ? "Keine Verbindung zu den Thermostaten."
                                 : "Keine Verbindung zum Thermostat.", tone: "bad" };
  }
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

const SENSOR_FRESH_MS = 15 * 60 * 1000;   // an older reading stays off the home page (the setup shows why)

function homeSensors(g) {  // the room's sensors with a fresh reading, for its home tile
  if (g.kind !== "room") return [];
  const now = serverNow().getTime();
  return (state.sensors || []).filter(s => {
    const at = localTime(s.measured_at);
    return s.room === g.id && s.temp != null && at && now - at.getTime() <= SENSOR_FRESH_MS;
  });
}

function roomTemp(g) {  // the room's temperature: its sensors' if one has a fresh reading, else at its heaters
  const sensors = homeSensors(g);
  const temps = (sensors.length ? sensors.map(s => s.temp) : g.heaters.filter(h => h.online).map(h => h.room_temp))
    .filter(t => t != null);
  return temps.length ? avg(temps) : null;
}

function heaterOff(h) {  // switched off – or held at its frost protection, which counts the same (see groupMode)
  return h.mode === "off" || h.mode === "standby"
    || (h.mode === "manual" && h.antifrost != null && h.setpoint != null && h.setpoint <= h.antifrost);
}

function sensorName(s, list) {  // „Fühler“, „Fühler „Fensterbank““ or „Fühler pill-1“ – as the residents read it
  return s.custom_name ? `Fühler „${esc(s.custom_name)}“` : list.length < 2 ? "Fühler"
    : /Fühler/.test(s.name) ? esc(s.name) : `Fühler ${esc(s.name)}`;   // "pill-1 · Fühler 2" names itself
}

const ICONS = {   // drawn for this app: a radiator (heaters and thermostats), a thermometer (sensors), a plug, a bulb
  heater: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="3.4" y="4.5" width="3.2" height="14" rx="1.6"/>' +
    '<rect x="8.1" y="4.5" width="3.2" height="14" rx="1.6"/><rect x="12.8" y="4.5" width="3.2" height="14" rx="1.6"/>' +
    '<rect x="17.5" y="4.5" width="3.2" height="14" rx="1.6"/><path d="M2 7.5h20M2 15.5h20M5 18.5v2M19.1 18.5v2" ' +
    'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  sensor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">' +
    '<path d="M10.5 14.1V5.5a1.5 1.5 0 0 1 3 0v8.6a3.6 3.6 0 1 1-3 0z"/><path d="M12 15.5V8.5"/>' +
    '<circle cx="12" cy="17.4" r="1.7" fill="currentColor" stroke="none"/></svg>',
  plug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M9 2.8v4.7M15 2.8v4.7"/><path d="M6.5 7.5h11v3.8a5.5 5.5 0 0 1-11 0z"/>' +
    '<path d="M12 16.8v4.5"/></svg>',
  light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M9.2 17.5h5.6M10 20.8h4"/><path d="M12 2.8a6.2 6.2 0 0 0-3.7 11.2c.6.5 ' +
    '.9 1.2.9 1.9v.3h5.6v-.3c0-.7.3-1.4.9-1.9A6.2 6.2 0 0 0 12 2.8z"/></svg>',
  motion: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">' +
    '<circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/><path d="M8.2 8.2a5.4 5.4 0 0 0 0 7.6' +
    'M15.8 8.2a5.4 5.4 0 0 1 0 7.6"/><path d="M5.2 5.2a9.6 9.6 0 0 0 0 13.6M18.8 5.2a9.6 9.6 0 0 1 0 13.6"/></svg>',
};
const CHEVRON = '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9.5l6 6 6-6" fill="none" ' +
  'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function deviceIcon(h, label = false) {
  // a radiator for heaters and thermostats – warm while it heats, red without connection – or a thermometer for sensors
  const cls = !h ? "sensor" : !h.online ? "offline" : h.heating ? "on" : "";
  const name = !h ? "Fühler" : h.kind === "zigbee" ? "Thermostat" : "Heizung";
  return `<span class="dico ${cls}" ${label ? `role="img" aria-label="${name}"` : 'aria-hidden="true"'}>${
    ICONS[h ? "heater" : "sensor"]}</span>`;
}

function deviationHTML(h, sensors) {
  // how far a heater shows from the room's sensor: heater minus sensor, read at the same moment – the „Abweichung“ of
  // the „Kalibrierung“ box (several sensors: their mean); yellow beyond the tolerance (±0,5 °C)
  const found = !h.online ? [] : sensors.flatMap(s => (s.compare || [])
    .filter(c => c.heater === h.id && c.deviation != null).map(c => ({ d: c.deviation, tol: s.tolerance ?? 0.5 })));
  if (!found.length) return "";
  const d = Math.round(avg(found.map(f => f.d)) * 10) / 10;
  const ok = Math.abs(d) <= Math.min(...found.map(f => f.tol)) + 1e-9;
  return `<span class="ddev${ok ? "" : " warn"}">${fmtDelta(d)} ${found.length > 1 ? "zu den Fühlern" : "zum Fühler"}</span>`;
}

function tileDevicesHTML(g, sensors) {
  // „Ist“ and „Soll“ of every device of the room, when it has more than one: heaters and thermostats (with how far they
  // show from the room's sensor), then the sensors with a fresh reading (and their humidity)
  const rows = g.heaters.map(h => ({ icon: deviceIcon(h, true), name: esc(h.name), dev: deviationHTML(h, sensors),
      ist: h.online ? degBig(h.room_temp) : null, soll: !h.online ? null : heaterOff(h) ? "aus" : degBig(h.setpoint) }))
    .concat(sensors.map(s => ({ icon: deviceIcon(null, true), name: sensorName(s, sensors) + (s.humidity == null ? ""
      : `<small>${Math.round(s.humidity)} % Luftfeuchte</small>`), dev: "", ist: degBig(s.temp), soll: "–" })));
  if (rows.length < 2) return "";
  return `<div class="tile-devices"><span></span><span></span><span class="th">Ist</span><span class="th">Soll</span>${
    rows.map(r => `${r.icon}<span class="dname">${r.name}</span>${r.ist == null ? '<span class="dnone">keine Verbindung</span>'
      : `<span class="dval">${esc(r.ist)}</span><span class="dval">${esc(r.soll)}</span>`}${r.dev}`).join("")}</div>`;
}

function roomPlugs(g) {   // the plugs of a tile: the room's own and its heaters' – a heater without room: its plug
  const plugs = state.plugs || [];
  return g.kind === "room" ? plugs.filter(p => p.room === g.id) : plugs.filter(p => g.heaters.some(h => h.id === p.heater));
}

function tilePlugsHTML(g) {
  // the plugs of the room: on or off – with „Strom auf der Startseite“ (Strom › Strom und Kosten) also what they draw
  // now and today, and since when a washing machine or a dryer runs
  const plugs = roomPlugs(g);
  if (!plugs.length) return "";
  const numbers = !!(state.costs && state.costs.show_power);
  return `<ul class="tile-plugs">${plugs.map(p => {
    const kind = applianceText(p.appliance), lost = !p.found || !!p.error;
    const sub = p.appliance !== "sonstiges" && kind !== p.name ? kind : "";
    const status = lost ? '<span class="pstate off">keine Verbindung</span>'
      : numbers && p.run ? '<span class="pstate run">läuft</span>'
      : p.state === "ON" ? '<span class="pstate">an</span>' : p.state === "OFF" ? '<span class="pstate off">aus</span>'
      : '<span class="pstate off">–</span>';
    const values = numbers && !lost ? [p.run ? (p.run.minutes < 1 ? "gerade los" : `seit ${durText(p.run.minutes * 60)}`) : "",
      p.state === "ON" && p.power != null ? `jetzt ${wattText(p.power)}` : "",
      p.energy_today != null ? `heute ${powerKwh(p.energy_today * 1000)}` : ""].filter(Boolean)
      .map(x => x.replace(/ /g, "\u00a0")).join(" · ") : "";   // breaks only between the parts
    return `<li><span class="dico${lost ? " offline" : p.state === "ON" ? " on" : ""}" role="img" aria-label="Steckdose">${
      ICONS.plug}</span><span class="dname">${esc(p.name)}${sub ? `<small>${esc(sub)}</small>` : ""}</span>${status}${
      values ? `<span class="pval">${esc(values)}</span>` : ""}</li>`;
  }).join("")}</ul>`;
}

function roomLights(g) {   // the lamps of a room (a heater without room has none)
  return g.kind === "room" ? (state.lights || []).filter(l => l.room === g.id) : [];
}

function roomDetectors(g) {   // the motion sensors of a room
  return g.kind === "room" ? (state.detectors || []).filter(x => x.room === g.id) : [];
}

function clockText(iso) {   // "21:45" – the server's local time
  const t = localTime(iso);
  return t ? t.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "";
}

function agoText(iso) {   // „gerade eben“, „vor 5 Min.“, „vor 2 Std.“, „gestern 22:10 Uhr“
  const t = localTime(iso);
  if (!t) return "";
  const now = serverNow(), min = Math.floor((now - t) / 60000);
  if (min < 1) return "gerade eben";
  if (min < 60) return `vor ${min} Min.`;
  if (min < 12 * 60) return `vor ${Math.floor(min / 60)} Std.`;
  const days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) -
    new Date(t.getFullYear(), t.getMonth(), t.getDate())) / 86400000);
  return (days === 0 ? "heute " : days === 1 ? "gestern " : `am ${t.toLocaleDateString("de-DE", { day: "2-digit",
    month: "2-digit" })} `) + `${clockText(iso)} Uhr`;
}

function motionWords(g, short = false) {   // „Bewegung gerade“, „Bewegung vor 5 Min.“ – null: no motion sensor in the
  const m = g.room && g.room.motion;           // room (short: or nothing known yet – the app was just started)
  if (!m || !roomDetectors(g).length) return null;
  return m.moving ? "Bewegung gerade" : m.moved ? `Bewegung ${agoText(m.moved)}` : short ? null
    : "Noch keine Bewegung gemeldet";
}

function tileMotionHTML(g) {   // when someone moved in the room last – and a sensor that went silent
  const words = motionWords(g);
  if (!words) return "";
  const sensors = roomDetectors(g), silent = sensors.filter(x => x.silent);
  const note = !silent.length ? "" : sensors.length === 1 ? "Der Bewegungsmelder meldet sich nicht mehr."
    : `${silent.map(x => x.name).join(", ")} ${silent.length === 1 ? "meldet" : "melden"} sich nicht mehr.`;
  return `<p class="tile-motion"><span class="dico${g.room.motion.moving ? " on" : ""}" aria-hidden="true">${
    ICONS.motion}</span><span>${esc(words)}${note ? `<small>${esc(note)}</small>` : ""}</span></p>`;
}

function lightAutoText(g, on) {   // „An durch Bewegung · aus um 21:45 Uhr“, „Geht bei Bewegung an“
  const m = g.room && g.room.motion;
  if (!m) return "";
  if (m.auto) return `An durch Bewegung${m.off_at ? ` · aus um ${clockText(m.off_at)} Uhr` : ""}`;
  return m.on && !on ? "Geht bei Bewegung an" : "";
}

// ------------------------------------------------------------------ tiles that open and close
// A tap on a room's name opens or closes its tile; closed it shows its name, its temperature and one line. With up to
// TILES_OPEN_UPTO tiles they start open, with more closed – each phone remembers what was tapped (localStorage).
const TILES_OPEN_UPTO = 2;
let tilesOpen = (() => {
  try { const v = JSON.parse(localStorage.getItem("heizung.tiles") || "{}"); return v && typeof v === "object" ? v : {}; }
  catch (e) { return {}; }
})();

function homeGroups() {   // the rooms with a tile: heaters, plugs, light or a motion sensor
  return groups().filter(g => g.heaters.length || roomPlugs(g).length || roomLights(g).length || roomDetectors(g).length);
}

function tileOpen(g, count) {
  const v = tilesOpen[keyOf(g)];
  return typeof v === "boolean" ? v : count <= TILES_OPEN_UPTO;
}

function setTilesOpen(keys, open) {
  const known = new Set(homeGroups().map(keyOf));   // rooms that are gone are forgotten
  tilesOpen = Object.fromEntries(Object.entries(tilesOpen).filter(([k]) => known.has(k)));
  keys.forEach(k => { tilesOpen[k] = open; });
  localStorage.setItem("heizung.tiles", JSON.stringify(tilesOpen));
  render();
}

function toggleTile(tile) {
  const g = groupFor(tile);
  if (g) setTilesOpen([keyOf(g)], !tileOpen(g, homeGroups().length));
}

function tileHeadHTML(g, open, extra = "") {   // the room's name opens and closes the tile
  return `<div class="tile-head"><h2><button class="tile-toggle" data-action="tile-toggle" aria-expanded="${open}">${
    esc(g.name)}</button></h2>${extra}${CHEVRON}</div>`;
}

function tileSumHTML(parts) {   // closed: one line – [text, tone] with tone "warn" or "bad"
  const shown = parts.filter(x => x && x[0]);
  return shown.length ? `<p class="tile-sum">${shown.map(([text, tone]) => `<span${tone ? ` class="${tone}"` : ""}>${
    esc(text)}</span>`).join("")}</p>` : "";
}

function lightSumParts(g) {   // the light and the motion of a room for its closed tile
  const lamps = roomLights(g), on = lamps.filter(l => l.state === "ON" && !l.lost);
  const light = !lamps.length ? null : on.length ? [g.room && g.room.motion && g.room.motion.auto
    ? "Licht an durch Bewegung" : "Licht an"] : lamps.every(l => l.lost) ? ["Licht: keine Verbindung", "warn"]
    : ["Licht aus"];
  const words = motionWords(g, true);
  return [light, words && [words]];
}

function plugSumParts(g) {   // the plugs of a room for its closed tile: what runs, what is on
  const plugs = roomPlugs(g);
  if (!plugs.length) return [];
  const numbers = !!(state.costs && state.costs.show_power);
  const running = numbers ? plugs.filter(p => p.run && p.found && !p.error) : [];
  if (running.length) return running.map(p => [`${p.appliance !== "sonstiges" ? applianceText(p.appliance) : p.name} läuft`]);
  const lost = plugs.filter(p => !p.found || p.error), on = plugs.filter(p => p.state === "ON" && p.found && !p.error);
  if (lost.length === plugs.length) return [[plugs.length === 1 ? "Steckdose: keine Verbindung" : "Steckdosen: keine Verbindung", "warn"]];
  return [[plugs.length === 1 ? (on.length ? "Steckdose an" : "Steckdose aus")
    : on.length ? `${on.length} von ${plugs.length} Steckdosen an` : "Steckdosen aus"]];
}

function tileLightsHTML(g) {
  // the light of the room: „Licht einschalten“ / „Licht ausschalten“ for all its lamps, „dunkler“ and „heller“ while it
  // is on – and with several lamps each one on its own
  const lamps = roomLights(g);
  if (!lamps.length) return "";
  const key = "light:" + g.id, sending = busy.has(key);
  const on = lamps.filter(l => l.state === "ON" && !l.lost);
  const dim = on.filter(l => l.dim && l.brightness != null);
  const level = dim.length ? Math.round(avg(dim.map(l => l.brightness))) : null;
  const lost = lamps.filter(l => l.lost);
  const say = on.length ? `an${level != null && lamps.length === 1 ? ` · ${level} %` : ""}`
    : lost.length === lamps.length ? "keine Verbindung" : "aus";
  const tune = lamps.some(l => l.dim || l.kelvins || l.hues)   // each lamp on its own: brightness, colour of light, colour
    ? '<button class="light-tune" data-action="light-sheet">Einstellen ›</button>' : "";
  const auto = lightAutoText(g, on.length > 0);
  const head = `<div class="light-head"><span class="dico${on.length ? " on" : lost.length === lamps.length ? " offline"
    : ""}" aria-hidden="true">${ICONS.light}</span><span class="light-title">Licht</span><span class="pstate${on.length
    ? "" : " off"}">${say}</span>${tune}</div>${auto ? `<p class="light-auto">${esc(auto)}</p>` : ""}`;
  const toggle = `<button class="big-btn${on.length ? "" : " primary"}" data-action="light-room" data-on="${on.length ? 0 : 1}"
    ${sending ? "disabled" : ""}>${sending ? "wird gesendet …" : on.length ? "Licht ausschalten" : "Licht einschalten"}</button>`;
  const dimmer = !dim.length ? "" : `<div class="tile-set light-set">
      <button class="step" data-action="light-dim" data-step="-1" ${sending ? "disabled" : ""} aria-label="Dunkler"><span class="sym">−</span><span class="word">dunkler</span></button>
      <div class="set-val"><span class="set-temp">${level} %</span><span class="set-label">Helligkeit</span></div>
      <button class="step" data-action="light-dim" data-step="1" ${sending ? "disabled" : ""} aria-label="Heller"><span class="sym">+</span><span class="word">heller</span></button>
    </div>`;
  const rows = lamps.length < 2 ? "" : `<ul class="tile-lights">${lamps.map(l => {
    const lkey = "lamp:" + l.id, isOn = l.state === "ON" && !l.lost;
    const words = l.lost ? "keine Verbindung" : isOn ? `an${l.brightness != null ? ` · ${l.brightness} %` : ""}`
      : l.state === "OFF" ? "aus" : "–";
    return `<li data-light="${esc(l.id)}"><span class="dico${isOn ? " on" : l.lost ? " offline" : ""}" role="img"
      aria-label="Lampe">${ICONS.light}</span><span class="dname">${esc(l.name)}<small>${esc(words)}</small></span>
      <button class="small" data-action="light-one" data-on="${isOn ? 0 : 1}" ${busy.has(lkey) || sending ? "disabled" : ""}>${
        busy.has(lkey) ? "schaltet …" : isOn ? "Ausschalten" : "Einschalten"}</button></li>`;
  }).join("")}</ul>`;
  const note = !lost.length ? "" : `<p class="tile-note">${lamps.length === 1 ? "Die Lampe antwortet nicht."
    : `${esc(lost.map(l => l.name).join(", "))} ${lost.length === 1 ? "antwortet" : "antworten"} nicht.`} Ist der
    Lichtschalter an der Wand an?</p>`;
  return `<div class="tile-light">${head}${toggle}${dimmer}${rows}${note}</div>`;
}

function tileStyle(g, tint) {   // the warmth of the room – and the colour that marks it
  const parts = [tint ? `--tint:${tint}` : "", roomColorOf(g) ? `--room:${esc(roomColorOf(g))}` : ""].filter(Boolean);
  return parts.length ? ` style="${parts.join(";")}"` : "";
}

function plainTile(g, open = true) {   // a room without heaters: its name, a fresh sensor's temperature, its plugs, its
  const felt = roomTemp(g);                // light and its motion
  const start = `<article class="tile cool${open ? "" : " collapsed"}${busy.has("light:" + g.id) ? " busy" : ""}${
    roomColorOf(g) ? " colored" : ""}" data-kind="${g.kind}" data-id="${esc(g.id)}"${tileStyle(g, felt != null ? tileTint(felt) : "")}>`;
  if (!open) {
    return `${start}${tileHeadHTML(g, false, felt == null ? "" : `<span class="mini-temp">${degBig(felt)}</span>`)}
      ${tileSumHTML([...plugSumParts(g), ...lightSumParts(g)])}</article>`;
  }
  return `${start}
    ${tileHeadHTML(g, true)}
    ${felt == null ? "" : `<div class="tile-now"><span class="now-temp">${degBig(felt)}</span><span class="now-label">im Raum</span></div>`}
    ${tilePlugsHTML(g)}
    ${tileLightsHTML(g)}
    ${tileMotionHTML(g)}
  </article>`;
}

function homeTile(g, open = true) {
  const key = keyOf(g);
  const online = g.heaters.filter(h => h.online);
  const sensors = homeSensors(g);
  const writable = online.some(h => h.writable);
  const p = pending[key];
  const target = p ? p.temp : currentTarget(g);
  const mode = groupMode(g);
  const heating = online.some(h => h.heating);
  const isOff = ["off", "standby", "holiday"].includes(mode);
  const say = homeStatus(g, online, mode, target, writable);
  const dis = writable ? "" : "disabled";
  const felt = roomTemp(g);   // the big number – the tint and the colour at night follow it too
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
  const label = p || busy.has(key) ? '<span class="set-label sending">wird gesendet …</span>'
    : Date.now() - (sentAt[key] || 0) < SENT_MS ? '<span class="set-label sending">✓ gesendet</span>'
    : '<span class="set-label">eingestellt</span>';
  const setter = isOff || !online.length ? "" : `<div class="tile-set">
      <button class="step cold" data-action="home-sp" data-delta="-0.5" ${dis} aria-label="Kälter"><span class="sym">−</span><span class="word">kälter</span></button>
      <div class="set-val"><span class="set-temp">${degBig(target)}</span>${label}</div>
      <button class="step warm" data-action="home-sp" data-delta="0.5" ${dis} aria-label="Wärmer"><span class="sym">+</span><span class="word">wärmer</span></button>
    </div>`;
  const badge = !online.length ? ""
    : mode === "holiday" ? '<span class="badge off">Urlaub</span>'
    : isOff ? '<span class="badge off">aus</span>'
    : online.some(h => h.window_open) ? '<span class="badge hint">Fenster offen</span>'
    : mode === "boost" ? '<span class="badge">volle Kraft</span>'
    : heating ? '<span class="badge">heizt</span>' : "";
  const tint = !online.length ? TINT_OFFLINE : isOff || felt == null ? "" : tileTint(felt);
  const glow = online.length && !isOff && felt != null && felt >= 18 ? "warm" : "cool";   // the number's colour at night
  const boost = mode === "boost" ? boostLeft(online) : null;
  // a thermostat that has been silent for hours wakes up with a press of its middle button
  const silent = g.heaters.some(h => h.kind === "zigbee" && !h.online && /mittlere Taste/.test(h.error || ""));
  const low = g.heaters.filter(h => h.kind === "zigbee" && h.battery_low);
  const battery = !low.length ? "" : `<p class="tile-note">${g.heaters.length === 1 ? "Batterie im Thermostat bald wechseln."
    : "Batterie bald wechseln: " + esc(low.map(h => h.name).join(", ")) + "."}</p>`;
  // a plug switched off at its button: the heater behind it has no power – one tap switches it on again
  const unplugged = g.heaters.filter(h => h.plug && h.plug.state === "OFF");
  const plugText = !unplugged.length ? "" : g.heaters.length === 1 ? "Die Steckdose ist aus – die Heizung hat keinen Strom."
    : `Steckdose aus – kein Strom für ${unplugged.map(h => h.name).join(", ")}.`;
  unplugged.slice().reverse().forEach(h => actions.unshift(`<button class="big-btn primary" data-action="plug-on" data-heater="${
    esc(h.id)}" ${busy.has(key) ? "disabled" : ""}>${unplugged.length === 1 ? "Steckdose einschalten" : `Steckdose für ${esc(h.name)} einschalten`}</button>`));
  const classes = ["tile", glow, isOff ? "is-off" : "", online.length ? "" : "offline", heating && !isOff ? "heating" : "",
                   busy.has(key) ? "busy" : "", open ? "" : "collapsed", roomColorOf(g) ? "colored" : ""].filter(Boolean).join(" ");
  if (!open) {   // closed: name and temperature, then what the room does in one line
    const heat = !online.length ? [["keine Verbindung", "bad"]]
      : [[mode === "holiday" ? "Urlaub" : isOff ? "aus" : target != null ? `Soll ${degShort(target)}` : ""],
         [mode === "boost" ? "volle Kraft" : heating && !isOff ? "heizt" : ""],
         [online.some(h => h.window_open) ? "Fenster offen" : "", "warn"],
         [unplugged.length ? "Steckdose aus" : "", "warn"], [low.length ? "Batterie bald wechseln" : "", "warn"]];
    return `<article class="${classes}" data-kind="${g.kind}" data-id="${esc(g.id)}"${tileStyle(g, tint)}>
      ${tileHeadHTML(g, false, felt == null ? "" : `<span class="mini-temp">${degBig(felt)}</span>`)}
      ${tileSumHTML([...heat, ...plugSumParts(g), ...lightSumParts(g)])}</article>`;
  }
  return `<article class="${classes}" data-kind="${g.kind}" data-id="${esc(g.id)}"${tileStyle(g, tint)}>
    ${tileHeadHTML(g, true, badge)}
    <div class="tile-now"><span class="now-temp">${degBig(felt)}</span>${felt == null ? "" : '<span class="now-label">im Raum</span>'}</div>
    ${setter}
    <p class="say ${say.tone}">${esc(say.text)}${online.length ? "" : ` <span class="more">${silent
      ? "Am Thermostat einmal die mittlere Taste drücken." : plugText ? esc(plugText) : "Die App versucht es immer wieder."}</span>`}</p>
    ${online.length && plugText ? `<p class="tile-note">${esc(plugText)}</p>` : ""}
    ${battery}
    ${boost == null ? "" : `<div class="boost-bar" aria-hidden="true"><span style="width:${Math.round(boost * 100)}%"></span></div>`}
    ${tileDevicesHTML(g, sensors)}
    ${tilePlugsHTML(g)}
    ${tileHistoryHTML(g)}
    ${actions.length ? `<div class="tile-actions">${actions.join("")}</div>` : ""}
    ${tileLightsHTML(g)}
    ${tileMotionHTML(g)}
    ${heating && !isOff ? WAVE : ""}
  </article>`;
}

function renderHome() {
  const now = new Date();
  document.getElementById("clock").textContent =
    now.toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" }) + " · " +
    now.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) + " Uhr";
  if (!state) return;
  renderHelp();
  renderLogFoot();
  const gs = homeGroups(), opens = gs.map(g => tileOpen(g, gs.length));   // only plugs, light or motion: a tile too
  const tiles = document.getElementById("tiles");
  const html = gs.length ? gs.map((g, i) => g.heaters.length ? homeTile(g, opens[i]) : plainTile(g, opens[i])).join("")
    : '<p class="home-empty">Noch keine Heizung eingerichtet. Das geht über „Einrichtung“ oben rechts.</p>';
  const tools = document.getElementById("tiles-tools");   // with several tiles: all open or all closed at once
  tools.hidden = gs.length <= TILES_OPEN_UPTO;
  if (!tools.hidden) setHTML(tools, `<button class="link-btn" data-action="tiles-all" data-open="${opens.every(Boolean)
    ? 0 : 1}">${opens.every(Boolean) ? "Alle zuklappen" : "Alle aufklappen"}</button>`);
  if (html !== tiles._html)   // new tiles: their heat waves go on where the old ones were, instead of starting over
    document.documentElement.style.setProperty("--wave-delay", `-${(performance.now() % 8000 / 1000).toFixed(2)}s`);
  setHTML(tiles, html);
  const notices = state.notices || [], box = document.getElementById("notices");   // „… ist fertig“ until OK
  box.hidden = !notices.length;
  setHTML(box, notices.map(n => `<div class="notice" role="status"><p>${esc(n.text)}</p><button class="big-btn primary"
    data-action="notice-ok" data-notice="${esc(n.id)}" ${busy.has("notice:" + n.id) ? "disabled" : ""}>OK</button></div>`).join(""));
  const pt = state.power_today, line = document.getElementById("home-power");   // only with its switch
  line.hidden = !(pt && pt.wh != null);
  if (!line.hidden) setHTML(line, `Strom heute <b>${esc(kwhText(pt.wh / 1000))}</b>${pt.eur != null
    ? ` · etwa ${esc(euroText(pt.eur))}` : ""}<span class="muted"> · gemessen an ${pt.plugs === 1 ? "einer Steckdose"
    : `${pt.plugs} Steckdosen`}</span>`);
  renderLightSheet();
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

function noticeOk(id) {   // „OK“ on „Die Waschmaschine ist fertig“ – anyone may tap it, without PIN
  act("notice:" + id, async () => {
    try { await api("POST", `api/notices/${encodeURIComponent(id)}/ok`); }
    catch (e) { if (!/nicht mehr/.test(e.message)) throw e; }   // gone already (someone else tapped it)
  });
}

function lightRoom(g, on) {   // „Licht einschalten“ / „Licht ausschalten“: every lamp of the room – anyone may
  if (!g) return;
  act("light:" + g.id, async () => {
    const res = await api("POST", `api/rooms/${encodeURIComponent(g.id)}/light`, { on });
    toast(`${g.name}: ${res.message}`, !!res.warning);
  });
}

function lightDim(g, step) {   // „heller“ / „dunkler“: the number on the tile says it – a note only if something is up
  if (!g) return;
  act("light:" + g.id, async () => {
    const res = await api("POST", `api/rooms/${encodeURIComponent(g.id)}/light/dim`, { step });
    if (res.warning || !/^Helligkeit/.test(res.message)) toast(`${g.name}: ${res.message}`, !!res.warning);
  });
}

function lightOne(id, on) {   // one lamp on its own – on the tile (several lamps in the room) and in Geräte › Licht
  if (!id) return;
  act("lamp:" + id, async () => {
    const res = await api("POST", `api/lights/${encodeURIComponent(id)}/switch`, { on });
    toast(res.message, !!res.warning);
  });
}

function lampSet(id, body) {   // „Einstellen“: the lamp's new value shows at the lamp – a note only if something is up
  if (!id) return;
  act("lamp:" + id, async () => {
    const res = await api("POST", `api/lights/${encodeURIComponent(id)}/set`, body);
    if (res.warning || /geht es nicht/.test(res.message)) toast(res.message, !!res.warning);
  });
}

function openLightSheet(g) {   // „Einstellen ›“ at the light of a room: each of its lamps on its own – anyone may
  if (!g) return;
  lightSheet = { room: g.id };
  renderLightSheet();
  const close = document.querySelector("#lightsheet .sheet-close");
  if (close) close.focus();
}

function closeLightSheet() {
  if (!lightSheet) return;
  lightSheet = null;
  lampDrag = false;
  renderLightSheet();
}

function renderLightSheet() {
  const el = document.getElementById("lightsheet");
  const g = lightSheet && state ? groups().find(x => x.kind === "room" && x.id === lightSheet.room) : null;
  const lamps = g ? roomLights(g) : [];
  if (lightSheet && !lamps.length) lightSheet = null;   // the room has no lamp (any more)
  document.body.classList.toggle("sheet-open", !!hist || !!lightSheet);
  if (!lightSheet) { el.hidden = true; el.innerHTML = ""; el._html = null; return; }
  const active = document.activeElement;
  if (lampDrag || (el.contains(active) && active.type === "range")) return;   // never redraw under the finger
  setHTML(el, `<div class="sheet light-sheet" role="dialog" aria-modal="true" aria-labelledby="ls-title">
    <div class="sheet-head"><h2 id="ls-title"><span class="kicker">Licht</span>${esc(g.name)}</h2>
      <button class="sheet-close" data-action="ls-close">Schließen</button></div>
    ${lamps.map(lampPanelHTML).join("")}
  </div>`);
  el.hidden = false;
}

function lampPanelHTML(l) {   // one lamp in „Einstellen“: on and off, then what it can
  const sending = busy.has("lamp:" + l.id), isOn = l.state === "ON" && !l.lost;
  return `<section class="lamp-panel" data-light="${esc(l.id)}">
    <div class="lamp-head"><span class="dico${isOn ? " on" : l.lost ? " offline" : ""}" aria-hidden="true">${ICONS.light}</span>
      <h3 class="dname">${esc(l.name)}<small>${esc(sending ? "wird gesendet …" : lightFacts(l) || "–")}</small></h3>
      <button class="big-btn${isOn ? "" : " primary"}" data-action="light-one" data-on="${isOn ? 0 : 1}" ${sending
        ? "disabled" : ""}>${isOn ? "Ausschalten" : "Einschalten"}</button></div>
    ${l.dim ? `<div class="lamp-row"><span class="lamp-label">Helligkeit</span>${lampBrightnessHTML(l)}</div>` : ""}
    ${l.kelvins ? `<div class="lamp-row"><span class="lamp-label">Lichtfarbe</span>${lampWhiteHTML(l)}</div>` : ""}
    ${l.hues ? `<div class="lamp-row"><span class="lamp-label">Farbe</span>${lampHueHTML(l)}</div>` : ""}
    ${l.lost ? '<p class="tile-note">Die Lampe antwortet nicht. Ist der Lichtschalter an der Wand an?</p>' : ""}
  </section>`;
}

function lampBrightnessHTML(l) {   // „dunkler“ and „heller“ in steps of 10 %, the slider for anything in between
  const dis = busy.has("lamp:" + l.id) ? "disabled" : "", b = l.brightness;
  const steps = !dis && l.state === "ON" && !l.lost && b != null ? "" : "disabled";   // the slider also switches on
  return `<div class="lamp-control">
    <div class="tile-set light-set">
      <button class="step" data-action="lamp-step" data-step="-1" ${steps} aria-label="Dunkler"><span class="sym">−</span><span class="word">dunkler</span></button>
      <div class="set-val"><span class="set-temp" data-lval="brightness">${b != null ? `${b} %` : "–"}</span></div>
      <button class="step" data-action="lamp-step" data-step="1" ${steps} aria-label="Heller"><span class="sym">+</span><span class="word">heller</span></button>
    </div>
    <input type="range" class="lamp-range" data-action="lamp-range" data-what="brightness" min="1" max="100" step="1"
      value="${b ?? 100}" ${dis} aria-label="Helligkeit von ${esc(l.name)} in Prozent">
  </div>`;
}

function lampWhiteHTML(l) {   // the colours of light with a name, the slider from warm to cool for everything it can
  const dis = busy.has("lamp:" + l.id) ? "disabled" : "", [k0, k1] = l.kelvins;
  const near = c => l.kelvin != null && Math.abs(Math.min(Math.max(LIGHT_KELVIN[c], k0), k1) - l.kelvin) <= 60;
  return `<div class="lamp-control">
    <div class="chip-row" role="group" aria-label="Lichtfarbe von ${esc(l.name)}">${l.colors.map(c => `<button class="lamp-chip${
      near(c) ? " active" : ""}" data-action="lamp-white" data-white="${c}" aria-pressed="${near(c)}" ${dis}>${
      LIGHT_COLOR_WORDS[c]}</button>`).join("")}</div>
    <div class="range-ends"><span>warm</span><input type="range" class="lamp-range kelvin" data-action="lamp-range"
      data-what="kelvin" min="${k0}" max="${k1}" step="100" value="${l.kelvin ?? Math.round((k0 + k1) / 200) * 100}" ${dis}
      aria-label="Lichtfarbe von ${esc(l.name)} in Kelvin, links warm, rechts kühl"><span>kühl</span></div>
    <span class="lamp-value muted" data-lval="kelvin">${l.kelvin != null ? `${l.kelvin} K` : l.hue
      ? "zeigt gerade eine Farbe" : ""}</span>
  </div>`;
}

function lampHueHTML(l) {   // a lamp with colours: eight of them, each by its name
  const dis = busy.has("lamp:" + l.id) ? "disabled" : "";
  return `<div class="chip-row" role="group" aria-label="Farbe von ${esc(l.name)}">${LIGHT_HUES.map(([key, word, h]) =>
    `<button class="lamp-chip${l.hue === key ? " active" : ""}" data-action="lamp-hue" data-hue="${key}" aria-pressed="${
      l.hue === key}" ${dis}><span class="swatch" style="--hue:${h}" aria-hidden="true"></span>${word}</button>`).join("")}</div>`;
}

function plugOn(g, id) {   // „Steckdose einschalten“: the plug of an AeroFlow gives it power again
  if (!g || !id) return;
  act(keyOf(g), async () => toast((await api("POST", `api/heaters/${encodeURIComponent(id)}/plug/on`)).message));
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

// ------------------------------------------------------------------ Fernhilfe in the residents' view
let helpPending = false;   // „Fernhilfe erlauben“ or „Beenden“ pressed, the answer is still out

function helpTime(minutes) {  // 120 -> "zwei Stunden"
  const m = minutes || 120;
  if (m % 60) return m === 1 ? "eine Minute" : `${m} Minuten`;
  return m === 60 ? "eine Stunde" : m === 120 ? "zwei Stunden" : `${m / 60} Stunden`;
}
function clockOf(at) { return new Date(at).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }); }
function helpUntil(r) {  // end of the Fernhilfe while it lasts, else null
  return r && r.supported && r.mode === "ask" && r.until && Date.parse(r.until) > Date.now() ? r.until : null;
}

function renderHelp() {
  const r = state.remote || {};
  const until = helpUntil(r);
  document.getElementById("remote-hint").hidden = !(r.on && !until);   // residents know when someone can look in
  const bar = document.getElementById("help-bar"), foot = document.getElementById("help-foot");
  const dis = helpPending ? "disabled" : "";
  bar.hidden = !until;
  if (until) setHTML(bar, `<p>${r.on ? `<b>Fernhilfe ist an</b> – bis ${esc(clockOf(until))} Uhr. Die Heizung kann jetzt aus
      der Ferne eingestellt werden.` : `<b>Fernhilfe ist erlaubt</b> bis ${esc(clockOf(until))} Uhr – verbindet sich noch …`}</p>
    <button class="big-btn" data-action="help-end" ${dis}>Beenden</button>`);
  const offer = r.supported && r.mode === "ask" && r.ready && !r.afar;   // from afar nobody can allow it
  foot.hidden = !offer;
  if (offer) setHTML(foot, `<button class="big-btn quiet" data-action="help-start" ${dis}>${until ? "Fernhilfe verlängern …"
      : "Fernhilfe erlauben …"}</button>
    ${until ? "" : `<p>Damit sich die Heizung aus der Ferne einstellen lässt – für ${esc(helpTime(r.help_minutes))}.</p>`}`);
}

async function helpStart() {
  const r = state.remote || {};
  const longer = !!helpUntil(r);
  const until = clockOf(Date.now() + (r.help_minutes || 120) * 60000);
  const yes = await ask({
    title: longer ? "Fernhilfe verlängern?" : "Fernhilfe erlauben?",
    text: longer ? `Die Fernhilfe läuft dann bis etwa ${until} Uhr.`
      : `Die Heizungs-App lässt sich dann ${helpTime(r.help_minutes)} lang aus der Ferne öffnen und einstellen, bis etwa ` +
        `${until} Uhr. Danach endet die Fernhilfe von selbst – oder vorher mit „Beenden“.`,
    buttons: [{ label: "Abbrechen", value: null }, { label: longer ? "Ja, verlängern" : "Ja, erlauben", value: "yes", cls: "primary" }],
  });
  if (yes) helpDo("api/remote/help");
}

async function helpEnd() {
  const yes = await ask({
    title: "Fernhilfe beenden?",
    text: (state.remote && state.remote.afar ? "Du bist gerade aus der Ferne hier – diese Seite ist danach weg. " : "") +
      "Die Heizungs-App lässt sich dann nicht mehr aus der Ferne öffnen.",
    buttons: [{ label: "Nein", value: null }, { label: "Ja, beenden", value: "yes", cls: "primary" }],
  });
  if (yes) helpDo("api/remote/help/end");
}

async function helpDo(url) {
  helpPending = true;
  render();
  try {
    const res = await api("POST", url);
    if (res.remote) state.remote = res.remote;
    if (res.message) toast(res.message);
  } catch (e) { toast(e.message, true); }
  helpPending = false;
  await refresh();
}

// ------------------------------------------------------------------ Verlauf und Kosten (a room's day, Strom und Kosten)
let hist = null;       // the open „Verlauf“: {kind, id, from: "home"|"setup", day, data, error, loading, table}
let lightSheet = null; // the open „Einstellen ›“ of a room's light on the start page: {room}
let lampDrag = false;  // a finger or the mouse on a slider of a lamp: nothing is redrawn under it
let histHover = null;  // start (s) of the five minutes under the finger or the mouse
let histResize = 0;
let costForm = null;   // „Strom und Kosten“ while it is being typed in: {price, show, show_power, powers: {heater id: text}}
const POWER_VALUES = [650, 1200, 1300, 1600, 1950, 2000, 2450];   // W, the FlexiSmart Pro models A01–A14 (manual)

function durText(s) {  // 7800 -> "2 Std. 10 Min."
  const m = Math.round((s || 0) / 60), h = Math.floor(m / 60), r = m % 60;
  if (!m) return s > 0 ? "unter 1 Min." : "0 Min.";
  return h ? (r ? `${h} Std. ${r} Min.` : `${h} Std.`) : `${r} Min.`;
}
function euroText(x) { return x.toFixed(2).replace(".", ",") + " €"; }
function kwhText(x) { return x.toFixed(x < 1 ? 2 : x < 10 ? 1 : 0).replace(".", ",") + " kWh"; }
function ctText(x) { return String(x).replace(".", ","); }
function costsShown(from) { return from === "setup" || !!(state && state.costs && state.costs.show); }   // the switch
function recordingKnown() { return !!(state && state.history && (state.history.recording || state.history.first_day)); }
function todayOf(g) { return g.kind === "room" ? g.room && g.room.today : g.heaters[0] && g.heaters[0].today; }
function addDays(day, n) {  // "2026-11-25", -1 -> "2026-11-24" (calendar days)
  const d = new Date(day + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function histClock(t, d) {  // "14:35" in the app's time zone
  const opt = { hour: "2-digit", minute: "2-digit" };
  try { return new Date(t * 1000).toLocaleTimeString("de-DE", d.tz ? Object.assign({ timeZone: d.tz }, opt) : opt); }
  catch (_) { return new Date(t * 1000).toLocaleTimeString("de-DE", opt); }
}

function todayText(t, from) {  // "Heute 2 Std. 10 Min. geheizt · etwa 0,90 €" – in the setup with the kWh as well
  let text = t.heat_s >= 60 ? `Heute ${durText(t.heat_s)} geheizt` : "Heute noch nicht geheizt";
  if (!costsShown(from) || t.heat_s < 60) return text;
  if (t.kwh != null && (from === "setup" || t.eur == null)) text += ` · etwa ${kwhText(t.kwh)}`;
  if (t.eur != null) text += ` · ${from === "setup" ? "" : "etwa "}${euroText(t.eur)}`;
  return text;
}

function tileHistoryHTML(g) {  // on the home tile: how long the room heated today – a tap opens its „Verlauf“
  const t = todayOf(g);
  if (!t || !recordingKnown()) return "";
  return `<button class="tile-history" data-action="history"><span>${esc(todayText(t, "home"))}</span>` +
    '<span class="go">Verlauf ›</span></button>';
}

function cardHistoryHTML(g) {  // the same in the setup's room card, with the energy
  const t = todayOf(g);
  if (!t || !recordingKnown()) return "";
  return `<div class="today-row"><span>${esc(todayText(t, "setup"))}</span>` +
    '<button class="small" data-action="history">Verlauf</button></div>';
}

function openHistory(card, from) {
  const g = groupFor(card);
  if (!g || !state) return;
  hist = { kind: g.kind, id: g.id, from, day: state.now.slice(0, 10), data: null, error: "", loading: true, table: false };
  histHover = null;
  renderHistory();
  loadHistory();
}

function closeHistory() {
  if (!hist) return;
  hist = null;
  renderHistory();
}

function openPlugEnergy(id) {   // „Strom“ of a plug: what flowed through it in every quarter hour, day by day
  if (!state) return;
  hist = { kind: "plug", id, from: "setup", day: state.now.slice(0, 10), data: null, error: "", loading: true, table: false,
          first: null };
  histHover = null;
  renderHistory();
  loadHistory();
}

function openRun(id, start) {   // one wash or drying: the power of every minute
  if (!state) return;
  hist = { kind: "run", id, from: "setup", day: String(start), data: null, error: "", loading: true, table: false };
  histHover = null;
  renderHistory();
  loadHistory();
}

async function loadHistory(day) {
  const h = hist;
  if (!h) return;
  if (day && day !== h.day) { h.day = day; h.loading = true; histHover = null; renderHistory(); }
  try {
    const data = await api("GET", h.kind === "plug" ? `api/plugs/${encodeURIComponent(h.id)}/energy/${h.day}`
      : h.kind === "run" ? `api/plugs/${encodeURIComponent(h.id)}/runs/${h.day}` : `${apiBase(h)}/history/${h.day}`);
    if (hist !== h || data.day !== h.day) return;   // closed, or another day chosen meanwhile
    h.data = data;
    if (h.kind === "plug") h.first = data.first_day;
    h.error = "";
  } catch (e) {
    if (hist !== h) return;
    h.error = e.message;
  }
  h.loading = false;
  renderHistory();
}

function renderHistory() {
  const el = document.getElementById("history");
  document.body.classList.toggle("sheet-open", !!hist || !!lightSheet);
  if (!hist || !state) { el.hidden = true; el.innerHTML = ""; el._html = null; return; }
  const h = hist, d = h.data && h.data.day === h.day ? h.data : null;
  const today = state.now.slice(0, 10), plug = h.kind === "plug", run = h.kind === "run";
  const first = plug ? h.first : state.history && state.history.first_day;
  const prev = run ? d && d.prev : first && h.day > first ? addDays(h.day, -1) : null;
  const next = run ? d && d.next : h.day < today ? addDays(h.day, 1) : null;   // a run: the one before and after
  const g = plug || run ? (state.plugs || []).find(x => x.id === h.id) : groups().find(x => x.kind === h.kind && x.id === h.id);
  const name = d ? d.name : g ? g.name : "";
  const step = (to, label, sym) => `<button class="day-step" data-action="hist-day" data-day="${to || ""}" ${
    to && !h.loading ? "" : "disabled"} aria-label="${label}">${sym}</button>`;
  const kicker = run ? ((RUN_WORDS[d ? d.appliance : g && g.appliance] || [])[0] || "Durchgang") : plug ? "Strom" : "Verlauf";
  setHTML(el, `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="hist-title">
    <div class="sheet-head"><h2 id="hist-title"><span class="kicker">${esc(kicker)}</span>${esc(name)}</h2>
      <button class="sheet-close" data-action="hist-close">Schließen</button></div>
    <div class="hist-day">${step(prev, run ? "Durchgang davor" : "Tag davor", "‹")}<span class="day-label">${
      esc(d ? d.label : h.loading ? "…" : "")}</span>${step(next, run ? "Durchgang danach" : "Tag danach", "›")}</div>
    ${d ? (run ? runBodyHTML(d) : plug ? energyBodyHTML(d) : historyBodyHTML(d, h.from))
      : `<p class="hist-empty">${esc(h.error || (run ? "Der Durchgang wird geladen …" : plug ? "Der Strom wird geladen …"
        : "Der Verlauf wird geladen …"))}</p>`}
  </div>`);
  el.hidden = false;
  drawSheetChart();
}

function drawSheetChart() {
  return !hist ? undefined : hist.kind === "plug" ? drawEnergyChart() : hist.kind === "run" ? drawRunChart() : drawHistoryChart();
}

function runBodyHTML(d) {   // one run: how long, how much – and the power of every minute
  const stats = [["Dauer", durText(d.end - d.start)], ["Strom", powerKwh(d.wh)]];
  if (d.eur != null) stats.push(["Kosten", "etwa " + euroText(d.eur)]);
  stats.push(["Höchste Leistung", wattText(d.w_max)]);
  if (d.heat_min) stats.push(["Heizzeit", `${d.heat_min} Min.`]);
  const hot = d.minutes.some(v => v > 1000), notes = [];
  if (!d.minutes.length) notes.push("Für diesen Durchgang gibt es keine Kurve.");
  notes.push("Jeder Balken ist eine Minute: wie viel Strom das Gerät darin im Schnitt gezogen hat. Über 1.000 W heizt es " +
    "(das Wasser, die Luft) – wie warm, weiß die App nicht.");
  if (d.price_ct == null) notes.push("Ohne Strompreis keine Kosten. Eintragen unter „Strom“ › „Strom und Kosten“.");
  return `<dl class="hist-stats">${stats.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="hist-chart" id="hist-chart"></div>
    <ul class="hist-legend"><li><span class="key power" aria-hidden="true"></span>Leistung je Minute</li>${
      hot ? '<li><span class="key heat" aria-hidden="true"></span>heizt (über 1.000 W)</li>' : ""}</ul>
    <ul class="hist-notes">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`;
}

function drawRunChart() {   // one bar per minute, as high as the power in it – the minutes it heats in the heat colour
  const box = document.getElementById("hist-chart");
  const d = hist && hist.data && hist.data.day === hist.day ? hist.data : null;
  if (!box || !d || d.kind !== "run") return;
  if (!d.minutes.length) { box.innerHTML = ""; box._geo = null; return; }
  const n = v => v.toFixed(1), start = d.start, end = start + d.minutes.length * d.slot;
  const W = Math.max(280, Math.round(box.clientWidth || 600));
  const left = 58, right = 12, top = 22, plotH = W < 520 ? 170 : 220, base = top + plotH, axisY = base + 20, H = axisY + 6;
  const x0 = left, x1 = W - right, X = t => x0 + (t - start) / (end - start) * (x1 - x0);
  const most = Math.max(0, ...d.minutes);
  const nice = x => { const e = Math.pow(10, Math.floor(Math.log10(x))); return [1, 2, 5, 10].map(f => f * e).find(v => x <= v); };
  const step = nice(Math.max(most, 8) / 4), hi = Math.max(step, Math.ceil(most / step) * step);
  const Y = v => base - Math.min(v, hi) / hi * plotH;
  const parts = [];
  for (let v = 0; v <= hi; v += step)
    parts.push(`<line class="grid" x1="${x0}" x2="${x1}" y1="${n(Y(v))}" y2="${n(Y(v))}"/>` +
      `<text class="ylab" x="${x0 - 8}" y="${n(Y(v) + 4)}">${esc(wattText(v))}</text>`);
  const every = end - start <= 3600 ? 900 : end - start <= 4 * 3600 ? 1800 : 3600, room = (end - start) * 0.12;
  const ticks = [start];
  for (let t = Math.ceil(start / every) * every; t < end; t += every) if (t - start > room && end - t > room) ticks.push(t);
  ticks.push(end);
  for (const t of ticks) {
    const x = X(t), anchor = t === start ? "start" : t === end ? "end" : "middle";
    if (t > start && t < end) parts.push(`<line class="grid" x1="${n(x)}" x2="${n(x)}" y1="${top}" y2="${base}"/>`);
    parts.push(`<text class="xlab" x="${n(x)}" y="${axisY}" text-anchor="${anchor}">${esc(histClock(t, d))}</text>`);
  }
  const width = X(start + d.slot) - X(start), gap = width > 4 ? 1 : 0;   // dense bars run into one another
  d.minutes.forEach((v, i) => {
    if (!(v > 0)) return;
    const h = Math.max(1.5, base - Y(v)), x = X(start + i * d.slot) + gap / 2;
    parts.push(`<rect class="qbar${v > 1000 ? " hot" : ""}" x="${n(x)}" y="${n(base - h)}" width="${n(Math.max(0.5, width - gap))}" height="${n(h)}"/>`);
  });
  parts.push(`<line class="lane-base" x1="${x0}" x2="${x1}" y1="${base}" y2="${base}"/>`);
  parts.push(`<g class="cross" visibility="hidden"><line y1="${top}" y2="${base}"/></g>`);
  box.innerHTML = `<svg class="hist-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${
    esc(`Leistung je Minute, ${d.label}`)}">${parts.join("")}</svg><div class="hist-tip" hidden></div>`;
  box._geo = { d: Object.assign({}, d, { end }), m: { until: end }, X, x0, x1, top, W };
  box.onpointermove = box.onpointerdown = ev => {
    const t = histSlotAt(ev);
    if (t !== histHover) { histHover = t; showRunHover(); }
  };
  box.onpointerleave = ev => { if (ev.pointerType === "mouse") { histHover = null; showRunHover(); } };
  showRunHover();
}

function showRunHover() {   // a line through the minute at histHover and the power in it
  const box = document.getElementById("hist-chart"), g = box && box._geo;
  if (!g) return;
  const cross = box.querySelector(".cross"), tip = box.querySelector(".hist-tip"), t = histHover, d = g.d;
  if (t == null || t < d.start || t >= g.m.until) { cross.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
  const v = d.minutes[Math.round((t - d.start) / d.slot)], x = g.X(t + d.slot / 2), line = cross.querySelector("line");
  line.setAttribute("x1", x);
  line.setAttribute("x2", x);
  cross.setAttribute("visibility", "visible");
  tip.innerHTML = `<span><b>${esc(histClock(t, d))} Uhr</b></span><span>${v == null ? "keine Werte" : esc(wattText(v))}</span>`;
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = `${Math.round(x + 14 + w <= g.W ? x + 14 : Math.max(0, x - 14 - w))}px`;
}

function historyBodyHTML(d, from) {
  const costs = costsShown(from), setup = from === "setup", m = histModel(d);
  const win = d.points.reduce((a, p) => a + (p.fenster || 0), 0);
  const stats = [["Geheizt", durText(d.heat_s)]];
  if (costs && d.kwh != null) stats.push(["Strom", "etwa " + kwhText(d.kwh)]);
  if (costs && d.eur != null) stats.push(["Kosten", "etwa " + euroText(d.eur)]);
  if (win >= 60) stats.push(["Fenster offen", durText(win)]);
  const legend = [["ist", "im Raum"], ["soll", "eingestellt"], ["heat", "heizt"]]
    .concat(m.hasWin ? [["win", "Fenster offen"]] : [], m.gaps.length ? [["gap", "keine Werte"]] : []);
  const devices = d.devices.length > 1 || setup ? `<ul class="hist-devices">${d.devices.map(x => `<li>${
    deviceIcon({ kind: x.kind, online: true })}<span>${esc(x.name)}</span><span class="dval">${esc(durText(x.heat_s))}${
    costs && x.kwh != null ? ` · etwa ${esc(kwhText(x.kwh))}` : ""}${
    setup && x.kind !== "zigbee" && !x.power_w ? " · Leistung fehlt" : ""}</span></li>`).join("")}</ul>` : "";
  const notes = [];
  if (!d.points.length) notes.push(d.now ? "Heute gibt es noch keine Werte." : "Für diesen Tag gibt es keine Werte.");
  if (d.now && state.history && !state.history.recording)
    notes.push("Gerade wird nichts aufgezeichnet. Nach dem Einschalten wartet die App, bis die Uhrzeit stimmt.");
  if (costs && d.kwh != null)
    notes.push(`Strom und Kosten sind geschätzt: Heizdauer × Leistung der Heizung${d.price_ct != null
      ? ` × Strompreis (${ctText(d.price_ct)} ct/kWh)` : ""}.`);
  if (setup && d.power_missing.length)
    notes.push(`Ohne Leistung kein Strom: ${d.power_missing.join(", ")}. Eintragen unter „Strom“ › „Strom und Kosten“.`);
  if (setup && d.kwh != null && d.price_ct == null) notes.push("Ohne Strompreis keine Kosten. Eintragen unter „Strom“ › „Strom und Kosten“.");
  if (costs && d.devices.some(x => x.kind === "zigbee")) notes.push("Thermostate zählen nur die Heizdauer, die Wärme kommt vom Heizkessel.");
  return `<dl class="hist-stats">${stats.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="hist-chart" id="hist-chart"></div>
    <ul class="hist-legend">${legend.map(([k, label]) => `<li><span class="key ${k}" aria-hidden="true"></span>${label}</li>`).join("")}</ul>
    ${devices}
    ${notes.length ? `<ul class="hist-notes">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    ${hourTableHTML(d)}`;
}

function whText(wh, keepWh) {  // 412.5 -> "413 Wh", 1840 -> "1,8 kWh" (with keepWh "1.840 Wh", for the table)
  if (wh >= 1000 && !keepWh) return kwhText(wh / 1000);
  return (wh < 10 ? (Math.round(wh * 10) / 10).toLocaleString("de-DE") : Math.round(wh).toLocaleString("de-DE")) + " Wh";
}

function lastOfMonth(day) {  // "2026-11-26" -> "2026-11-30"
  const [y, m] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
}

function energyBodyHTML(d) {  // „Strom“ of a plug: what flowed through it in every quarter hour of the day
  const stats = [["Strom", d.kwh != null ? kwhText(d.kwh) : "–"]];
  if (d.eur != null) stats.push(["Kosten", "etwa " + euroText(d.eur)]);
  if (d.w_max != null) stats.push(["Höchste Leistung", wattText(d.w_max)]);
  const where = [d.heater ? `an ${d.heater}` : "", d.room ? `im Raum „${d.room}“` : ""].filter(Boolean).join(" · ");
  const m = energyModel(d), notes = [];
  if (!d.points.length) notes.push(d.now ? "Heute gibt es noch keine Werte." : "Für diesen Tag gibt es keine Werte.");
  if (d.now && !d.recording)
    notes.push("Gerade wird nichts aufgezeichnet. Nach dem Einschalten wartet die App, bis die Uhrzeit stimmt.");
  notes.push("Jeder Balken ist eine Viertelstunde, gemessen vom Zähler der Steckdose. Meldet sie sich eine Weile nicht, " +
    "verteilt die App, was in der Zeit floss, gleichmäßig auf die Viertelstunden.");
  if (d.kwh != null && d.price_ct == null) notes.push("Ohne Strompreis keine Kosten. Eintragen unter „Strom“ › „Strom und Kosten“.");
  const month = d.day.slice(0, 8) + "01";
  return `${where ? `<p class="hist-where">${esc(where)}</p>` : ""}
    <dl class="hist-stats">${stats.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="hist-chart" id="hist-chart"></div>
    <ul class="hist-legend"><li><span class="key power" aria-hidden="true"></span>Strom je Viertelstunde</li>${
      m.gaps.length ? '<li><span class="key gap" aria-hidden="true"></span>keine Werte</li>' : ""}</ul>
    <ul class="hist-notes">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>
    ${quarterTableHTML(d)}
    <p class="hist-download">Als Tabelle für Excel, alle Steckdosen: <a href="api/energy/${d.day}/${d.day}" download>dieser Tag</a>
      · <a href="api/energy/${month}/${lastOfMonth(d.day)}" download>dieser Monat</a></p>`;
}

function energyModel(d) {  // the quarter hours by time, the stretches without values, until when
  const byT = new Map(d.points.map(p => [p.t, p]));
  const until = d.now != null ? Math.min(d.end, Math.floor(d.now / d.slot) * d.slot + d.slot) : d.end;
  const gaps = [];
  for (let t = d.start; t < until; t += d.slot) {
    if (byT.has(t)) continue;
    const last = gaps[gaps.length - 1];
    if (last && last[1] === t) last[1] = t + d.slot; else gaps.push([t, t + d.slot]);
  }
  if (d.now != null && gaps.length && gaps[gaps.length - 1][1] === until)
    gaps[gaps.length - 1][1] = Math.min(until, Math.max(gaps[gaps.length - 1][0], d.now));   // not past „jetzt“
  return { byT, gaps, until };
}

function drawEnergyChart() {  // one bar per quarter hour, as high as the energy that flowed in it
  const box = document.getElementById("hist-chart");
  const d = hist && hist.data && hist.data.day === hist.day ? hist.data : null;
  if (!box || !d || d.kind !== "plug") return;
  const m = energyModel(d), n = v => v.toFixed(1);
  const W = Math.max(280, Math.round(box.clientWidth || 600));
  const left = 58, right = 12, top = 22, plotH = W < 520 ? 170 : 220, base = top + plotH, axisY = base + 20, H = axisY + 6;
  const x0 = left, x1 = W - right, span = d.end - d.start;
  const X = t => x0 + (t - d.start) / span * (x1 - x0);
  const most = Math.max(0, ...d.points.map(p => p.wh));
  const nice = x => { const e = Math.pow(10, Math.floor(Math.log10(x))); return [1, 2, 5, 10].map(f => f * e).find(v => x <= v); };
  const step = nice(Math.max(most, 8) / 4), hi = Math.max(step, Math.ceil(most / step) * step);
  const Y = v => base - Math.min(v, hi) / hi * plotH;
  const parts = ['<defs><pattern id="hist-hatch" width="6" height="6" patternUnits="userSpaceOnUse" ' +
    'patternTransform="rotate(45)"><line x1="1" y1="0" x2="1" y2="6" class="gap-line"/></pattern></defs>'];
  for (const [a, b] of m.gaps)
    parts.push(`<rect class="gap" x="${n(X(a))}" y="${top}" width="${n(X(b) - X(a))}" height="${plotH}"/>`);
  for (let v = 0; v <= hi; v += step)
    parts.push(`<line class="grid" x1="${x0}" x2="${x1}" y1="${n(Y(v))}" y2="${n(Y(v))}"/>` +
      `<text class="ylab" x="${x0 - 8}" y="${n(Y(v) + 4)}">${esc(whText(v))}</text>`);
  for (const tick of d.ticks) {
    const x = X(tick.t), anchor = tick.t <= d.start ? "start" : tick.t >= d.end ? "end" : "middle";
    if (tick.t > d.start && tick.t < d.end) parts.push(`<line class="grid" x1="${n(x)}" x2="${n(x)}" y1="${top}" y2="${base}"/>`);
    parts.push(`<text class="xlab" x="${n(x)}" y="${axisY}" text-anchor="${anchor}">${esc(tick.label)}</text>`);
  }
  const gap = (X(d.start + d.slot) - X(d.start)) > 4 ? 1 : 0.5;   // a thin line of background between the bars
  for (const p of d.points) {
    if (!(p.wh > 0)) continue;
    const h = Math.max(1.5, base - Y(p.wh)), x = X(p.t) + gap / 2, w = Math.max(0.5, X(p.t + d.slot) - X(p.t) - gap);
    parts.push(`<rect class="qbar" x="${n(x)}" y="${n(base - h)}" width="${n(w)}" height="${n(h)}"/>`);
  }
  parts.push(`<line class="lane-base" x1="${x0}" x2="${x1}" y1="${base}" y2="${base}"/>`);
  if (d.now != null && d.now < d.end) {
    const x = X(d.now);
    parts.push(`<line class="now" x1="${n(x)}" x2="${n(x)}" y1="${top - 4}" y2="${base}"/>` +
      `<text class="now-lab" x="${n(x)}" y="${top - 8}" text-anchor="${x > x1 - 24 ? "end" : x < x0 + 24 ? "start" : "middle"}">jetzt</text>`);
  }
  parts.push(`<g class="cross" visibility="hidden"><line y1="${top}" y2="${base}"/></g>`);
  box.innerHTML = `<svg class="hist-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${
    esc(`Strom je Viertelstunde, ${d.label}`)}">${parts.join("")}</svg><div class="hist-tip" hidden></div>`;
  box._geo = { d, m, X, Y, x0, x1, top, W };
  box.onpointermove = box.onpointerdown = ev => {
    const t = histSlotAt(ev);
    if (t !== histHover) { histHover = t; showEnergyHover(); }
  };
  box.onpointerleave = ev => { if (ev.pointerType === "mouse") { histHover = null; showEnergyHover(); } };
  showEnergyHover();
}

function showEnergyHover() {  // a line through the quarter hour at histHover and what flowed in it
  const box = document.getElementById("hist-chart"), g = box && box._geo;
  if (!g) return;
  const cross = box.querySelector(".cross"), tip = box.querySelector(".hist-tip"), t = histHover;
  if (t == null || t < g.d.start || t >= g.m.until) { cross.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
  const d = g.d, p = g.m.byT.get(t), x = g.X(t + d.slot / 2), line = cross.querySelector("line");
  line.setAttribute("x1", x);
  line.setAttribute("x2", x);
  cross.setAttribute("visibility", "visible");
  const lines = [`<b>${esc(histClock(t, d))}–${esc(histClock(t + d.slot, d))} Uhr</b>`];
  if (!p) lines.push("keine Werte");
  else {
    lines.push(esc(whText(p.wh)));
    if (p.wh > 0 && (d.now == null || t + d.slot <= d.now))   // the quarter hour that is still running: no average yet
      lines.push(`im Schnitt ${esc(wattText(p.wh * 3600 / d.slot))}`);
    if (p.w_max != null) lines.push(`höchstens ${esc(wattText(p.w_max))}`);
  }
  tip.innerHTML = lines.map(l => `<span>${l}</span>`).join("");
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = `${Math.round(x + 14 + w <= g.W ? x + 14 : Math.max(0, x - 14 - w))}px`;
  tip.style.top = `${g.top}px`;
}

function quarterTableHTML(d) {  // the same day as a table: one line per hour with its four quarter hours
  const rows = new Map();
  for (const p of d.points) {
    const clock = histClock(p.t, d), hour = Number(clock.slice(0, 2)), q = Math.floor(Number(clock.slice(3, 5)) / 15);
    const r = rows.get(hour) || { hour, q: [null, null, null, null], sum: 0 };
    r.q[q] = (r.q[q] || 0) + p.wh;
    r.sum += p.wh;
    rows.set(hour, r);
  }
  const list = [...rows.values()].sort((a, b) => a.hour - b.hour);
  if (!list.length) return "";
  return `<details class="hist-table" ${hist && hist.table ? "open" : ""}><summary>Viertelstunde für Viertelstunde</summary><table>
    <thead><tr><th>Stunde</th><th>:00</th><th>:15</th><th>:30</th><th>:45</th><th>zusammen</th></tr></thead>
    <tbody>${list.map(r => `<tr><td>${r.hour} Uhr</td>${r.q.map(v => `<td>${v == null ? "–" : esc(whText(v, true))}</td>`).join("")}<td>${
      esc(whText(r.sum, true))}</td></tr>`).join("")}</tbody></table></details>`;
}

function histModel(d) {  // the day in five-minute steps: points by time, the stretches without values, until when
  const byT = new Map(d.points.map(p => [p.t, p]));
  const until = d.now != null ? Math.min(d.end, Math.floor(d.now / d.slot) * d.slot + d.slot) : d.end;
  const gaps = [];
  for (let t = d.start; t < until; t += d.slot) {
    const p = byT.get(t);
    if (p && (p.da || p.ist != null)) continue;   // the heaters answered, or a sensor measured
    const last = gaps[gaps.length - 1];
    if (last && last[1] === t) last[1] = t + d.slot; else gaps.push([t, t + d.slot]);
  }
  if (d.now != null && gaps.length && gaps[gaps.length - 1][1] === until && !byT.has(until - d.slot))
    gaps[gaps.length - 1][1] = Math.min(until, Math.max(gaps[gaps.length - 1][0], d.now));   // not past „jetzt“
  return { byT, gaps, until, hasWin: d.points.some(p => p.fenster > 0) };
}

function runs(points, slot, test) {  // neighbouring five minutes that pass test(), as lists
  const out = [];
  for (const p of points) {
    if (!test(p)) continue;
    const last = out[out.length - 1];
    if (last && last[last.length - 1].t + slot === p.t) last.push(p); else out.push([p]);
  }
  return out;
}

function drawHistoryChart() {
  const box = document.getElementById("hist-chart");
  const d = hist && hist.data && hist.data.day === hist.day ? hist.data : null;
  if (!box || !d) return;
  const m = histModel(d), n = v => v.toFixed(1);
  const W = Math.max(280, Math.round(box.clientWidth || 600));
  const left = 42, right = 12, top = 22, plotH = W < 520 ? 170 : 220, laneH = 22;
  const laneTop = top + plotH + 10, laneBase = laneTop + laneH, axisY = laneBase + 20, H = axisY + 6;
  const x0 = left, x1 = W - right, span = d.end - d.start;
  const X = t => x0 + (t - d.start) / span * (x1 - x0);
  // temperature scale around what the room measured; a setpoint far below (switched off) sits on the bottom edge
  const ist = d.points.map(p => p.ist).filter(v => v != null), soll = d.points.map(p => p.soll).filter(v => v != null);
  const base = ist.length ? ist : soll.length ? soll : [18, 22];
  let lo = Math.min(...base), hi = Math.max(...base);
  for (const v of soll) if (v >= lo - 3 && v <= hi + 3) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  lo = Math.floor(lo - 0.5);
  hi = Math.ceil(hi + 0.5);
  if (hi - lo < 4) { lo = Math.floor((lo + hi) / 2 - 2); hi = lo + 4; }
  const step = hi - lo <= 6 ? 1 : hi - lo <= 12 ? 2 : 5;
  const Y = v => top + (hi - Math.max(lo, Math.min(hi, v))) / (hi - lo) * plotH;
  const parts = ['<defs><pattern id="hist-hatch" width="6" height="6" patternUnits="userSpaceOnUse" ' +
    'patternTransform="rotate(45)"><line x1="1" y1="0" x2="1" y2="6" class="gap-line"/></pattern></defs>'];
  for (const [a, b] of m.gaps)   // no values: hatched over the curve and the heating strip
    parts.push(`<rect class="gap" x="${n(X(a))}" y="${top}" width="${n(X(b) - X(a))}" height="${laneBase - top}"/>`);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step)
    parts.push(`<line class="grid" x1="${x0}" x2="${x1}" y1="${n(Y(v))}" y2="${n(Y(v))}"/>` +
      `<text class="ylab" x="${x0 - 8}" y="${n(Y(v) + 4)}">${v}°</text>`);
  for (const tick of d.ticks) {
    const x = X(tick.t), anchor = tick.t <= d.start ? "start" : tick.t >= d.end ? "end" : "middle";
    if (tick.t > d.start && tick.t < d.end) parts.push(`<line class="grid" x1="${n(x)}" x2="${n(x)}" y1="${top}" y2="${laneBase}"/>`);
    parts.push(`<text class="xlab" x="${n(x)}" y="${axisY}" text-anchor="${anchor}">${esc(tick.label)}</text>`);
  }
  // heating strip: every five minutes as high as it heated in them; window open in blue behind it
  for (const r of runs(d.points, d.slot, p => p.fenster > 0))
    parts.push(`<rect class="win" x="${n(X(r[0].t))}" y="${laneTop}" width="${n(X(r[r.length - 1].t + d.slot) - X(r[0].t))}" height="${laneH}"/>`);
  for (const r of runs(d.points, d.slot, p => p.heizt > 0)) {
    let path = `M${n(X(r[0].t))} ${laneBase}`;
    for (const p of r) path += `V${n(laneBase - Math.max(2, laneH * Math.min(1, p.heizt / d.slot)))}H${n(X(p.t + d.slot))}`;
    parts.push(`<path class="heat" d="${path}V${laneBase}Z"/>`);
  }
  parts.push(`<line class="lane-base" x1="${x0}" x2="${x1}" y1="${laneBase}" y2="${laneBase}"/>` +
    `<text class="ylab" x="${x0 - 8}" y="${laneBase - 6}">heizt</text>`);
  // setpoint as steps (dashed) and room temperature as a line – both broken where values are missing
  let sollPath = "", istPath = "", before = null;
  for (const p of d.points) {
    const joined = before && before.t + d.slot === p.t;
    if (p.soll != null) sollPath += joined && before.soll != null ? `V${n(Y(p.soll))}H${n(X(p.t + d.slot))}`
      : `M${n(X(p.t))} ${n(Y(p.soll))}H${n(X(p.t + d.slot))}`;
    if (p.ist != null) istPath += `${joined && before.ist != null ? "L" : "M"}${n(X(p.t + d.slot / 2))} ${n(Y(p.ist))}`;
    before = p;
  }
  if (sollPath) parts.push(`<path class="soll" d="${sollPath}"/>`);
  if (istPath) parts.push(`<path class="ist" d="${istPath}"/>`);
  for (const p of d.points)   // a lone value between gaps would not show as a line
    if (p.ist != null && !m.byT.has(p.t - d.slot) && !m.byT.has(p.t + d.slot))
      parts.push(`<circle class="ist-dot" cx="${n(X(p.t + d.slot / 2))}" cy="${n(Y(p.ist))}" r="2.5"/>`);
  if (d.now != null && d.now < d.end) {
    const x = X(d.now);
    parts.push(`<line class="now" x1="${n(x)}" x2="${n(x)}" y1="${top - 4}" y2="${laneBase}"/>` +
      `<text class="now-lab" x="${n(x)}" y="${top - 8}" text-anchor="${x > x1 - 24 ? "end" : x < x0 + 24 ? "start" : "middle"}">jetzt</text>`);
  }
  parts.push(`<g class="cross" visibility="hidden"><line y1="${top}" y2="${laneBase}"/><circle r="5"/></g>`);
  box.innerHTML = `<svg class="hist-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${
    esc(`Temperatur im Raum und Heizzeiten, ${d.label}`)}">${parts.join("")}</svg><div class="hist-tip" hidden></div>`;
  box._geo = { d, m, X, Y, x0, x1, top, W };
  box.onpointermove = box.onpointerdown = ev => {
    const t = histSlotAt(ev);
    if (t !== histHover) { histHover = t; showHistHover(); }
  };
  box.onpointerleave = ev => { if (ev.pointerType === "mouse") { histHover = null; showHistHover(); } };
  showHistHover();
}

function histSlotAt(ev) {  // start of the five minutes under the pointer, null beside the chart or in the future
  const box = document.getElementById("hist-chart"), g = box && box._geo;
  if (!g) return null;
  const x = ev.clientX - box.getBoundingClientRect().left, d = g.d;
  if (x < g.x0 - 10 || x > g.x1 + 10) return null;
  const t = d.start + Math.floor(Math.max(0, Math.min(1, (x - g.x0) / (g.x1 - g.x0))) * (d.end - d.start - 1) / d.slot) * d.slot;
  return t < g.m.until ? t : null;
}

function showHistHover() {  // crosshair and the values of the five minutes at histHover
  const box = document.getElementById("hist-chart"), g = box && box._geo;
  if (!g) return;
  const cross = box.querySelector(".cross"), tip = box.querySelector(".hist-tip"), t = histHover;
  if (t == null || t < g.d.start || t >= g.m.until) { cross.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
  const d = g.d, p = g.m.byT.get(t), x = g.X(t + d.slot / 2), dot = cross.querySelector("circle");
  const line = cross.querySelector("line");
  line.setAttribute("x1", x);
  line.setAttribute("x2", x);
  dot.setAttribute("visibility", p && p.ist != null ? "visible" : "hidden");
  if (p && p.ist != null) { dot.setAttribute("cx", x); dot.setAttribute("cy", g.Y(p.ist)); }
  cross.setAttribute("visibility", "visible");
  const lines = [`<b>${esc(histClock(t, d))} Uhr</b>`];
  if (!p) lines.push("nicht aufgezeichnet");
  else {
    if (p.ist != null) lines.push(`im Raum ${deg(p.ist)}`);
    if (p.soll != null) lines.push(`eingestellt ${degShort(p.soll)}`);
    if (p.da === 0) lines.push("keine Verbindung");
    else if (p.da != null) lines.push(p.heizt >= 30 ? `heizt ${durText(p.heizt)}` : "heizt nicht");
    if (p.fenster >= 30) lines.push("Fenster offen");
  }
  tip.innerHTML = lines.map(l => `<span>${l}</span>`).join("");
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = `${Math.round(x + 14 + w <= g.W ? x + 14 : Math.max(0, x - 14 - w))}px`;
  tip.style.top = `${g.top}px`;
}

function hourTableHTML(d) {  // „Stunde für Stunde“: the same day as a table (also for screen readers)
  const rows = new Map();
  for (const p of d.points) {
    const hour = Number(histClock(p.t, d).slice(0, 2));
    const r = rows.get(hour) || { hour, ist: [], soll: null, heat: 0, win: 0 };
    if (p.ist != null) r.ist.push(p.ist);
    if (p.soll != null) r.soll = p.soll;
    r.heat += p.heizt || 0;
    r.win += p.fenster || 0;
    rows.set(hour, r);
  }
  const list = [...rows.values()].sort((a, b) => a.hour - b.hour);
  if (!list.length) return "";
  const anyWin = list.some(r => r.win >= 60);
  return `<details class="hist-table" ${hist && hist.table ? "open" : ""}><summary>Stunde für Stunde</summary><table>
    <thead><tr><th>Uhrzeit</th><th>im Raum</th><th>eingestellt</th><th>geheizt</th>${anyWin ? "<th>Fenster offen</th>" : ""}</tr></thead>
    <tbody>${list.map(r => `<tr><td>${r.hour} Uhr</td><td>${r.ist.length ? deg(avg(r.ist)) : "–"}</td><td>${
      r.soll == null ? "–" : degShort(r.soll)}</td><td>${durText(r.heat)}</td>${
      anyWin ? `<td>${r.win >= 60 ? durText(r.win) : "–"}</td>` : ""}</tr>`).join("")}</tbody></table></details>`;
}

// „Strom und Kosten“ at the end of „Strom“: one price for the household, the power of every AeroFlow, the switches
function costValues() {
  if (costForm) return costForm;
  const c = state.costs || {}, heaters = state.heaters.filter(h => h.kind !== "zigbee");
  return { price: c.price_ct == null ? "" : ctText(c.price_ct), show: !!c.show, show_power: !!c.show_power,
           powers: Object.fromEntries(heaters.map(h => [h.id, h.power_w == null ? "" : String(h.power_w)])) };
}

function wattText(w) { return Math.round(w).toLocaleString("de-DE") + " W"; }

function plugLineHTML(h) {   // the heater's plug: what it measures – and „Übernehmen“ for the power it measured while heating
  const p = h.plug;
  if (!p) return "";
  if (!p.found) return `<p class="hint plug-line">${esc(p.kind === "zigbee" ? `${p.name} ist nicht mehr im Zigbee-Netz.`
    : `${p.name}: ${p.error || "noch keine Antwort – die App fragt alle paar Sekunden."}`)}</p>`;
  const now = [p.state === "OFF" ? "ist aus" : p.state === "ON" ? (p.power != null ? `jetzt ${wattText(p.power)}` : "ist an") : "",
               p.energy_today != null ? `heute ${kwhText(p.energy_today)}` : ""].filter(Boolean).join(" · ");
  const measured = p.measured_w != null
    ? `Beim Heizen gemessen: etwa ${wattText(p.measured_w)} <button class="small" data-action="cpower-take" data-heater="${
        esc(h.id)}" data-watts="${p.measured_w}">Übernehmen</button>`
    : "Die Leistung misst die App, sobald die Heizung heizt.";
  return `<p class="hint plug-line">${esc(p.name)}${now ? " " + esc(now) : ""}. ${measured}${p.error
    ? ` <span class="error-text">${esc(p.error)}</span>` : ""}</p>`;
}
function editCosts() { if (!costForm) costForm = clone(costValues()); return costForm; }

function costsCardHTML() {
  const heaters = state.heaters.filter(h => h.kind !== "zigbee"), plugs = state.plugs || [];
  const f = costValues(), saving = busy.has("costs");
  const powers = heaters.map(h => `<div class="power-item"><label class="power-row"><span class="pname">${deviceIcon(h)}${esc(h.name)}</span>
      <span class="unit-input"><input data-cpower="${esc(h.id)}" inputmode="numeric" list="power-values" autocomplete="off"
        value="${esc(f.powers[h.id] ?? "")}" placeholder="z. B. 1300" aria-label="Leistung von ${esc(h.name)} in Watt"><span>W</span></span></label>
      ${plugLineHTML(h)}</div>`).join("");
  const anyPlug = heaters.some(h => h.plug);
  return `<article class="card costs-card">
    <div class="card-head"><h2>Strom und Kosten</h2></div>
    <p class="hint">Ein Strompreis für den ganzen Haushalt – damit rechnet die App den Strom in Euro um.${heaters.length
      ? " Die Heizungen melden keinen Verbrauch. Die App schätzt ihn: Heizdauer × Leistung × Strompreis." : ""}</p>
    <div class="costs-form">
      <label>Strompreis<span class="unit-input"><input data-cfield="price" inputmode="decimal" autocomplete="off"
        value="${esc(f.price)}" placeholder="z. B. 32,5"><span>ct/kWh</span></span></label>
      ${heaters.length ? `<fieldset><legend>Leistung der Heizungen</legend>${powers}
        <p class="hint">Steht auf dem Typenschild, bei der FlexiSmart Pro je nach Größe 650 bis 2450 W. ${anyPlug
          ? `Hängt eine Heizung an einer Steckdose mit Strommessung, misst die App beim Heizen nach – „Übernehmen“
          trägt den Wert ein. Welche Steckdose zu welcher Heizung gehört, wählst du unter „Geräte“ › „Steckdosen“.`
          : `Genauer wird es, wenn du einmal einen Tag lang mit einem Zwischenstecker mit Strommessung nachmisst – oder
          die Heizung an eine Steckdose mit Strommessung hängst: unter „Geräte“ mit „+ Gerät hinzufügen“.`}</p></fieldset>
      <datalist id="power-values">${POWER_VALUES.map(w => `<option value="${w}">`).join("")}</datalist>
      <label class="check"><input type="checkbox" data-cfield="show" ${f.show ? "checked" : ""}>
        <span>Strom und Kosten der Heizungen auch auf der Startseite zeigen</span></label>` : ""}
      ${plugs.length ? `<label class="check"><input type="checkbox" data-cfield="show_power" ${f.show_power ? "checked" : ""}>
        <span>Strom der Steckdosen auch auf der Startseite zeigen<small>„Strom heute“ unter den Räumen, an jedem Raum
          Watt und kWh seiner Steckdosen und seit wann Waschmaschine oder Trockner laufen. An und aus und „fertig“ stehen
          immer da.</small></span></label>` : ""}
      <div class="row">
        <button class="primary" data-action="costs-save" ${saving ? "disabled" : ""}>${saving ? "speichere …" : "Speichern"}</button>
        ${costForm ? '<button data-action="costs-cancel">Verwerfen</button>' : ""}
      </div>
    </div>
  </article>`;
}

async function saveCosts() {
  const f = costValues();
  busy.add("costs");
  render();
  try {
    const res = await api("PUT", "api/costs", { price_ct: f.price, show: f.show, show_power: f.show_power, powers: f.powers });
    costForm = null;
    toast(res.message || "Gespeichert.", !!res.warning);
  } catch (e) { toast(e.message, true); }
  busy.delete("costs");
  await refresh();
}

// ------------------------------------------------------------------ „Strom“: what every plug measured (setup tab)
// A day, a week (Monday to Sunday) or a month: per device kWh and „etwa … €“, the biggest first – a tap opens its
// quarter hours; a week and a month with the one before. Below: the standby of every device and „Strom und Kosten“.
const APPLIANCES = [["heizung", "Heizung"], ["waschmaschine", "Waschmaschine"], ["trockner", "Trockner"],
                    ["kuehlschrank", "Kühlschrank"], ["gefriergeraet", "Gefriergerät"], ["klimaanlage", "Klimaanlage"],
                    ["sonstiges", "Sonstiges"]];   // „Angeschlossen“ at a plug – like Service.APPLIANCES
const NO_STANDBY = ["heizung", "kuehlschrank", "gefriergeraet"];   // their lowest power is no standby (Service.NO_STANDBY)
const POWER_SPANS = [["day", "Tag", "Tag davor", "Tag danach"], ["week", "Woche", "Woche davor", "Woche danach"],
                     ["month", "Monat", "Monat davor", "Monat danach"]];
let powerSpan = ["week", "month"].includes(localStorage.getItem("heizung.powerSpan")) ? localStorage.getItem("heizung.powerSpan") : "day";
let power = null;   // the period shown: {span, day (a day in it), data, loading, error, at (Date.now() when asked)}

function applianceText(id) { return (APPLIANCES.find(a => a[0] === id) || APPLIANCES[APPLIANCES.length - 1])[1]; }

async function loadPower(day) {   // the period with `day` in it – without one, the period shown again (or today's)
  if (!state) return;
  const same = power && power.span === powerSpan ? power : null;
  const p = power = { span: powerSpan, day: day || (same ? same.day : state.now.slice(0, 10)),
                      data: same && !day ? same.data : null, loading: true, error: "", at: Date.now() };
  render();
  try {
    const data = await api("GET", `api/power/${p.span}/${p.day}`);
    if (power !== p) return;   // another period chosen meanwhile
    p.data = data;
  } catch (e) {
    if (power !== p) return;
    p.error = e.message;
  }
  p.loading = false;
  render();
}

function powerAgain() {   // a period still running comes again every minute while it is shown
  const p = power;
  if (p && !p.loading && p.data && p.data.running && view === "setup" && tab === "power" && !document.hidden
      && Date.now() - p.at > 60000) loadPower();
}

function powerKwh(wh) { return wh > 0 && wh < 5 ? "unter 0,01 kWh" : kwhText(wh / 1000); }   // not „0,00 kWh“
function powerEur(eur, wh) {   // „ · etwa 1,30 €“ – a few Wh: „ · unter 1 Cent“, nothing: nothing
  return eur == null ? "" : eur >= 0.01 ? ` · etwa ${euroText(eur)}` : wh > 0 ? " · unter 1 Cent" : "";
}

function standbyText(w) { return (w < 10 && w % 1 ? w.toFixed(1).replace(".", ",") : String(Math.round(w))) + " W"; }

function powerCutText(t, d) {   // „Donnerstag, 19.11., 14:00 Uhr“ in the app's time zone
  const opt = { weekday: "long", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" };
  try { return new Date(t * 1000).toLocaleString("de-DE", d.tz ? Object.assign({ timeZone: d.tz }, opt) : opt) + " Uhr"; }
  catch (_) { return new Date(t * 1000).toLocaleString("de-DE", opt) + " Uhr"; }
}

function compareHTML(wh, before, d) {   // „Woche davor 3,8 kWh · 11 % mehr“
  if (!d.before || wh == null || before == null) return "";
  const pct = before > 0 ? Math.round((wh - before) / before * 100) : null;
  const change = pct == null ? "" : pct === 0 ? " · gleich viel" : ` · ${Math.abs(pct)} % ${pct > 0 ? "mehr" : "weniger"}`;
  return `<span class="power-before">${esc(d.before.label)} ${esc(powerKwh(before))}${esc(change)}</span>`;
}

function powerRowHTML(x, d, max) {   // one device: name, what it is and where, kWh and €, a bar for the amount
  const kind = applianceText(x.appliance);   // a plug called like what hangs on it: once is enough
  const where = [kind !== x.name ? kind : "", x.heater || x.room, x.known ? "" : "nicht mehr in der App"].filter(Boolean).join(" · ");
  const value = x.wh == null ? '<span class="muted">keine Werte</span>' : `<b>${esc(powerKwh(x.wh))}</b><span class="muted">${
    esc(powerEur(x.eur, x.wh))}</span>`;
  const share = x.wh && max ? Math.max(1, Math.round(x.wh / max * 100)) : 0;
  const inner = `<span class="power-name"><b>${esc(x.name)}</b><span class="muted">${esc(where)}</span></span>
    <span class="power-value">${value}${compareHTML(x.wh, x.before_wh, d)}</span>
    <span class="pbar" aria-hidden="true"><span style="width:${share}%"></span></span>`;
  return `<li>${x.known ? `<button class="power-row" data-action="power-plug" data-plug="${esc(x.id)}"
    aria-label="${esc(x.name)}: Viertelstunden ansehen">${inner}</button>` : `<div class="power-row">${inner}</div>`}</li>`;
}

function powerCardHTML() {
  const p = power, d = p && p.data, span = POWER_SPANS.find(s => s[0] === powerSpan);
  const step = (to, label, sym) => `<button class="day-step" data-action="power-day" data-day="${to || ""}" ${
    to && !(p && p.loading) ? "" : "disabled"} aria-label="${label}">${sym}</button>`;
  const head = `<div class="hist-day power-nav">${step(d && d.prev, span[2], "‹")}<span class="day-label">${
    esc(d ? d.label : "…")}${d && d.dates && d.dates !== d.label ? `<small>${esc(d.dates)}</small>` : ""}</span>${
    step(d && d.next, span[3], "›")}</div>`;
  if (!d) return `<article class="card power-card">${head}<p class="hint">${esc((p && p.error) || "Wird geladen …")}</p></article>`;
  const max = Math.max(0, ...d.devices.map(x => x.wh || 0)), t = d.total;
  const notes = [];
  if (t.wh != null) notes.push("Gezählt ist nur, was durch die Steckdosen fließt – nicht der ganze Haushalt. Antippen zeigt die Viertelstunden.");
  if (t.wh != null && d.price_ct == null) notes.push("Ohne Strompreis keine Kosten. Eintragen unten bei „Strom und Kosten“.");
  if (d.before && d.running && d.before.cut != null)
    notes.push(`Verglichen wird mit der gleichen Zeit davor – bis ${powerCutText(d.before.cut, d)}.`);
  if (d.before && d.devices.some(x => x.wh != null && x.before_wh == null))
    notes.push("Einen Vergleich gibt es, sobald eine Steckdose beide Male fast durchgehend gemessen hat.");
  if (t.wh == null) notes.push(`In dieser Zeit hat die App ${d.running ? "noch " : ""}nichts gemessen.`);
  return `<article class="card power-card">${head}
    <ul class="power-list">${d.devices.map(x => powerRowHTML(x, d, max)).join("")}</ul>
    ${t.wh == null ? "" : `<div class="power-sum"><span>Zusammen</span><span class="power-value"><b>${esc(powerKwh(t.wh))}</b><span
      class="muted">${esc(powerEur(t.eur, t.wh))}</span>${compareHTML(t.wh, t.before_wh, d)}</span></div>`}
    ${notes.map(n => `<p class="hint">${esc(n)}</p>`).join("")}
  </article>`;
}

function standbyCardHTML() {   // „Bereitschaft“: what a device draws while it does nothing – and what that costs a year
  const d = power && power.data;
  const rows = d ? d.devices.filter(x => x.known && !NO_STANDBY.includes(x.appliance)).map(x => {
    const text = x.standby_w == null ? '<span class="muted">noch zu wenig Werte</span>'
      : x.standby_w === 0 ? "0 W <span class=\"muted\">– ganz aus, wenn es nichts tut</span>"
      : `<b>${esc(standbyText(x.standby_w))}</b> rund um die Uhr<span class="muted"> ≈ ${esc(kwhText(x.standby_w * 8.76))} im Jahr${
        x.standby_eur_year != null ? ` ≈ ${esc(euroText(x.standby_eur_year))}` : ""}</span>`;
    const kind = applianceText(x.appliance);
    return `<li><span class="power-name"><b>${esc(x.name)}</b><span class="muted">${kind !== x.name ? esc(kind) : ""}</span></span>
      <span class="power-value">${text}</span></li>`;
  }).join("") : "";
  if (!rows) return "";
  return `<article class="card"><div class="card-head"><h2>Bereitschaft</h2></div>
    <p class="hint">Was ein Gerät auch dann zieht, wenn es nichts tut: die niedrigsten Werte der letzten ${d.standby_days} Tage.
      Erst nach einem Tag Messung. Heizung, Kühlschrank und Gefriergerät fehlen hier – sie haben keine Bereitschaft.</p>
    <ul class="standby-list">${rows}</ul></article>`;
}

const RUN_WORDS = { waschmaschine: ["Wäsche", "Wäschen", "keine Wäsche"],
                    trockner: ["Trockengang", "Trockengänge", "kein Trockengang"] };

function runCount(n, kind) {   // „1 Wäsche“, „5 Wäschen“, „keine Wäsche“
  const w = RUN_WORDS[kind] || ["Durchgang", "Durchgänge", "kein Durchgang"];
  return n ? `${n} ${n === 1 ? w[0] : w[1]}` : w[2];
}

function runWhen(r, d) {   // „Do., 26.11., 10:12–11:58 Uhr“ – within a day only the times
  const clock = `${histClock(r.start, d)}–${histClock(r.end, d)} Uhr`, opt = { weekday: "short", day: "2-digit", month: "2-digit" };
  if (d.span === "day") return clock;
  try { return new Date(r.start * 1000).toLocaleDateString("de-DE", d.tz ? Object.assign({ timeZone: d.tz }, opt) : opt) + ", " + clock; }
  catch (_) { return new Date(r.start * 1000).toLocaleDateString("de-DE", opt) + ", " + clock; }
}

function runsCardHTML(x, d) {   // a washing machine or a dryer: its runs in the period – how many, what they took, each one
  const r = x.runs;
  if (!r || (!x.known && !r.count)) return "";
  const words = RUN_WORDS[x.appliance] || ["Durchgang", "Durchgänge", "kein Durchgang"];
  const now = r.running ? `<p class="run-now">Läuft gerade – seit ${esc(r.running.since.slice(11, 16))} Uhr, bisher ${
    esc(powerKwh(r.running.wh))}.</p>` : "";
  const sum = r.count ? `${esc(d.label)}: <b>${esc(runCount(r.count, x.appliance))}</b> · ${esc(powerKwh(r.wh))}${
    esc(powerEur(r.eur, r.wh))}${r.count > 1 ? ` · im Schnitt ${esc(powerKwh(r.avg_wh))}` : ""}`
    : `${esc(d.label)}: ${esc(runCount(0, x.appliance))}${d.running ? " bisher" : ""}.`;
  const before = r.before ? `<p class="hint">${esc(d.before.label)}: ${esc(runCount(r.before.count, x.appliance))}${
    r.before.count > 1 ? ` · im Schnitt ${esc(powerKwh(r.before.avg_wh))}` : r.before.count ? ` · ${esc(powerKwh(r.before.avg_wh))}` : ""}</p>` : "";
  const items = r.items.map(i => `<li><button class="power-row" data-action="run-open" data-plug="${esc(x.id)}"
    data-start="${i.start}" aria-label="${esc(runWhen(i, d))}: Verlauf ansehen"><span class="power-name"><b>${esc(runWhen(i, d))}</b>
    <span class="muted">${esc(durText(i.end - i.start))}${i.heat_min ? ` · Heizzeit ${i.heat_min} Min.` : ""}</span></span>
    <span class="power-value"><b>${esc(powerKwh(i.wh))}</b><span class="muted">${esc(powerEur(i.eur, i.wh))}</span></span></button></li>`).join("");
  const rule = x.appliance === "trockner" ? "nach 3 Minuten ohne Dauerlast – die kurzen Stöße des Knitterschutzes zählen nicht"
    : "nach 5 Minuten fast ohne Strom";
  return `<article class="card"><div class="card-head"><h2>${esc(words[1])} – ${esc(x.name)}</h2></div>
    ${now}<p class="run-sum">${sum}</p>${before}
    ${items ? `<ul class="power-list">${items}</ul>` : ""}
    <p class="hint">Die App erkennt das an der Leistung: Es läuft, sobald das Gerät eine Minute lang mehr als 15 W zieht,
      und ist fertig ${esc(rule)}. Auf der Startseite steht dann „fertig“, bis jemand OK tippt. Antippen zeigt die Leistung
      Minute für Minute.</p>
  </article>`;
}

function renderPower(el) {
  el.className = "tab single";
  const plugs = state.plugs || [], d = power && power.data;
  const spans = `<div class="subtabs" role="tablist" aria-label="Zeitraum">${POWER_SPANS.map(([id, label]) =>
    `<button data-powerspan="${id}" role="tab" aria-selected="${powerSpan === id}">${label}</button>`).join("")}</div>`;
  const none = `<article class="card"><div class="card-head"><h2>Strom der Geräte</h2></div><p class="hint">Noch keine
    Steckdose mit Strommessung in der App. Unter „Geräte“ kommt eine mit „+ Gerät hinzufügen“ dazu – dann zeigt die App
    hier, was jedes Gerät braucht und kostet.</p></article>`;
  const runs = d ? d.devices.map(x => runsCardHTML(x, d)).join("") : "";   // washing machines and dryers
  setHTML(el, (plugs.length || (d && d.devices.length) ? spans + powerCardHTML() + runs + standbyCardHTML() : none)
    + costsCardHTML());
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
    ${d.html || ""}
    ${d.fields.map(f => f.type === "text"   // a name – else a PIN
      ? `<label>${esc(f.label)}<input data-dialog-field="${esc(f.id)}" type="text" value="${esc(f.value || "")}"
        placeholder="${esc(f.placeholder || "")}" maxlength="${f.maxlength || 40}" autocomplete="off" ${d.busy ? "disabled" : ""}></label>`
      : `<label>${esc(f.label)}<input data-dialog-field="${esc(f.id)}" type="password" inputmode="numeric"
      pattern="[0-9]*" maxlength="8" autocomplete="off" ${d.busy ? "disabled" : ""}></label>`).join("")}
    <p class="dialog-error" ${d.error ? "" : "hidden"}>${esc(d.error)}</p>
    <div class="dialog-buttons">${d.buttons.map((b, i) =>
      `<button data-dialog-button="${i}" class="${b.cls || ""}" ${d.busy ? "disabled" : ""}>${esc(b.label)}</button>`).join("")}</div>
  </div>`;
  linkAddresses(el);
  el.hidden = false;
  const first = el.querySelector("input");
  if (first) { first.focus(); if (first.type === "text") first.select(); }
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
  for (const f of d.fields) if (f.type === "text") f.value = values[f.id];   // drawn again (busy, error): what was typed stays
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
  if ((editing || roomForm || deviceForm || calibForm || sensorForm || pillForm || (wifiForm && !wifiForm.job)) && !auto) {
    const yes = await ask({ title: "Einrichtung schließen?", text: "Nicht gespeicherte Änderungen gehen dabei verloren.",
      buttons: [{ label: "Weiter bearbeiten", value: null }, { label: "Schließen", value: "yes", cls: "primary" }] });
    if (!yes) return;
  }
  closeRoom();
  editing = roomForm = deviceForm = calibForm = sensorForm = pillForm = wifiForm = null;
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
    if (editing || roomForm || deviceForm || calibForm || sensorForm || pillForm || wifiForm || dialog) return touchSetup();
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

function renderRooms(el) {   // the list of rooms – a tap opens a room with everything in it (renderRoomView)
  if (roomView != null && roomView !== "none" && !state.rooms.some(r => r.id === roomView)) closeRoom();   // deleted
  if (roomView != null) return renderRoomView(el);
  el.className = "tab";
  const adding = roomForm && roomForm.mode === "new";
  const head = `<div class="tab-tools"><button class="small primary" data-action="room-new" ${adding ? "disabled" : ""}>+ Raum</button></div>`
    + (adding ? `<article class="card"><div class="card-head"><h2>Neuer Raum</h2></div>${roomFormHTML(roomForm)}</article>` : "");
  const loose = devicesIn("none");
  const items = state.rooms.map(r => roomItemHTML(r.id)).join("")
    + (Object.values(loose).some(list => list.length) ? roomItemHTML("none") : "");
  setHTML(el, head + items + (state.rooms.length || adding ? "" :
    '<p class="empty">Tipp: Über „+ Raum“ einen Raum anlegen. Im Raum stehen dann seine Heizungen, Fühler, Steckdosen ' +
    'und das Licht beisammen – mehrere Heizungen lassen sich gemeinsam steuern und bekommen einen Heizplan.</p>'));
}

function devicesIn(id) {   // every device of a room – id "none": the ones without a room (a plug at a heater: the heater's)
  const inRoom = x => (id === "none" ? !x.room : x.room === id);
  return { heaters: state.heaters.filter(inRoom), sensors: (state.sensors || []).filter(inRoom),
           plugs: (state.plugs || []).filter(inRoom), lights: (state.lights || []).filter(inRoom),
           buttons: (state.buttons || []).filter(inRoom), detectors: (state.detectors || []).filter(inRoom) };
}

function roomChipParts(d) {   // [text, tone] for a room in the list: what it has – then what needs a look
  const count = (n, one, many) => (n === 1 ? one : `${n} ${many}`);
  const aeroflows = d.heaters.filter(h => h.kind !== "zigbee").length, trvs = d.heaters.length - aeroflows;
  const parts = [aeroflows && [count(aeroflows, "1 Heizung", "Heizungen")], trvs && [count(trvs, "1 Thermostat", "Thermostate")],
    d.sensors.length && [count(d.sensors.length, "Fühler", "Fühler")],
    d.plugs.length && [count(d.plugs.length, "1 Steckdose", "Steckdosen")],
    d.lights.length && [`Licht · ${count(d.lights.length, "1 Lampe", "Lampen")}`],
    d.buttons.length && [count(d.buttons.length, "1 Schalter", "Schalter")],
    d.detectors.length && [count(d.detectors.length, "Bewegungsmelder", "Bewegungsmelder")]].filter(Boolean);
  const reached = [...d.heaters.map(h => !h.online), ...d.sensors.map(s => s.online === false),   // switches sleep
    ...d.plugs.map(p => !p.found || !!p.error), ...d.lights.map(l => !!l.lost), ...d.detectors.map(x => !!x.silent)];
  const lost = reached.filter(Boolean).length;
  if (lost) parts.push([lost === reached.length ? "keine Verbindung" : `${lost} ${lost === 1 ? "Gerät" : "Geräte"} ohne Verbindung`, "bad"]);
  if (d.heaters.some(h => h.battery_low) || [...d.buttons, ...d.detectors].some(x => x.battery != null && x.battery <= 15)) {
    parts.push(["Batterie bald wechseln", "warn"]);
  }
  return parts.length ? parts : [["noch keine Geräte"]];
}

function roomItemHTML(id) {   // a room in the list: its name and temperature, then small labels
  const none = id === "none", r = none ? null : state.rooms.find(x => x.id === id);
  const g = none ? null : groups().find(x => x.kind === "room" && x.id === id);
  const temp = g ? roomTemp(g) : null, color = roomColorOf(g);
  return `<button class="room-item${none ? " none" : ""}${color ? " colored" : ""}" data-action="room-open" data-room="${
    esc(id)}"${color ? ` style="--room:${esc(color)}"` : ""}>
    <span class="room-item-head"><span class="room-item-name">${none ? "Ohne Raum" : esc(r.name)}</span>${temp == null ? ""
      : `<span class="room-item-temp">${degBig(temp)}</span>`}<span class="go" aria-hidden="true">›</span></span>
    <span class="room-chips">${roomChipParts(devicesIn(id)).map(([text, tone]) => `<span${tone ? ` class="${tone}"` : ""}>${
      esc(text)}</span>`).join("")}</span></button>`;
}

function roomFromHash() {   // #raum=<id> in the address
  const m = /^#raum=(.+)$/.exec(location.hash || "");
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch (e) { return null; }
}

function openRoom(id) {   // a room on its own page – an entry in the history, so the phone's back button leads to the list
  if (roomForm && roomForm.mode === "edit") roomForm = null;
  roomView = id;
  const hash = "#raum=" + encodeURIComponent(id);
  if (location.hash !== hash) history.pushState({ raum: id }, "", hash);
  if (state && state.app.zigbee) loadZigbee();   // the signal of its Zigbee devices – and adding new ones
  invalidate();
  render();
  window.scrollTo(0, 0);
}

function leaveRoomState() {   // what belongs to the room ends with it: its „+ Gerät hinzufügen“, an unsaved „Bearbeiten“
  if (addFlow && addFlow.home != null && addFlow.home === roomView) { addReset(); addFlow = null; }
  if (roomForm && roomForm.mode === "edit") roomForm = null;
}

function closeRoom() {   // back to the list of rooms – the entry openRoom made goes from the history again
  if (roomView == null) return;
  leaveRoomState();
  roomView = null;
  if (history.state && history.state.raum) history.back();   // popstate then finds no room in the address
  else if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  invalidate();
}

function goToRoom(id) {   // from "Geräte" into a room
  tab = "rooms";
  localStorage.setItem("heizung.tab", tab);
  openRoom(id);
}

function toggleRoomSection(id) {
  roomSections[id] = roomSections[id] === false;
  localStorage.setItem("heizung.roomSections", JSON.stringify(roomSections));
  render();
}

function roomSectionHTML(id, title, count, body, extra = "") {   // a part of the room view – its title opens and closes it
  const open = roomSections[id] !== false;
  return `<section class="room-sec${open ? "" : " closed"}"><h2 class="sec-head"><button class="sec-toggle"
    data-action="room-sec" data-sec="${id}" aria-expanded="${open}"><span class="sec-title">${title}</span><span
    class="count">${count}</span>${extra}${CHEVRON}</button></h2>${open ? body() : ""}</section>`;
}

function renderRoomView(el) {   // one room with everything in it – or „Ohne Raum“: the devices that have none yet
  el.className = "tab single";
  if (addFlow && addFlow.home === roomView) return setHTML(el, addFlowHTML(roomOptionsHTML));
  const none = roomView === "none", r = none ? null : state.rooms.find(x => x.id === roomView);
  const g = none ? null : groups().find(x => x.kind === "room" && x.id === r.id);
  const d = devicesIn(roomView), heaters = none ? d.heaters : g.heaters;
  const bar = `<div class="room-bar"><button class="link-btn" data-action="room-back">‹ Alle Räume</button>${none ? ""
    : `<button class="primary" data-action="add-open" data-home="${esc(r.id)}">+ Gerät hinzufügen</button>`}</div>`;
  const title = !none && roomForm && roomForm.mode === "edit" && roomForm.id === r.id
    ? `<article class="card"><div class="card-head"><h2>${esc(r.name)}</h2><span class="tag">bearbeiten</span></div>${
      roomFormHTML(roomForm)}</article>`
    : `<div class="room-title">${!none && r.color ? `<span class="room-dot" style="--room:${esc(r.color)}"
      aria-hidden="true"></span>` : ""}<h2>${none ? "Ohne Raum" : esc(r.name)}</h2>${none ? ""
      : `<button class="small" data-action="room-edit" data-room="${esc(r.id)}">Bearbeiten</button>`}</div>${none
      ? '<p class="hint room-hint">Diese Geräte haben noch keinen Raum. Wähle bei jedem seinen Raum – dann steht es dort.</p>' : ""}`;
  const card = html => `<article class="card">${html}</article>`;
  const secs = [];
  if (heaters.length) {   // the heating: for a room one card – without a room one for each heater – then their cards
    secs.push(roomSectionHTML("heat", "Heizen", heaters.length, () => (none
      ? heaters.map(h => roomCard({ kind: "heater", id: h.id, name: h.name, heaters: [h] })).join("") : roomCard(g))
      + card(heaters.map(h => heaterTileHTML(h, roomOptionsHTML)).join("")), none ? "" : heatStateHTML(g)));
  }
  if (d.sensors.length) {
    secs.push(roomSectionHTML("sensors", "Raumfühler", d.sensors.length,
      () => card(d.sensors.map(s => sensorTileHTML(s, roomOptionsHTML)).join(""))));
  }
  if (d.plugs.length) secs.push(roomSectionHTML("plugs", "Steckdosen", d.plugs.length, () => card(d.plugs.map(plugTileHTML).join(""))));
  const light = d.lights.length + d.buttons.length + d.detectors.length;
  if (light) secs.push(roomSectionHTML("light", "Licht", light, () => roomLightHTML(r, d)));
  const empty = secs.length ? "" : `<p class="empty">${none ? "Alle Geräte haben einen Raum."
    : "Noch kein Gerät in diesem Raum. Ein neues kommt über „+ Gerät hinzufügen“ dazu. Eins, das schon in der App ist, " +
      "holst du unter „Geräte“ hierher: bei ihm diesen Raum wählen – eine Heizung auch hier über „Bearbeiten“."}</p>`;
  setHTML(el, bar + title + secs.join("") + empty);
}

function roomLightHTML(r, d) {   // the lamps, switches and motion sensors of a room – and its „Licht bei Bewegung“
  const parts = [["Lampen", d.lights.map(lightTileHTML)], ["Schalter", d.buttons.map(buttonTileHTML)],
                 ["Bewegungsmelder", d.detectors.map(detectorTileHTML)]].filter(([, items]) => items.length);
  const motion = r && d.detectors.length ? motionRoomHTML(r) : "";
  const titles = parts.length > 1 || !!motion;
  return `<article class="card">${parts.map(([title, items]) => (titles ? `<h3 class="sec-sub">${title}</h3>` : "")
    + items.join("")).join("")}${motion}</article>`;
}

function heatStateHTML(g) {   // „heizt“, „heizt nicht“ or „offline“
  const online = g.heaters.filter(h => h.online), heating = online.some(h => h.heating);
  return `<span class="state ${heating ? "heating" : ""}">${online.length ? (heating ? "heizt" : "heizt nicht") : "offline"}</span>`;
}

function roomOptionsHTML(sel) {   // the rooms to choose at a heater or a sensor
  return ['<option value="">– kein Raum –</option>'].concat(state.rooms.map(r =>
    `<option value="${esc(r.id)}" ${r.id === sel ? "selected" : ""}>${esc(r.name)}</option>`)).join("");
}

// the colours that mark a room (room list, room view, its tile on the start page) – any other with „Eigene Farbe …“
const ROOM_COLORS = [["#d93f3f", "Rot"], ["#f07c6c", "Koralle"], ["#ef7d22", "Orange"], ["#f2a93b", "Bernstein"],
  ["#e8c547", "Gelb"], ["#a8c94a", "Limette"], ["#4caf50", "Grün"], ["#2e7d4f", "Waldgrün"], ["#5cc8a8", "Mint"],
  ["#1fa5a5", "Türkis"], ["#1d6f7a", "Petrol"], ["#4aa8e0", "Himmelblau"], ["#3b6fd6", "Blau"], ["#2c3e8c", "Marineblau"],
  ["#5b4fc9", "Indigo"], ["#8a4fd1", "Violett"], ["#b07cd8", "Lila"], ["#c2378f", "Magenta"], ["#ec6aa8", "Pink"],
  ["#f3a5b6", "Rosa"], ["#8d5a3b", "Braun"], ["#c9a77c", "Sand"], ["#8a8f94", "Grau"], ["#4f5b66", "Schiefer"]];

function roomColorOf(g) {   // "#rrggbb" or "" – a heater without room has none
  return (g && g.kind === "room" && g.room && g.room.color) || "";
}

function roomColorsHTML(f) {   // in the room form: no colour, one of ROOM_COLORS – or any other
  const own = !!f.color && !ROOM_COLORS.some(([c]) => c === f.color);
  const swatch = (color, name) => `<button type="button" class="room-swatch${color ? "" : " none"}${f.color === color
    ? " chosen" : ""}" data-action="room-color" data-color="${color}"${color ? ` style="--room:${color}"` : ""}
    aria-pressed="${f.color === color}" aria-label="${name}" title="${name}"></button>`;
  return `<fieldset class="room-colors"><legend>Farbe – an ihr erkennt man den Raum</legend>
    <div class="room-swatches">${swatch("", "Keine Farbe")}${ROOM_COLORS.map(([c, name]) => swatch(c, name)).join("")}</div>
    <label class="room-own${own ? " chosen" : ""}"><input type="color" data-field="color" value="${esc(own ? f.color
      : "#3b6fd6")}" aria-label="Eigene Farbe"><span>Eigene Farbe …${own ? ' <span class="muted">(gewählt)</span>' : ""}</span></label>
    <p class="hint">Die Farbe steht in der Raumliste, oben im Raum und als Streifen an seiner Kachel auf der Startseite.</p>
  </fieldset>`;
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
    ${roomColorsHTML(f)}
    <fieldset><legend>Heizungen in diesem Raum</legend>${heaters || '<span class="muted">Noch keine Heizungen angelegt.</span>'}</fieldset>
    <div class="row">
      <button class="primary" data-action="room-save" ${saving ? "disabled" : ""}>${saving ? "speichere …" : f.mode === "new" ? "Anlegen" : "Speichern"}</button>
      <button data-action="room-cancel">Abbrechen</button>
      ${f.mode === "edit" ? '<button class="danger" data-action="room-delete">Raum löschen</button>' : ""}
    </div>
  </div>`;
}

function roomCard(g) {   // the heating of a room – or of a heater without room – in the room view: temperature, colder
  // and warmer, the mode, the Heizplan, the list of its devices and the Verlauf
  const key = keyOf(g);
  const online = g.heaters.filter(h => h.online);
  const writable = online.some(h => h.writable);
  const p = pending[key];
  const target = p ? p.temp : currentTarget(g);
  const mode = groupMode(g);
  const boosting = online.find(h => h.mode === "boost" && h.boost_remaining);
  const dis = writable ? "" : "disabled";
  const buttons = MODE_BUTTONS.map(([m, label]) => {
    const text = m === "boost" && boosting ? `Boost ${boosting.boost_remaining}` : label;
    return `<button data-action="mode" data-mode="${m}" class="${mode === m ? "active" : ""}" ${dis}>${esc(text)}</button>`;
  }).join("");
  return `<article class="card ${busy.has(key) ? "busy" : ""}" data-kind="${g.kind}" data-id="${esc(g.id)}">
    ${g.kind === "heater" ? `<div class="card-head"><h2>${esc(g.name)}</h2>${heatStateHTML(g)}</div>` : ""}
    <div class="temps">
      <div><span class="big">${deg(roomTemp(g))}</span><span class="label">Raumtemperatur</span></div>
      <div class="target ${p ? "pending" : ""}">
        <button class="round" data-action="sp" data-delta="-0.5" ${dis} aria-label="kälter">−</button>
        <div><span class="big">${deg(target)}</span><span class="label">${mode === "auto" ? planLabel(g, target) : "Solltemperatur"}</span></div>
        <button class="round" data-action="sp" data-delta="0.5" ${dis} aria-label="wärmer">+</button>
      </div>
    </div>
    <div class="modes">${buttons}</div>
    ${g.kind === "room" ? planRow(g, writable) : '<p class="hint">Für Heizpläne diese Heizung einem Raum zuordnen: unten bei ihr unter „Raum“.</p>'}
    ${roomDevicesHTML(g)}
    ${cardHistoryHTML(g)}
    ${!writable && online.length ? '<p class="hint">Nur Anzeige: In heizungen.conf fehlt der Token.</p>' : ""}
  </article>`;
}

function roomDevicesHTML(g) {
  // at the bottom of a room in the setup: every device with „Ist“ and „Soll“ – heaters and thermostats with their mode,
  // what is wrong and how far they show from the room's sensor, then the room's sensors with the time of their last reading
  const sensors = g.kind === "room" ? (state.sensors || []).filter(s => s.room === g.id) : [];
  const fresh = homeSensors(g);   // the deviation only from a current reading, like the room temperature
  const row = (icon, name, kind, ist, soll, extra = "") => `<li>${icon}<span class="dname">${name}<span
    class="dkind">${kind}</span>${extra}</span><span class="dval">${ist}</span><span class="dval">${soll}</span></li>`;
  const rows = g.heaters.map(h => row(deviceIcon(h), esc(h.name),
      (h.kind === "zigbee" ? "Thermostat" : "Heizung") + heaterTags(h), h.online ? deg(h.room_temp) : "–",
      !h.online ? "offline" : heaterOff(h) ? "aus" : deg(h.setpoint), deviationHTML(h, fresh)))
    .concat(sensors.map(s => row(deviceIcon(null), esc(s.name), "Fühler" + (s.measured_at ? ` · gemessen ${esc(fmtTime(s.measured_at))}` : ""),
      deg(s.temp), "–")));
  return `<ul class="room-devices"><li class="head"><span></span><span>Gerät</span><span>Ist</span><span>Soll</span></li>${
    rows.join("")}</ul>`;
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

function heaterTags(h) {  // what to know about a heater in its room's list: its mode (off says „Soll“) and what is wrong
  const tags = [];
  if (h.window_open) tags.push('<span class="tag warn">Fenster offen</span>');
  if (h.mode && !["manual", "off"].includes(h.mode)) tags.push(`<span class="tag">${esc(MODE_TEXT[h.mode] || h.mode)}</span>`);
  if (!h.writable) tags.push('<span class="tag">nur lesen</span>');
  if (h.battery_low) tags.push('<span class="tag warn">Batterie schwach</span>');
  if ((h.faults || []).some(f => !f.startsWith("Batterie"))) tags.push('<span class="tag bad">Störung</span>');
  if (h.online && h.clock_drift != null && Math.abs(h.clock_drift) > 60) tags.push('<span class="tag bad">Uhr falsch</span>');
  return tags.join("");
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
  if (view === "home") {   // the number hops at each tap – not when the tile is merely drawn again
    const num = document.querySelector(`#tiles .tile[data-kind="${g.kind}"][data-id="${CSS.escape(g.id)}"] .set-temp`);
    if (num) num.classList.add("pop");
  }
}

function commitSetpoint(g, temp) {
  const key = keyOf(g);
  act(key, async () => {
    try {
      const data = await api("POST", apiBase(g) + "/setpoint", { temp });
      if (view === "home") homeReport(g, `${degShort(temp)} eingestellt`, data);
      else report("Solltemperatur", data);
      if (!((data && data.results) || []).some(r => !r.ok)) {   // „✓ gesendet“ on the tile for a moment
        sentAt[key] = Date.now();
        setTimeout(render, SENT_MS + 50);
      }
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
  setHTML(el, editorHTML());
}

function updateDayBar(dayEl) {
  // after a new temperature only the day's bar changes: a redraw would replace the field under the mouse, and holding
  // its arrows would stop after one step
  const old = dayEl.querySelector(".day-head .bar");
  if (old) old.outerHTML = bar(editing.days[dayEl.dataset.day]);
  document.getElementById("tab-plans")._html = editorHTML();   // what the page shows now, for setHTML's comparison
}

function editorHTML() {
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
  return `<article class="card">
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
    </article>`;
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
  if (f.zigbee) {
    const removing = zigbeeNetPending === f.id;
    return `<div class="device-form">
      <label>Name<input data-field="name" value="${esc(f.name)}" placeholder="${esc(f.placeholderName)}" autocomplete="off"></label>
      <p class="hint">Das Thermostat funkt über Zigbee – es braucht keine Adresse und keinen Token. Den Raum wählst du in
        der Übersicht, den Heizplan bekommt es über seinen Raum.</p>
      <div class="row">
        <button class="primary" data-action="dev-save" ${saving || removing ? "disabled" : ""}>${saving ? "speichere …" : "Speichern"}</button>
        <button data-action="dev-cancel">Abbrechen</button>
        <button class="danger" data-action="dev-delete" ${saving || removing ? "disabled" : ""}>${removing ? "entferne …" : "Thermostat entfernen"}</button>
      </div>
    </div>`;
  }
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
  if (s.running) return `<div class="scan"><p class="scan-wait searching">Suche im Heimnetz … das dauert ein paar Sekunden.</p></div>`;
  const known = Object.fromEntries(state.heaters.filter(h => h.serial).map(h => [String(h.serial), h]));
  const other = `<div class="row scan-other"><span class="muted">Anderes Netz:</span>
      <input data-field="scan-network" value="${esc(scanNet)}" placeholder="z. B. 192.168.178" autocomplete="off"
        autocapitalize="off" spellcheck="false" inputmode="decimal">
      <button class="small" data-action="scan-other">Durchsuchen</button></div>`;
  if (s.error) {
    return `<div class="scan"><div class="scan-head"><strong>Suche im Netz</strong>
      <button class="small" data-action="scan-run">Erneut suchen</button></div>
      <p class="error-text">${esc(s.error)}</p>${other}</div>`;
  }
  const d = s.data;
  const items = d.heaters.map(f => {
    const h = known[String(f.serial)];
    const where = [f.address, f.room_temp != null ? deg(f.room_temp) : null].filter(Boolean).join(" · ");
    const status = h ? `<span class="tag">in der App: ${esc(h.name)}</span>`
      : `<button class="small primary" data-action="scan-add" data-address="${esc(f.address)}"
           data-hostname="${esc(f.hostname || "")}">Übernehmen</button>`;
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
      <button class="small" data-action="scan-run">Erneut suchen</button></div>
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

// ------------------------------------------------------------------ room sensors: Shelly Pill („Fühler suchen“)
const PILL_MODES = { onewire: "DS18B20 – Temperaturfühler (bis zu 5)", dht22: "DHT22 – Temperatur und Luftfeuchte" };
const PILL_RESTART_MS = 15 * 1000;   // a Pill is back about 15 seconds after a restart

function sensorText(f) {  // "21,3 °C · 45 % Luftfeuchte"
  const parts = [];
  if (f.temp != null) parts.push(deg(f.temp));
  if (f.humidity != null) parts.push(`${Math.round(f.humidity)} % Luftfeuchte`);
  return parts.join(" · ");
}

function pillModeText(mode) {
  return mode === "onewire" ? "DS18B20" : mode === "dht22" ? "DHT22" : mode ? `andere Betriebsart (${mode})` : "–";
}

function foundPill(id) {
  return sensorSearch && sensorSearch.data ? sensorSearch.data.pills.find(p => p.id === id) : null;
}

function pillProbesHTML(p, saved) {  // one row per probe the Pill reports, each with „Übernehmen“
  if (p.protected) return '<p class="hint">Mit einem Passwort geschützt – die App kann die Messwerte nicht lesen.</p>';
  if (!["onewire", "dht22"].includes(p.mode)) {
    return `<p class="hint">Die Pill steht auf einer anderen Betriebsart (${esc(p.mode || "unbekannt")}). Für einen
      Raumfühler „Einrichten“ drücken und DS18B20 oder DHT22 wählen.</p>`;
  }
  if (!p.probes.length) {
    return `<p class="hint">Kein Fühler erkannt. Den Fühler in die Buchse der Pill stecken, dann „Einrichten“ →
      „Pill neu starten“.</p>`;
  }
  const many = p.probes.length > 1;
  return `<ul class="heater-list scan-list">${p.probes.map((x, i) => {
    const sid = `${p.id}-${x.channel}`;
    const status = saved[sid] ? `<span class="tag">in der App: ${esc(saved[sid].name)}</span>`
      : sensorForm && sensorForm.mode === "new" && sensorForm.id === sid ? '<span class="tag">wird übernommen …</span>'
      : x.temp == null ? ""
      : `<button class="small primary" data-action="sensor-adopt" data-pill="${esc(p.id)}" data-channel="${x.channel}">Übernehmen</button>`;
    return `<li><span class="dot ${x.temp == null ? "offline" : "ok"}"></span><span class="hname"><b>${many ? `Fühler ${i + 1}` : "Fühler"}</b>
      <span class="muted">${esc(x.temp == null ? "liefert keinen Wert – steckt er richtig?" : sensorText(x))}</span></span>${status}</li>`;
  }).join("")}</ul>`;
}

function sensorPanelHTML() {
  const s = sensorSearch;
  if (s.running) {
    return `<div class="scan"><div class="scan-head"><strong class="searching">${s.waiting ? "Die Pill startet neu …" : "Suche läuft …"}</strong>
      <span class="muted">${s.waiting ? "Danach sucht die App von selbst noch einmal." : "Die App fragt jede Adresse im Heimnetz – das dauert ein paar Sekunden."}</span></div></div>`;
  }
  if (s.error) {
    return `<div class="scan"><div class="scan-head"><strong>Suche im Netz</strong>
      <button class="small" data-action="sensor-search">Erneut suchen</button></div><p class="error-text">${esc(s.error)}</p>${
      s.offline ? '<p class="hint">Sobald das Handy wieder im Heim-WLAN ist, sucht die App von selbst noch einmal.</p>' : ""}</div>`;
  }
  const d = s.data;
  const saved = Object.fromEntries((state.sensors || []).map(x => [x.id, x]));
  const pills = d.pills.map(p => `<div class="pill-found">
      <div class="pill-head"><span class="dot ${p.protected ? "offline" : "ok"}"></span><span class="hname"><b>${esc(p.name || p.model)}</b>
        <span class="muted">${esc([p.address, `Fühlerart: ${pillModeText(p.mode)}`].join(" · "))}</span></span>
        ${p.protected || (pillForm && pillForm.at === "search" && pillForm.pill === p.id) ? ""
          : `<button class="small" data-action="pill-setup" data-pill="${esc(p.id)}">Einrichten</button>`}</div>
      ${pillForm && pillForm.at === "search" && pillForm.pill === p.id ? pillFormHTML(pillForm) : pillProbesHTML(p, saved)}
    </div>`).join("");
  const summary = d.pills.length ? `${d.pills.length} Pill${d.pills.length === 1 ? "" : "s"} gefunden` : "keine Pill gefunden";
  return `<div class="scan"><div class="scan-head"><strong>Suche im Netz</strong>
      <span class="muted">${esc(d.networks.join(", "))} · ${String(d.seconds).replace(".", ",")} s · ${summary}</span>
      <button class="small" data-action="sensor-search">Erneut suchen</button></div>
    ${pills || `<p class="hint">Ist die Pill am USB-Netzteil und im WLAN? Eine neue Pill zuerst mit der Shelly-App ins WLAN
      bringen. Läuft die App in Docker oder in einem anderen Netz, findet sie die Pill nicht.</p>`}
  </div>`;
}

async function runSensorSearch() {  // one sweep of the home network – a Pill never sleeps, so one is enough
  const s = sensorSearch = { running: true };
  render();
  try {
    s.data = await api("POST", "api/sensors/discover", {});
  } catch (e) {
    s.error = e.message;
    if (e.offline) s.offline = Date.now();   // searchAgain() tries again once the Pi answers
  }
  s.running = false;
  if (sensorSearch !== s) return;
  invalidate();
  await refresh();
}

// ------------------------------------------------------------------ plugs with power metering („Steckdosen“)
// Zigbee plugs come from the Zigbee network, WLAN plugs (Shelly) from the search – both with „+ Gerät hinzufügen“.
// Which AeroFlow hangs on a plug is chosen at the plug, like the room of a sensor.
function plugFacts(p) {   // "an · 612 W · heute 1,4 kWh"
  return [p.state === "ON" ? "an" : p.state === "OFF" ? "aus" : "", p.state === "ON" && p.power != null ? wattText(p.power) : "",
          p.energy_today != null ? `heute ${kwhText(p.energy_today)}` : ""].filter(Boolean).join(" · ");
}

function radioTag(zigbee) { return `<span class="tag radio">${zigbee ? "Zigbee" : "WLAN"}</span>`; }   // how the app reaches it

function zigbeeDevice(ieee) { return ((zigbee && zigbee.net && zigbee.net.devices) || []).find(d => d.ieee === ieee) || null; }

function plugHeaterOptions(plugId, chosen) {   // the AeroFlows a plug can belong to – a thermostat has batteries
  return ['<option value="">– keine –</option>'].concat(state.heaters.filter(h => h.kind !== "zigbee").map(h => `<option value="${
    esc(h.id)}" ${h.id === chosen ? "selected" : ""}>${esc(h.name)}${h.plug && h.plug.id !== plugId
    ? ` (hängt an ${esc(h.plug.name)})` : ""}</option>`)).join("");
}

function plugRoomOptions(chosen) {   // the rooms a plug without a heater can be in
  return ['<option value="">– kein Raum –</option>'].concat(state.rooms.map(r => `<option value="${esc(r.id)}" ${
    r.id === chosen ? "selected" : ""}>${esc(r.name)}</option>`)).join("");
}

function plugRoomHTML(p) {   // at a heater: the heater's room – without one: a room to choose (any other device)
  if (p.heater) {
    const r = state.rooms.find(x => x.id === p.room);
    return r ? `${esc(r.name)} <span class="muted">– der Raum der Heizung</span>` : '<span class="muted">wie die Heizung – die hat noch keinen Raum</span>';
  }
  return `<select data-action="plug-room" aria-label="Raum von ${esc(p.name)}" ${busy.has("plugroom:" + p.id) ? "disabled" : ""}>${
    plugRoomOptions(p.room)}</select>`;
}

function applianceOptions(chosen) {
  return APPLIANCES.map(([id, label]) => `<option value="${id}" ${id === chosen ? "selected" : ""}>${label}</option>`).join("");
}

function plugApplianceHTML(p) {   // what hangs on the plug – at an AeroFlow a heater (to change: „– keine –“ at „Heizung“)
  if (p.heater) return 'Heizung <span class="muted">– eine AeroFlow</span>';
  return `<select data-action="plug-appliance" aria-label="Was an ${esc(p.name)} hängt" ${
    busy.has("plugappliance:" + p.id) ? "disabled" : ""}>${applianceOptions(p.appliance)}</select>`;
}

function plugSwitchHTML(p) {   // on and off, to test – the start page only switches a plug on again
  if (busy.has("plugswitch:" + p.id)) return '<span class="muted">schaltet …</span>';
  const on = '<button class="small" data-action="plug-switch" data-on="1">Einschalten</button>';
  const off = '<button class="small" data-action="plug-switch" data-on="0">Ausschalten</button>';
  return p.state === "ON" ? `an ${off}` : p.state === "OFF" ? `aus ${on}` : `${on} ${off}`;
}

function plugTileHTML(p) {
  const removing = busy.has("plug:" + p.id) || zigbeeNetPending === p.id;
  const z = p.kind === "zigbee" ? zigbeeDevice(p.id) : null;
  const device = p.kind === "zigbee" ? [p.model, z && zigbeeSignal(z.signal)]
    : [p.model, p.device_name !== p.name ? p.device_name : "", p.address];   // its own name is its name in the app
  const aeroflows = state.heaters.some(h => h.kind !== "zigbee");
  return `<div class="device ${removing ? "busy" : ""}" data-plug="${esc(p.id)}">
    <div class="device-head"><span class="dot ${p.error ? "offline" : p.found ? "ok" : ""}"></span><strong class="dname">${esc(p.name)}</strong>
      ${radioTag(p.kind === "zigbee")}<span class="muted">${esc(plugFacts(p) || (p.found ? "" : "noch kein Messwert"))}</span>
      <button class="small" data-action="plug-remove" ${removing ? "disabled" : ""}>${removing ? "entferne …" : "Entfernen …"}</button></div>
    <dl>
      <dt>Name</dt><dd>${esc(p.name)} <button class="small" data-action="plug-rename">Ändern …</button></dd>
      <dt>Angeschlossen</dt><dd>${plugApplianceHTML(p)}</dd>
      ${p.heater || p.appliance === "heizung" ? `<dt>Heizung</dt><dd>${aeroflows
        ? `<select data-action="plug-heater" aria-label="Heizung an ${esc(p.name)}" ${busy.has("plugheater:" + p.id)
          ? "disabled" : ""}>${plugHeaterOptions(p.id, p.heater)}</select>`
        : '<span class="muted">noch keine AeroFlow-Heizung in der App</span>'}</dd>` : ""}
      <dt>Raum</dt><dd>${plugRoomHTML(p)}</dd>
      <dt>Schalten</dt><dd>${plugSwitchHTML(p)}</dd>
      <dt>Strom</dt><dd><button class="small" data-action="plug-energy">Viertelstunden ansehen</button></dd>
      <dt>Gerät</dt><dd>${esc(device.filter(Boolean).join(" · ") || "–")}</dd>
      ${p.kind === "zigbee" ? `<dt>Zigbee-Adresse</dt><dd>${esc(p.id)}</dd>` : ""}
      ${p.error ? `<dt>Zustand</dt><dd><span class="error-text">${esc(p.error)}</span></dd>` : ""}
    </dl></div>`;
}

function plugSearchPanelHTML() {   // the WLAN part of „+ Gerät hinzufügen“ › Steckdose
  const s = plugSearch;
  if (s.running) {
    return `<div class="scan"><div class="scan-head"><strong class="searching">Suche im WLAN …</strong>
      <span class="muted">Die App fragt jede Adresse im Heimnetz – das dauert ein paar Sekunden.</span></div></div>`;
  }
  if (s.error) {
    return `<div class="scan"><div class="scan-head"><strong>Suche im WLAN</strong>
      <button class="small" data-action="plug-search">Erneut suchen</button></div><p class="error-text">${esc(s.error)}</p>${
      s.offline ? '<p class="hint">Sobald das Handy wieder im Heim-WLAN ist, sucht die App von selbst noch einmal.</p>' : ""}</div>`;
  }
  const d = s.data, known = new Set((state.plugs || []).map(p => p.id));
  const rows = d.plugs.map(p => {
    const facts = p.protected ? "mit einem Passwort geschützt – die App kann sie nicht lesen" : p.offline ? "antwortet nicht"
      : [p.on === true ? "an" : p.on === false ? "aus" : "", p.on && p.power != null ? wattText(p.power) : ""].filter(Boolean).join(" · ");
    const status = known.has(p.id) ? '<span class="tag">in der App</span>' : p.protected || p.offline ? ""
      : `<button class="small primary" data-action="plug-adopt" data-plug="${esc(p.id)}" ${busy.has("plug:" + p.id)
        ? "disabled" : ""}>Übernehmen</button>`;
    return `<li><span class="dot ${p.protected || p.offline ? "offline" : "ok"}"></span><span class="hname"><b>${esc(p.name)}</b>
      <span class="muted">${esc([p.model, p.device_name !== p.name ? p.device_name : "", p.address, facts].filter(Boolean)
        .join(" · "))}</span></span>${status}</li>`;
  }).join("");
  const summary = d.plugs.length ? `${d.plugs.length} Steckdose${d.plugs.length === 1 ? "" : "n"} gefunden` : "keine Steckdose gefunden";
  return `<div class="scan"><div class="scan-head"><strong>Suche im WLAN</strong>
      <span class="muted">${esc(d.networks.join(", "))} · ${String(d.seconds).replace(".", ",")} s · ${summary}</span>
      <button class="small" data-action="plug-search">Erneut suchen</button></div>
    ${rows ? `<ul class="heater-list scan-list">${rows}</ul>` : `<p class="hint">Ist die Steckdose eingesteckt und im WLAN?
      Dann „Erneut suchen“. Läuft die App in Docker oder in einem anderen Netz, findet sie die Steckdose nicht.</p>`}
  </div>`;
}

function plugsCardHTML() {
  const plugs = state.plugs || [];
  return `<article class="card"><div class="card-head"><h2>Steckdosen mit Strommessung</h2></div>
    <p class="hint">Was an einer Steckdose hängt, wählst du bei „Angeschlossen“ – unter „Strom“ zeigt die App dann, was
      jedes Gerät braucht und kostet. Hängt eine AeroFlow an einer Steckdose mit Strommessung, misst die App beim Heizen,
      wie viel Strom sie wirklich zieht. Zu welcher Heizung eine Steckdose gehört – oder ohne Heizung, in welchem Raum
      sie ist –, wählst du hier bei der Steckdose. „Viertelstunden ansehen“ zeigt, wie viel Strom sie in jeder Viertelstunde gezählt hat. Ist
      die Steckdose aus, zeigt die Startseite das und schaltet sie mit einem Tipp wieder ein. Ausschalten geht nur hier
      bei „Schalten“ – zum Testen.</p>
    ${plugs.map(plugTileHTML).join("")}
    ${plugs.length ? "" : '<p class="empty">Noch keine Steckdose in der App.</p>'}
  </article>`;
}

async function runPlugSearch() {  // one sweep of the home network for Shelly plugs – they never sleep
  const s = plugSearch = { running: true };
  render();
  try {
    s.data = await api("POST", "api/plugs/discover", {});
  } catch (e) {
    s.error = e.message;
    if (e.offline) s.offline = Date.now();   // searchAgain() tries again once the Pi answers
  }
  s.running = false;
  if (plugSearch !== s) return;
  invalidate();
  await refresh();
}

function searchAgain(back = false) {   // „+ Gerät hinzufügen“ searches the WLAN again by itself: once the Pi answers
  // after a search could not reach it (the phone was in the plug's own WLAN, say), and – back – whenever the page is
  // seen again, so a Pill or plug just brought into the WLAN shows up without „Erneut suchen“
  const f = addFlow;
  if (!f || f.step !== "find" || document.hidden) return;
  const due = s => s && !s.running && (back || (s.offline && Date.now() - s.offline > 4000));
  if (f.kind === "plug" && due(plugSearch)) runPlugSearch();
  else if (f.kind === "sensor" && !sensorForm && due(sensorSearch)) runSensorSearch();
}

async function adoptPlug(id) {   // „Übernehmen“: the plug from the search becomes a plug of the app – then: which heater?
  const key = "plug:" + id;
  if (busy.has(key)) return;
  busy.add(key);
  render();
  try {
    toast((await api("POST", "api/plugs", { plug: id })).message);
    if (addFlow) addWhere("plug", id);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
}

async function setPlugHeater(id, heater, quiet = false) {   // „gehört zu“ at the plug – true when saved
  const key = "plugheater:" + id;
  if (busy.has(key)) return false;
  busy.add(key);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  let ok = false;
  try {
    const res = await api("PUT", `api/plugs/${encodeURIComponent(id)}/heater`, { heater: heater || null });
    if (!quiet || res.warning) toast(res.message || "Gespeichert.", !!res.warning);
    ok = true;
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
  return ok;
}

async function setPlugAppliance(id, appliance, quiet = false) {   // „Angeschlossen“: what hangs on it – true when saved
  const key = "plugappliance:" + id;
  if (busy.has(key)) return false;
  busy.add(key);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  let ok = false;
  try {
    const res = await api("PUT", `api/plugs/${encodeURIComponent(id)}/appliance`, { appliance });
    if (!quiet) toast(res.message || "Gespeichert.");
    ok = true;
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
  return ok;
}

async function setPlugRoom(id, room, quiet = false) {   // a plug without a heater: the room it is in – true when saved
  const key = "plugroom:" + id;
  if (busy.has(key)) return false;
  busy.add(key);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  let ok = false;
  try {
    const res = await api("PUT", `api/plugs/${encodeURIComponent(id)}/room`, { room: room || null });
    if (!quiet) toast(res.message || "Gespeichert.");
    ok = true;
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
  return ok;
}

async function switchPlug(id, on) {   // „Einschalten“ / „Ausschalten“ at the plug – to test it
  const p = (state.plugs || []).find(x => x.id === id);
  if (!p) return;
  const owner = p.heater && state.heaters.find(h => h.id === p.heater);
  if (!on && owner) {
    const yes = await ask({ title: "Steckdose ausschalten?",
      text: `${owner.name} hat dann keinen Strom und heizt nicht, bis die Steckdose wieder an ist. Einschalten kannst du ` +
        "sie hier, auf der Startseite oder an ihrer Taste.",
      buttons: [{ label: "Abbrechen", value: null }, { label: "Ausschalten", value: "yes", cls: "danger-solid" }] });
    if (!yes) return;
  }
  act("plugswitch:" + id, async () => toast((await api("POST", `api/plugs/${encodeURIComponent(id)}/switch`, { on })).message));
}

async function renamePlug(id) {   // „Ändern …“ at the name: a WLAN plug keeps it itself, a Zigbee plug's stays in the app
  const p = (state.plugs || []).find(x => x.id === id);
  if (!p) return;
  const wifi = p.kind !== "zigbee";
  const fallback = `${wifi ? "WLAN-Steckdose" : "Steckdose"} ${id.slice(-4).toUpperCase()}`;   // as the server names it
  const done = await ask({ title: "Steckdose umbenennen",
    text: (wifi ? "Die App speichert den Namen in der Steckdose – die Shelly-App zeigt ihn dann auch. " : "") +
      `Leer lassen: „${fallback}“.`,
    fields: [{ id: "name", label: "Name", type: "text", value: p.name === fallback ? "" : p.name, placeholder: fallback }],
    buttons: [{ label: "Abbrechen", value: null }, { label: "Speichern", value: "submit", cls: "primary" }],
    onSubmit: async v => {
      try { toast((await api("PUT", `api/plugs/${encodeURIComponent(id)}/name`, { name: v.name })).message); return null; }
      catch (e) { return e.message; }
    } });
  if (done) { invalidate(); await refresh(); }
}

async function removePlug(id) {   // „Entfernen …“: a WLAN plug the app forgets, a Zigbee plug the Pi unpairs – the plug stays as it is
  const p = (state.plugs || []).find(x => x.id === id);
  if (!p) return;
  const owner = p.heater && state.heaters.find(h => h.id === p.heater);
  const zigbeePlug = p.kind === "zigbee";
  const yes = await ask({ title: "Steckdose entfernen?",
    text: (zigbeePlug ? `Der Pi meldet ${p.name} im Zigbee-Netz ab.` : `Die App liest ${p.name} dann nicht mehr.`) +
      (owner ? ` Sie gehört dann auch nicht mehr zu ${owner.name}.` : "") + " Die Steckdose selbst bleibt, wie sie ist – an " +
      `oder aus. Später kannst du sie mit „+ Gerät hinzufügen“ wieder ${zigbeePlug ? "anlernen" : "übernehmen"}.`,
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  if (zigbeePlug) {
    await zigbeeUnpair(id, "die Steckdose", "sie");
    return refresh();
  }
  act("plug:" + id, async () => toast((await api("DELETE", `api/plugs/${encodeURIComponent(id)}`)).message));
}

// ------------------------------------------------------------------ Geräte › Licht
const LIGHT_COLOR_WORDS = { kerze: "Kerzenlicht", warm: "warmweiß", neutral: "neutralweiß", kalt: "kaltweiß",
  tag: "Tageslicht" };   // Service.LIGHT_COLORS …
const LIGHT_KELVIN = { kerze: 2200, warm: 2700, neutral: 4000, kalt: 5000, tag: 6500 };   // … in kelvin
const LIGHT_HUES = [["rot", "Rot", 0], ["orange", "Orange", 30], ["gelb", "Gelb", 55], ["gruen", "Grün", 120],
  ["tuerkis", "Türkis", 175], ["blau", "Blau", 230], ["lila", "Lila", 275], ["pink", "Pink", 320]];   // Service.LIGHT_HUES

function lightFacts(l) {   // "an · 70 %"
  return l.lost ? "keine Verbindung" : l.state === "ON" ? `an${l.brightness != null ? ` · ${l.brightness} %` : ""}`
    : l.state === "OFF" ? "aus" : "";
}

function lightSwitchHTML(l) {   // on and off – as on the start page
  if (busy.has("lamp:" + l.id)) return '<span class="muted">schaltet …</span>';
  const on = '<button class="small" data-action="light-one" data-on="1">Einschalten</button>';
  const off = '<button class="small" data-action="light-one" data-on="0">Ausschalten</button>';
  return l.lost ? `${on} ${off}` : l.state === "ON" ? `an ${off}` : l.state === "OFF" ? `aus ${on}` : `${on} ${off}`;
}

function lampDefaultsHTML(l) {   // what the app sets whenever it switches the lamp on – or as it was last
  const levels = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
  if (l.level != null && !levels.includes(l.level)) levels.push(l.level), levels.sort((a, b) => a - b);
  const level = !l.dim ? "" : `<label class="lamp-default">Helligkeit <select data-action="light-level-set" ${
    busy.has("lightlevel:" + l.id) ? "disabled" : ""}><option value="">wie zuletzt</option>${levels.map(v =>
    `<option value="${v}" ${l.level === v ? "selected" : ""}>${v} %</option>`).join("")}</select></label>`;
  const color = !l.colors.length ? "" : `<label class="lamp-default">Lichtfarbe <select data-action="light-color-set" ${
    busy.has("lightcolor:" + l.id) ? "disabled" : ""}><option value="">wie zuletzt</option>${l.colors.map(c =>
    `<option value="${c}" ${l.color === c ? "selected" : ""}>${LIGHT_COLOR_WORDS[c]} (${LIGHT_KELVIN[c]} K)</option>`)
    .join("")}</select></label>`;
  if (!level && !color) return '<span class="muted">–</span>';
  return `${level}${color}<div class="segmented-text muted">Stellt die App ein, wenn sie die Lampe einschaltet – nicht
    beim Lichtschalter an der Wand.</div>`;
}

function lightTileHTML(l) {
  const removing = zigbeeNetPending === l.id, z = zigbeeDevice(l.id);
  return `<div class="device ${removing ? "busy" : ""}" data-light="${esc(l.id)}">
    <div class="device-head"><span class="dot ${l.lost ? "offline" : "ok"}"></span><strong class="dname">${esc(l.name)}</strong>
      ${radioTag(true)}<span class="muted">${esc(lightFacts(l))}</span>
      <button class="small" data-action="light-remove" ${removing ? "disabled" : ""}>${removing ? "entferne …" : "Entfernen …"}</button></div>
    <dl>
      <dt>Name</dt><dd>${esc(l.name)} <button class="small" data-action="light-rename">Ändern …</button></dd>
      <dt>Raum</dt><dd><select data-action="light-room-set" aria-label="Raum von ${esc(l.name)}" ${
        busy.has("lightroom:" + l.id) ? "disabled" : ""}>${plugRoomOptions(l.room)}</select>${l.room ? ""
        : ' <span class="muted">– ohne Raum nicht auf der Startseite</span>'}</dd>
      <dt>Schalten</dt><dd>${lightSwitchHTML(l)}</dd>
      ${l.dim ? `<dt>Helligkeit</dt><dd class="lamp-wide">${lampBrightnessHTML(l)}</dd>` : ""}
      ${l.kelvins ? `<dt>Lichtfarbe</dt><dd>${lampWhiteHTML(l)}</dd>` : ""}
      ${l.hues ? `<dt>Farbe</dt><dd>${lampHueHTML(l)}</dd>` : ""}
      <dt>Beim Einschalten</dt><dd>${lampDefaultsHTML(l)}</dd>
      <dt>Gerät</dt><dd>${esc([l.model, z && zigbeeSignal(z.signal)].filter(Boolean).join(" · ") || "–")}</dd>
      <dt>Zigbee-Adresse</dt><dd>${esc(l.id)}</dd>
      ${l.lost ? '<dt>Zustand</dt><dd><span class="error-text">Hat beim letzten Schalten nicht geantwortet. Ist der ' +
        "Lichtschalter an der Wand an?</span></dd>" : ""}
    </dl></div>`;
}

function lightsCardHTML() {
  const lamps = state.lights || [];
  return `<article class="card"><div class="card-head"><h2>Licht</h2></div>
    <p class="hint">Lampen mit Zigbee – Hue, IKEA, Innr und andere – hängen direkt am Pi, ohne Bridge. Wähle für jede
      Lampe ihren Raum: Auf der Startseite schalten die Bewohner dann das Licht des Raums ein und aus, heller und dunkler
      – und mit „Einstellen ›“ jede Lampe für sich. Damit die Lampen immer Strom haben, bleibt der Lichtschalter an der
      Wand an.</p>
    ${lamps.map(lightTileHTML).join("")}
    ${lamps.length ? "" : '<p class="empty">Noch keine Lampe in der App. Eine neue lernst du mit „+ Gerät hinzufügen“ an.</p>'}
  </article>${buttonsCardHTML()}${detectorsCardHTML()}${motionHintHTML()}`;
}

// ------------------------------------------------------------------ motion sensors (Geräte › Licht)
const SENSITIVITY_WORDS = { low: "niedrig", medium: "mittel", high: "hoch", very_high: "sehr hoch",
  max: "am höchsten" };   // Service.SENSITIVITY_WORDS

function detectorsCardHTML() {
  const sensors = state.detectors || [];
  if (!sensors.length && !(state.lights || []).length) return "";
  return `<article class="card"><div class="card-head"><h2>Bewegungsmelder</h2></div>
    <p class="hint">Ein Bewegungsmelder merkt, wenn sich im Raum jemand bewegt. Dann schaltet die App dort das Licht ein –
      und wieder aus, wenn eine Weile nichts mehr los ist. Wie, stellst du im Raum bei „Licht bei Bewegung“ ein. Das
      schaltet die App, also nur, wenn der Pi läuft.</p>
    ${sensors.map(detectorTileHTML).join("")}
    ${sensors.length ? "" : '<p class="empty">Noch kein Bewegungsmelder in der App. Einen neuen lernst du mit „+ Gerät hinzufügen“ an.</p>'}
  </article>`;
}

function detectorTileHTML(x) {
  const removing = zigbeeNetPending === x.id, z = zigbeeDevice(x.id), sending = busy.has("detector:" + x.id);
  const low = x.battery != null && x.battery <= 15;
  const sens = !x.sensitivities.length ? "" : `<select data-action="detector-sens" aria-label="Empfindlichkeit von ${
    esc(x.name)}" ${sending ? "disabled" : ""}>${x.sensitivity ? "" : '<option value="" selected disabled>– noch nicht bekannt –</option>'}${
    x.sensitivities.map(v => `<option value="${esc(v)}" ${v === x.sensitivity ? "selected" : ""}>${esc(SENSITIVITY_WORDS[v] || v)}</option>`)
    .join("")}</select>`;
  return `<div class="device ${removing ? "busy" : ""}" data-detector="${esc(x.id)}">
    <div class="device-head"><span class="dot ${x.silent ? "offline" : x.moving ? "on" : "ok"}"></span><strong class="dname">${
      esc(x.name)}</strong>${radioTag(true)}<span class="muted">${esc(x.moving ? "Bewegung gerade" : x.moved
      ? `Bewegung ${agoText(x.moved)}` : "Bewegungsmelder")}</span>
      <button class="small" data-action="detector-remove" ${removing ? "disabled" : ""}>${removing ? "entferne …" : "Entfernen …"}</button></div>
    <dl>
      <dt>Name</dt><dd>${esc(x.name)} <button class="small" data-action="detector-rename">Ändern …</button></dd>
      <dt>Raum</dt><dd><select data-action="detector-room-set" aria-label="Raum von ${esc(x.name)}" ${
        busy.has("detectorroom:" + x.id) ? "disabled" : ""}>${plugRoomOptions(x.room)}</select>${x.room ? ""
        : ' <span class="muted">– ohne Raum schaltet er kein Licht</span>'}</dd>
      <dt>Bewegung</dt><dd>${esc(x.moving ? "gerade" : x.moved ? agoText(x.moved) : "seit dem Start der App noch keine")}${
        x.silent ? `<br><span class="error-text">Meldet sich seit Stunden nicht mehr. Ist die Batterie leer, oder ist er
        zu weit vom Pi weg?</span>` : ""}</dd>
      ${x.lux != null ? `<dt>Helligkeit</dt><dd>${Math.round(x.lux)} Lux</dd>` : ""}
      ${x.temperature != null ? `<dt>Temperatur</dt><dd>${deg(x.temperature)}</dd>` : ""}
      <dt>Batterie</dt><dd>${x.battery == null ? "–" : low ? `<span class="error-text">${Math.round(x.battery)} % – bitte bald
        wechseln</span>` : `${Math.round(x.battery)} %`}</dd>
      ${sens ? `<dt>Empfindlichkeit</dt><dd>${sens}</dd>` : ""}
      ${x.led_settable ? `<dt>Sein Licht</dt><dd><label class="check"><input type="checkbox" data-action="detector-led" ${
        x.led ? "checked" : ""} ${sending ? "disabled" : ""}> blinkt bei Bewegung</label></dd>` : ""}
      ${sending ? '<dt></dt><dd class="muted">sende … Er schläft zwischendurch – einmal vor ihm bewegen weckt ihn.</dd>' : ""}
      <dt>Gerät</dt><dd>${esc([x.model, z && zigbeeSignal(z.signal)].filter(Boolean).join(" · ") || "–")}</dd>
      <dt>Zigbee-Adresse</dt><dd>${esc(x.id)}</dd>
    </dl></div>`;
}

async function setDetectorRoom(id, room, quiet = false) {   // the room of a motion sensor – true when saved
  const key = "detectorroom:" + id;
  if (busy.has(key)) return false;
  busy.add(key);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  let ok = false;
  try {
    const res = await api("PUT", `api/detectors/${encodeURIComponent(id)}/room`, { room: room || null });
    if (!quiet) toast(res.message || "Gespeichert.");
    ok = true;
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
  return ok;
}

async function renameDetector(id, quiet = false, name = null) {   // „Ändern …“ at the name – it stays in the app
  const x = (state.detectors || []).find(v => v.id === id);
  if (!x) return false;
  const fallback = `Melder ${id.slice(-4).toUpperCase()}`;   // as the server names it
  const url = `api/detectors/${encodeURIComponent(id)}/name`;
  if (name !== null) {   // from „+ Gerät hinzufügen“
    try { const res = await api("PUT", url, { name }); if (!quiet) toast(res.message); }
    catch (e) { toast(e.message, true); return false; }
    return true;
  }
  const done = await ask({ title: "Bewegungsmelder umbenennen", text: `Leer lassen: „${fallback}“.`,
    fields: [{ id: "name", label: "Name", type: "text", value: x.name === fallback ? "" : x.name, placeholder: fallback }],
    buttons: [{ label: "Abbrechen", value: null }, { label: "Speichern", value: "submit", cls: "primary" }],
    onSubmit: async v => {
      try { toast((await api("PUT", url, { name: v.name })).message); return null; }
      catch (e) { return e.message; }
    } });
  if (done) { invalidate(); await refresh(); }
  return !!done;
}

async function removeDetector(id) {   // „Entfernen …“: the Pi unpairs it – it sleeps: a motion right before wakes it
  const x = (state.detectors || []).find(v => v.id === id);
  if (!x) return;
  const yes = await ask({ title: "Bewegungsmelder entfernen?",
    text: `Der Pi meldet ${x.name} im Zigbee-Netz ab. Beweg dich kurz vorher vor dem Melder – dann ist er wach und hört ` +
      "es. Später kannst du ihn mit „+ Gerät hinzufügen“ wieder anlernen.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  await zigbeeUnpair(id, "den Bewegungsmelder", "er", "ihn");
  return refresh();
}

function setDetector(id, body) {   // its sensitivity or its light – it sleeps: the server waits a few seconds for it
  act("detector:" + id, async () => toast((await api("PUT", `api/detectors/${encodeURIComponent(id)}/settings`, body)).message));
}

// „Licht bei Bewegung“: in the room view of a room with a motion sensor
const MOTION_MINUTES = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120];
const MOTION_LEVELS = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
const MOTION_LUX = [5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 1000];
const MOTION_PAUSE = [0, 1, 2, 5, 10, 15, 30];
const MOTION_CLOCK = Array.from({ length: 48 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`);

function motionHintHTML() {   // „Licht bei Bewegung“ is set in the room view – here only the way there
  const sensors = state.detectors || [];
  if (!sensors.length) return "";
  const rooms = state.rooms.filter(r => sensors.some(x => x.room === r.id));
  return `<article class="card"><div class="card-head"><h2>Licht bei Bewegung</h2></div>
    <p class="hint">Geht jemand durch einen Raum mit Bewegungsmelder, schaltet die App dort das Licht ein – und nach einer
      Weile ohne Bewegung wieder aus. Wie lange, wie hell und ob nachts gedämpft, stellst du im Raum ein: unter „Räume“,
      im Abschnitt „Licht“.</p>
    ${rooms.length ? `<div class="row">${rooms.map(r => `<button class="small" data-action="room-go" data-room="${
      esc(r.id)}">${esc(r.name)} · ${r.motion && r.motion.on ? "an" : "aus"} ›</button>`).join("")}</div>`
      : '<p class="empty">Noch kein Bewegungsmelder in einem Raum. Wähle oben beim Melder seinen Raum.</p>'}
  </article>`;
}

function motionRoomHTML(r) {
  const m = r.motion || {}, dis = busy.has("motion:" + r.id) ? "disabled" : "";
  const lamps = (state.lights || []).filter(l => l.room === r.id), sensors = (state.detectors || []).filter(x => x.room === r.id);
  const minutes = n => `${n} Minute${n === 1 ? "" : "n"}`;
  const pick = (field, list, word) => `<select data-mfield="${field}" ${dis}>${(list.includes(m[field]) || m[field] == null
    ? list : list.concat([m[field]]).sort((a, b) => a < b ? -1 : 1)).map(v => `<option value="${v}" ${v === m[field]
    ? "selected" : ""}>${esc(word(v))}</option>`).join("")}</select>`;
  const colors = field => pick(field, Object.keys(LIGHT_COLOR_WORDS), c => LIGHT_COLOR_WORDS[c]);
  const box = (field, words) => `<label class="check"><input type="checkbox" data-mfield="${field}" ${m[field] ? "checked"
    : ""} ${dis}> ${words}</label>`;
  const lux = sensors.filter(x => x.lux != null).map(x => x.lux);
  const status = !lamps.length ? "In diesem Raum ist noch keine Lampe. Wähle bei einer Lampe diesen Raum."
    : m.auto ? `Gerade an durch Bewegung${m.off_at ? ` – geht um ${clockText(m.off_at)} Uhr aus` : ""}.`
    : m.pause_until ? `Von Hand ausgeschaltet – reagiert wieder ab ${clockText(m.pause_until)} Uhr.` : "";
  const head = `<div class="motion-head"><h3>Licht bei Bewegung</h3>${box("on", "eingeschaltet")}</div>${status
    ? `<p class="hint">${esc(status)}</p>` : ""}`;
  if (!m.on) return `<div class="motion-room" data-motion-room="${esc(r.id)}">${head}</div>`;
  const which = (legend, attr, items, chosen) => items.length < 2 ? "" : `<fieldset><legend>${legend}</legend>${items.map(x =>
    `<label class="check"><input type="checkbox" data-${attr}="${esc(x.id)}" ${!chosen || chosen.includes(x.id) ? "checked" : ""} ${
      dis}> ${esc(x.name)}</label>`).join("")}</fieldset>`;
  return `<div class="motion-room" data-motion-room="${esc(r.id)}">${head}
    <div class="motion-form">
      <label>Aus, wenn sich so lange nichts bewegt hat ${pick("minutes", MOTION_MINUTES, minutes)}</label>
      <fieldset><legend>${m.night ? "Tagsüber" : "So hell"}</legend>
        <label>Helligkeit ${pick("day_level", MOTION_LEVELS, v => v + " %")}</label>
        <label>Lichtfarbe ${colors("day_color")}</label></fieldset>
      ${box("night", "Nachts gedämpftes Licht")}
      ${!m.night ? "" : `<fieldset><legend>Nachts${m.night_now ? " – gilt gerade" : ""}</legend>
        <label>von ${pick("night_from", MOTION_CLOCK, v => v + " Uhr")}</label>
        <label>bis ${pick("night_to", MOTION_CLOCK, v => v + " Uhr")}</label>
        <label>Helligkeit ${pick("night_level", MOTION_LEVELS, v => v + " %")}</label>
        <label>Lichtfarbe ${colors("night_color")}</label></fieldset>`}
      ${box("dark_only", "Nur, wenn es dunkel ist")}
      ${!m.dark_only ? "" : `<label>Dunkel heißt: weniger als ${pick("lux", MOTION_LUX, v => v + " Lux")}<small>${lux.length
        ? `Der Melder misst gerade ${Math.round(Math.min(...lux))} Lux${lamps.some(l => l.state === "ON") ? " – mit Licht" : ""}.`
        : "Der Melder hat noch keine Helligkeit gemeldet."}</small></label>`}
      <label>Licht, das jemand von Hand eingeschaltet hat, <select data-mfield="hand_off" ${dis}><option value="0" ${
        m.hand_off ? "" : "selected"}>bleibt an</option><option value="1" ${m.hand_off ? "selected" : ""}>geht auch aus</option></select></label>
      <label>Von Hand ausgeschaltet: wieder bei Bewegung ${pick("pause", MOTION_PAUSE, v => v ? `nach ${minutes(v)}` : "sofort")}</label>
      ${which("Diese Lampen gehen an", "mlamp", lamps, m.lamps)}
      ${which("Bei diesen Meldern", "mdet", sensors, m.detectors)}
    </div></div>`;
}

function motionChange(t) {   // a change in „Licht bei Bewegung“ goes to the server at once
  const room = t.closest("[data-motion-room]").dataset.motionRoom, field = t.dataset.mfield;
  let body;
  if (field) {
    body = { [field]: t.type === "checkbox" ? t.checked : field === "hand_off" ? t.value === "1"
      : ["minutes", "day_level", "night_level", "lux", "pause"].includes(field) ? Number(t.value) : t.value };
  } else {   // which lamps, which sensors: all of them – null, so a new one counts too
    const attr = t.dataset.mlamp !== undefined ? "mlamp" : "mdet";
    const boxes = [...t.closest("fieldset").querySelectorAll(`input[data-${attr}]`)];
    const chosen = boxes.filter(b => b.checked).map(b => b.dataset[attr]);
    if (!chosen.length) {
      toast(attr === "mlamp" ? "Mindestens eine Lampe muss angehen." : "Mindestens ein Melder muss das Licht schalten.", true);
      invalidate();
      return render();
    }
    body = { [attr === "mlamp" ? "lamps" : "detectors"]: chosen.length === boxes.length ? null : chosen };
  }
  t.blur();
  act("motion:" + room, async () => toast((await api("PUT", `api/rooms/${encodeURIComponent(room)}/motion`, body)).message));
}

// ------------------------------------------------------------------ switches (Geräte › Licht)
function buttonsCardHTML() {
  const buttons = state.buttons || [];
  if (!buttons.length && !(state.lights || []).length) return "";
  return `<article class="card"><div class="card-head"><h2>Schalter</h2></div>
    <p class="hint">Ein Funk-Schalter schaltet das Licht eines Raums – mehrere Schalter im selben Raum schalten dieselben
      Lampen. IKEA RODRET, Hue Smart Button und Hue Dimmschalter koppelt die App direkt mit den Lampen: Dann gehen sie
      auch, wenn der Pi aus ist. Andere Schalter schalten über die App.</p>
    ${buttons.map(buttonTileHTML).join("")}
    ${buttons.length ? "" : '<p class="empty">Noch kein Schalter in der App. Einen neuen lernst du mit „+ Gerät hinzufügen“ an.</p>'}
  </article>`;
}

function buttonState(b) {   // [dot, words]: coupled, waiting for a press, through the app
  if (!b.room) return ["", "Schaltet kein Licht."];
  if (!b.direct) return ["ok", "Schaltet über die App – nur, wenn der Pi läuft."];
  if (b.coupled) return ["ok", "Gekoppelt – schaltet die Lampen direkt, auch ohne Pi."];
  return ["on", "Noch nicht gekoppelt: einmal eine Taste am Schalter drücken. Bis dahin schaltet die App."];
}

function buttonRoomOptions(chosen) {
  return ['<option value="">– kein Licht –</option>'].concat(state.rooms.map(r => `<option value="${esc(r.id)}" ${
    r.id === chosen ? "selected" : ""}>${esc(r.name)}</option>`)).join("");
}

function buttonTileHTML(b) {
  const removing = zigbeeNetPending === b.id, z = zigbeeDevice(b.id), [dot, words] = buttonState(b);
  const low = b.battery != null && b.battery <= 15;
  return `<div class="device ${removing ? "busy" : ""}" data-button="${esc(b.id)}">
    <div class="device-head"><span class="dot ${dot}"></span><strong class="dname">${esc(b.name)}</strong>
      ${radioTag(true)}<span class="muted">${esc(b.kind || "Schalter")}</span>
      <button class="small" data-action="button-remove" ${removing ? "disabled" : ""}>${removing ? "entferne …" : "Entfernen …"}</button></div>
    <dl>
      <dt>Name</dt><dd>${esc(b.name)} <button class="small" data-action="button-rename">Ändern …</button></dd>
      <dt>Schaltet das Licht im Raum</dt><dd><select data-action="button-room-set" aria-label="Licht für ${esc(b.name)}" ${
        busy.has("buttonroom:" + b.id) ? "disabled" : ""}>${buttonRoomOptions(b.room)}</select></dd>
      <dt>Zustand</dt><dd>${esc(words)}${b.error && b.room && !b.coupled ? `<br><span class="error-text">Koppeln hat
        nicht geklappt: ${esc(b.error)} Am besten nah am Pi noch einmal eine Taste drücken.</span>` : ""}</dd>
      <dt>Batterie</dt><dd>${b.battery == null ? "–" : low ? `<span class="error-text">${Math.round(b.battery)} % – bitte bald
        wechseln</span>` : `${Math.round(b.battery)} %`}</dd>
      <dt>Zuletzt gedrückt</dt><dd>${b.pressed ? esc(fmtTime(b.pressed)) + " Uhr" : "–"}</dd>
      <dt>Gerät</dt><dd>${esc([b.model, z && zigbeeSignal(z.signal)].filter(Boolean).join(" · ") || "–")}</dd>
      <dt>Zigbee-Adresse</dt><dd>${esc(b.id)}</dd>
    </dl></div>`;
}

async function setButtonRoom(id, room, quiet = false) {   // the room whose light the switch switches – when saved:
  const key = "buttonroom:" + id;                          // what the server said (true without words), else false
  if (busy.has(key)) return false;
  busy.add(key);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  let ok = false;
  try {
    const res = await api("PUT", `api/buttons/${encodeURIComponent(id)}/room`, { room: room || null });
    if (!quiet) toast(res.message || "Gespeichert.");
    ok = res.message || true;
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
  return ok;
}

async function renameButton(id, quiet = false, name = null) {   // „Ändern …“ at the name – it stays in the app
  const b = (state.buttons || []).find(x => x.id === id);
  if (!b) return false;
  const fallback = `Schalter ${id.slice(-4).toUpperCase()}`;   // as the server names it
  if (name !== null) {   // from „+ Gerät hinzufügen“
    try { const res = await api("PUT", `api/buttons/${encodeURIComponent(id)}/name`, { name }); if (!quiet) toast(res.message); }
    catch (e) { toast(e.message, true); return false; }
    return true;
  }
  const done = await ask({ title: "Schalter umbenennen", text: `Leer lassen: „${fallback}“.`,
    fields: [{ id: "name", label: "Name", type: "text", value: b.name === fallback ? "" : b.name, placeholder: fallback }],
    buttons: [{ label: "Abbrechen", value: null }, { label: "Speichern", value: "submit", cls: "primary" }],
    onSubmit: async v => {
      try { toast((await api("PUT", `api/buttons/${encodeURIComponent(id)}/name`, { name: v.name })).message); return null; }
      catch (e) { return e.message; }
    } });
  if (done) { invalidate(); await refresh(); }
  return !!done;
}

async function removeButton(id) {   // „Entfernen …“: the Pi unpairs it – it sleeps: a press right before wakes it
  const b = (state.buttons || []).find(x => x.id === id);
  if (!b) return;
  const yes = await ask({ title: "Schalter entfernen?",
    text: `Der Pi meldet ${b.name} im Zigbee-Netz ab und nimmt ihm die Lampen. Drück kurz vorher eine Taste am Schalter – ` +
      "dann ist er wach und hört es. Später kannst du ihn mit „+ Gerät hinzufügen“ wieder anlernen.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  await zigbeeUnpair(id, "den Schalter", "er", "ihn");
  return refresh();
}

async function setLightRoom(id, room, quiet = false) {   // the room of a lamp – true when saved
  const key = "lightroom:" + id;
  if (busy.has(key)) return false;
  busy.add(key);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  let ok = false;
  try {
    const res = await api("PUT", `api/lights/${encodeURIComponent(id)}/room`, { room: room || null });
    if (!quiet) toast(res.message || "Gespeichert.");
    ok = true;
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
  return ok;
}

function setLightColor(id, color) {   // „Beim Einschalten“ › Lichtfarbe – the light that is on stays as it is
  act("lightcolor:" + id, async () => toast((await api("PUT", `api/lights/${encodeURIComponent(id)}/color`, { color })).message));
}

function setLightLevel(id, level) {   // „Beim Einschalten“ › Helligkeit
  act("lightlevel:" + id, async () => toast((await api("PUT", `api/lights/${encodeURIComponent(id)}/level`,
    { level: level ? Number(level) : null })).message));
}

async function renameLight(id, quiet = false, name = null) {   // „Ändern …“ at the name – it stays in the app
  const l = (state.lights || []).find(x => x.id === id);
  if (!l) return false;
  const fallback = `Lampe ${id.slice(-4).toUpperCase()}`;   // as the server names it
  if (name !== null) {   // from „+ Gerät hinzufügen“
    try { const res = await api("PUT", `api/lights/${encodeURIComponent(id)}/name`, { name }); if (!quiet) toast(res.message); }
    catch (e) { toast(e.message, true); return false; }
    return true;
  }
  const done = await ask({ title: "Lampe umbenennen", text: `Leer lassen: „${fallback}“.`,
    fields: [{ id: "name", label: "Name", type: "text", value: l.name === fallback ? "" : l.name, placeholder: fallback }],
    buttons: [{ label: "Abbrechen", value: null }, { label: "Speichern", value: "submit", cls: "primary" }],
    onSubmit: async v => {
      try { toast((await api("PUT", `api/lights/${encodeURIComponent(id)}/name`, { name: v.name })).message); return null; }
      catch (e) { return e.message; }
    } });
  if (done) { invalidate(); await refresh(); }
  return !!done;
}

async function removeLight(id) {   // „Entfernen …“: the Pi unpairs it – the lamp itself stays as it is
  const l = (state.lights || []).find(x => x.id === id);
  if (!l) return;
  const yes = await ask({ title: "Lampe entfernen?",
    text: `Der Pi meldet ${l.name} im Zigbee-Netz ab. Die Lampe selbst bleibt, wie sie ist – an oder aus. Später kannst ` +
      "du sie mit „+ Gerät hinzufügen“ wieder anlernen.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  await zigbeeUnpair(id, "die Lampe", "sie");
  return refresh();
}

function sensorTileHTML(s, roomOptions) {
  const editingThis = sensorForm && sensorForm.mode === "edit" && sensorForm.id === s.id;
  const setupThis = !editingThis && pillForm && pillForm.at === s.id;
  const room = state.rooms.find(r => r.id === s.room);
  const reading = busy.has("sensor:" + s.id);
  const dot = s.online === false ? "offline" : s.online && !s.error ? "ok" : "";
  const since = s.online === false && s.offline_since ? ` (seit ${fmtTime(s.offline_since)})` : "";
  return `<div class="device ${reading ? "busy" : ""}" data-sensor="${esc(s.id)}">
    <div class="device-head"><span class="dot ${dot}"></span><strong class="dname">${esc(s.name)}</strong>
      <span class="muted">${esc(room ? room.name : "kein Raum")}</span>
      ${editingThis || setupThis ? "" : `<button class="small" data-action="sensor-refresh" ${reading ? "disabled" : ""}>${reading
        ? "liest …" : "Aktualisieren"}</button><button class="small" data-action="sensor-edit">Bearbeiten</button>`}</div>
    ${editingThis ? sensorFormHTML(sensorForm) : setupThis ? pillFormHTML(pillForm) : `<dl>
      <dt>Raum</dt><dd><select data-action="sensor-room" aria-label="Raum">${roomOptions(s.room)}</select></dd>
      <dt>Messwert</dt><dd>${s.temp == null ? "–" : esc(sensorText(s))}${s.measured_at
        ? ` <span class="muted">(gemessen ${esc(fmtTime(s.measured_at))})</span>` : ""}${s.error
        ? `<br><span class="error-text">${esc(s.error + since)}</span>` : ""}</dd>
      <dt>Gerät</dt><dd>${esc([s.model, s.device_name, s.host].filter(Boolean).join(" · ") || "–")}
        ${pillRestart[s.pill] ? '<span class="tag">startet neu …</span>'
          : `<button class="small" data-action="pill-setup" data-pill="${esc(s.pill || "")}">Einrichten</button>`}</dd>
      <dt>Fühlerart</dt><dd>${esc(pillModeText(s.mode))}${s.probe_number ? ` · Fühler ${s.probe_number}` : ""}</dd>
      ${s.update ? `<dt>Firmware</dt><dd>${esc(s.firmware || "–")} <span class="muted">(Update ${esc(s.update)} verfügbar –
        in der Shelly-App einspielen)</span></dd>` : ""}
      <dt>Kennung</dt><dd>${esc(s.id)}</dd>
    </dl>${sensorCompareHTML(s)}`}</div>`;
}

function sensorCompareHTML(s) {  // calibration, step 1: the sensor next to the heaters of its room, measured together
  const run = s.calibration ? calibRunHTML(s) : "";   // a run whose room has no heater any more: still to close
  if (!s.room) return `<div class="calib-compare"><p class="hint">Für den Vergleich mit den Heizungen den Fühler einem Raum zuordnen.</p>${run}</div>`;
  if (!s.compare.length) return `<div class="calib-compare"><p class="hint">In diesem Raum ist noch keine Heizung.</p>${run}</div>`;
  const rows = s.compare.map(c => {
    const verdict = c.deviation == null ? '<span class="muted">kein gemeinsamer Messwert</span>'
      : c.ok ? '<span class="tag ok">passt</span>'
      : `<span class="tag warn">zeigt ${c.deviation > 0 ? "zu viel" : "zu wenig"}</span>`;
    return `<tr><td>${esc(c.name)}</td><td class="num">${c.temp == null ? "–" : deg(c.temp)}</td>
      <td class="num">${c.deviation == null ? "–" : fmtDelta(c.deviation)}</td><td>${verdict}</td></tr>`;
  }).join("");
  const missing = s.compare.some(c => c.deviation == null);
  return `<div class="calib-compare">
    <h3>Kalibrierung</h3>
    <p class="hint">Messung ${s.measured_at ? esc(fmtTime(s.measured_at)) : "–"}: Fühler und Heizungen zum selben Zeitpunkt.
      Abweichung = Heizung − Fühler; bis ±${String(s.tolerance).replace(".", ",")} °C passt es.</p>
    <table><thead><tr><th></th><th class="num">Ist</th><th class="num">Abweichung</th><th></th></tr></thead>
      <tbody><tr class="ref"><td>Fühler</td><td class="num">${s.temp == null ? "–" : deg(s.temp)}</td><td></td><td></td></tr>${rows}</tbody></table>
    ${missing ? '<p class="hint">Fehlt ein Wert, kommt er mit der nächsten Messung (jede Minute) – oder gleich mit „Aktualisieren“.</p>' : ""}
    ${calibRunHTML(s)}
  </div>`;
}

// calibration, step 2: the „Kalibrierlauf“ – calm minutes over hours or days, then one proposal per heater
const CALIB_HOURS = [[6, "6 Stunden"], [12, "12 Stunden"], [24, "1 Tag (empfohlen)"], [48, "2 Tage"], [72, "3 Tage"]];
const CALIB_SKIPPED = { settling: "nach Umstellen", window: "Fenster offen", boost: "Boost", off: "ausgeschaltet",
                        offline: "Heizung offline" };

function fmtMinutes(min) {  // 135 -> "2 Std. 15 Min."
  const total = Math.max(0, Math.round(min)), h = Math.floor(total / 60), m = total % 60;
  return h ? `${h} Std.${m ? ` ${m} Min.` : ""}` : `${m} Min.`;
}

function fmtWhen(iso) {  // the server's local time as „heute 22:10 Uhr“, „morgen 08:00 Uhr“ or „23.11. 08:00 Uhr“
  const t = localTime(iso);
  if (!t) return "–";
  const day0 = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day0(t) - day0(serverNow())) / 86400000);
  const two = n => String(n).padStart(2, "0");
  const day = days === 0 ? "heute" : days === 1 ? "morgen" : days === -1 ? "gestern" : `${two(t.getDate())}.${two(t.getMonth() + 1)}.`;
  return `${day} ${two(t.getHours())}:${two(t.getMinutes())} Uhr`;
}

function skippedText(skipped) {  // „35 Min. nach Umstellen, 3 Min. Fenster offen“ – or ""
  return esc(Object.entries(skipped || {}).filter(([, n]) => n > 0)
    .map(([why, n]) => `${fmtMinutes(n)} ${CALIB_SKIPPED[why] || why}`).join(", "));
}

function spreadText(h, warn) {  // how far the middle half of the calm minutes lies apart
  const word = h.spread <= warn / 2 ? "gleichmäßig" : h.spread <= warn ? "schwanken etwas" : "schwanken stark";
  return `Messwerte ${word} <span class="nowrap">(${h.spread.toFixed(1).replace(".", ",")} °C)</span>`;
}

function calibRunHTML(s) {
  const c = s.calibration, rules = state.calib_rules || {};
  const working = busy.has("crun:" + s.id), off = working ? "disabled" : "";
  const settle = rules.settle_minutes ? ` und frühestens ${fmtMinutes(rules.settle_minutes)} nach dem letzten Umstellen` : "";
  if (!c) {
    const hours = calibHours[s.id] || rules.hours || 24;
    const options = CALIB_HOURS.filter(([h]) => h <= (rules.max_hours || 72))
      .map(([h, label]) => `<option value="${h}" ${h === hours ? "selected" : ""}>${label}</option>`).join("");
    return `<div class="calib-run">
      <h4>Kalibrierlauf</h4>
      <p class="hint">Genauer als ein einzelner Vergleich: Die App misst eine Zeit lang jede Minute den Fühler und die
        Heizungen dieses Raums und schlägt danach für jede Heizung eine Korrektur vor. Es zählen nur ruhige Minuten –
        Fenster zu, kein Boost, nicht ausgeschaltet${settle}. Den Fühler dafür in die Raummitte auf 1–1,5 m Höhe legen,
        nicht in die Sonne und nicht neben Fenster oder Heizung.</p>
      <div class="row">
        <label class="inline">Dauer <select data-action="crun-hours" ${off}>${options}</select></label>
        <button class="primary" data-action="crun-start" ${off}>${working ? "starte …" : "Kalibrierlauf starten"}</button>
      </div>
    </div>`;
  }
  if (c.running) {
    const left = (localTime(c.until) - serverNow()) / 60000;
    const rows = c.heaters.map(h => {
      const dev = h.estimate == null || h.current == null ? null : Math.round((h.current - h.estimate) * 10) / 10;
      const verdict = dev == null ? '<span class="muted">noch kein Wert</span>'
        : `<span class="nowrap">${fmtDelta(dev)}</span> ${Math.abs(dev) <= s.tolerance + 1e-9 ? '<span class="tag ok">passt</span>'
          : `<span class="tag warn">zeigt ${dev > 0 ? "zu viel" : "zu wenig"}</span>`}`;
      const skipped = skippedText(h.skipped);
      return `<li><div class="calib-line"><b>${esc(h.name)}</b><span>${verdict}</span></div>
        <div class="muted">${h.minutes ? `${fmtMinutes(h.minutes)} ruhig gemessen` : "noch keine ruhige Minute"}${h.proposal != null
          ? " – genug für einen Vorschlag" : ""}${skipped ? `; nicht gezählt: ${skipped}` : ""}</div></li>`;
    }).join("");
    return `<div class="calib-run">
      <h4>Kalibrierlauf läuft</h4>
      <p class="hint">Seit ${esc(fmtWhen(c.started))}, bis ${esc(fmtWhen(c.until))}${left > 0 ? ` (noch ${fmtMinutes(left)})` : ""}.
        Die Heizungen heizen dabei ganz normal; geändert wird an ihnen nichts.</p>
      <ul class="calib-list">${rows}</ul>
      <p class="hint">Abweichung bisher = Heizung − Fühler, der mittlere Wert aller ruhigen Minuten.${c.missing
        ? ` Der Fühler hatte ${fmtMinutes(c.missing)} lang keinen Messwert.` : ""}</p>
      <div class="row">
        <button data-action="crun-finish" ${c.can_finish && !working ? "" : "disabled"}>Jetzt auswerten</button>
        <button data-action="crun-discard" ${off}>Abbrechen</button>
      </div>
      ${c.can_finish ? "" : `<p class="hint">„Jetzt auswerten“ geht, sobald eine Heizung ${fmtMinutes(rules.min_minutes ?? 120)} ruhig gemessen ist.</p>`}
    </div>`;
  }
  const warn = rules.spread_warn ?? 1;
  const rows = c.heaters.map(h => {
    const corr = h.applied ? `Korrektur <span class="nowrap">${fmtDelta(h.applied.old)}</span> → <b class="nowrap">${fmtDelta(h.applied.new)}</b>`
      : h.proposal == null ? "" : `Korrektur <span class="nowrap">${h.current == null ? "–" : fmtDelta(h.current)}</span> → <b class="nowrap">${fmtDelta(h.proposal)}</b>`;
    const action = h.applied ? '<span class="tag ok">übernommen</span>'
      : h.can_apply && h.change === 0 ? '<span class="tag ok">passt schon</span>'
      : h.can_apply ? `<button class="small primary" data-action="crun-apply" data-heater="${esc(h.id)}" ${off}>Übernehmen</button>` : "";
    return `<li><div class="calib-line"><b>${esc(h.name)}</b>${action}</div>
      ${corr ? `<div>${corr}</div>` : ""}
      <div class="muted">${h.minutes ? `${fmtMinutes(h.minutes)} ruhig gemessen` : "keine ruhige Minute"}${h.spread != null
        ? `, ${spreadText(h, warn)}` : ""}</div>
      ${!h.applied && h.why ? `<div class="${h.proposal == null ? "muted" : "warn-line"}">${esc(h.why)}</div>` : ""}</li>`;
  }).join("");
  const shaky = c.heaters.some(h => h.uncertain && !h.applied);
  return `<div class="calib-run">
    <h4>Kalibrierlauf ausgewertet</h4>
    <p class="hint">Gemessen ${esc(fmtWhen(c.started))} bis ${esc(fmtWhen(c.finished))}. Vorschlag je Heizung = die Korrektur,
      mit der sie im Mittel (Median) aller ruhigen Minuten genau den Wert des Fühlers zeigt – einzelne Ausreißer, etwa Sonne
      auf dem Fühler, zählen kaum. In die Heizung kommt er erst mit „Übernehmen“.</p>
    <ul class="calib-list">${rows}</ul>
    ${shaky ? `<p class="hint warn">Schwanken die Messwerte stark (mittlere Hälfte über ${String(warn).replace(".", ",")} °C auseinander),
      ist der Vorschlag unsicher: lieber den Platz des Fühlers prüfen (Sonne, Zugluft, Nähe zur Heizung) und noch einmal messen.</p>` : ""}
    <div class="row"><button data-action="crun-discard" ${off}>Schließen</button></div>
  </div>`;
}

async function calibRun(sensorId, what, heaterId) {  // start, finish („Jetzt auswerten“), discard (abbrechen/schließen), apply
  const s = (state.sensors || []).find(x => x.id === sensorId), c = s && s.calibration, key = "crun:" + sensorId;
  if (!s || busy.has(key)) return;
  const base = `api/sensors/${encodeURIComponent(sensorId)}/calibration`;
  let method = "POST", url = base, body;
  if (what === "start") {
    body = { hours: calibHours[sensorId] || (state.calib_rules || {}).hours || 24 };
  } else if (what === "finish") {
    url = base + "/finish";
  } else if (what === "discard") {
    method = "DELETE";
    if (c && (c.running || c.heaters.some(h => h.can_apply && h.change !== 0))) {
      const yes = await ask(c.running
        ? { title: "Kalibrierlauf abbrechen?", text: "Die bisherigen Messungen gehen verloren. An den Heizungen ändert sich nichts.",
            buttons: [{ label: "Weiter messen", value: null }, { label: "Ja, abbrechen", value: "yes", cls: "danger-solid" }] }
        : { title: "Ergebnis schließen?", text: "Vorschläge, die noch nicht übernommen sind, gehen verloren.",
            buttons: [{ label: "Nein", value: null }, { label: "Ja, schließen", value: "yes", cls: "danger-solid" }] });
      if (!yes) return;
    }
  } else if (what === "apply") {
    const h = c && c.heaters.find(x => x.id === heaterId);
    if (!h || !h.can_apply) return;
    const diff = Math.round((h.proposal - (h.current ?? 0)) * 10) / 10;
    const yes = await ask({ title: `${h.name}: Korrektur übernehmen?`,
      text: `Die Korrektur der Heizung ändert sich von ${fmtDelta(h.current ?? 0)} auf ${fmtDelta(h.proposal)} – sie zeigt danach ` +
        `${Math.abs(diff).toFixed(1).replace(".", ",")} °C ${diff < 0 ? "weniger" : "mehr"} an, wie der Fühler. ` +
        "Ändern lässt sich das jederzeit wieder unter „Kalibrieren“ bei der Heizung.",
      buttons: [{ label: "Abbrechen", value: null }, { label: "Übernehmen", value: "yes", cls: "primary" }] });
    if (!yes) return;
    url = base + "/apply";
    body = { heater: heaterId };
  }
  busy.add(key);
  render();
  try {
    toast((await api(method, url, body)).message);
    if (what === "start") delete calibHours[sensorId];
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
}

async function refreshSensor(id, quiet = false) {  // read now – the Pill never sleeps, so this is one request
  const key = "sensor:" + id;
  if (busy.has(key)) return;
  busy.add(key);
  render();
  try {
    const res = await api("POST", `api/sensors/${encodeURIComponent(id)}/refresh`);
    if (!quiet) toast(res.message);
  } catch (e) {
    if (!quiet) toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  invalidate();
  await refresh();
}

function pillFormHTML(f) {
  const working = busy.has("pillform");
  const choices = Object.entries(PILL_MODES).map(([mode, label]) => `<label class="check"><input type="radio"
      name="pill-mode" data-pfield="mode" value="${mode}" ${f.mode === mode ? "checked" : ""}> ${esc(label)}</label>`).join("");
  return `<div class="device-form pill-form">
    <h3>Pill einrichten</h3>
    <p class="hint">Was steckt in der Pill? Die App stellt die Pill darauf ein. Nach einem Wechsel melden gespeicherte
      Fühler dieser Pill womöglich nichts mehr.</p>
    ${choices}
    <div class="row">
      <button class="primary" data-action="pill-mode-save" ${working ? "disabled" : ""}>${working ? "einen Moment …" : "Speichern"}</button>
      <button data-action="pill-restart" ${working ? "disabled" : ""}>Pill neu starten</button>
      <button data-action="pill-cancel">Abbrechen</button>
    </div>
    <p class="hint">Fühler eingesteckt, aber nicht erkannt? „Pill neu starten“ – nach etwa 15 Sekunden sieht die App
      wieder nach.</p>
  </div>`;
}

function openPillForm(pillId, at) {  // at: "search" (search result) or the id of the saved sensor whose tile shows it
  const p = foundPill(pillId) || (state.sensors || []).find(s => s.pill === pillId);
  if (!p) return;
  sensorForm = null;
  pillForm = { pill: pillId, at, mode: ["onewire", "dht22"].includes(p.mode) ? p.mode : "onewire" };
  invalidate();
  render();
}

async function pillAction(kind) {  // writes to the Pill: what is plugged in, or a restart
  const f = pillForm;
  if (!f || busy.has("pillform")) return;
  if (kind === "restart") {
    const yes = await ask({ title: "Pill neu starten?", text: "Die Pill ist etwa 15 Sekunden nicht erreichbar und sucht dabei die eingesteckten Fühler.",
      buttons: [{ label: "Nein", value: null }, { label: "Ja, neu starten", value: "yes", cls: "primary" }] });
    if (!yes) return;
  }
  busy.add("pillform");
  render();
  let res = null;
  try {
    res = await api("POST", `api/pills/${encodeURIComponent(f.pill)}/${kind === "mode" ? "mode" : "restart"}`,
      kind === "mode" ? { mode: f.mode } : {});
    pillForm = null;
    toast(res.message);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete("pillform");
  }
  invalidate();
  if (res) await lookAgain(f.pill, res.restart);
  else render();
}

async function lookAgain(pillId, restarting) {  // after „Einrichten“: search again, or read its saved sensors again
  if (restarting) {
    pillRestart[pillId] = true;
    if (sensorSearch) sensorSearch = { running: true, waiting: true };
    render();
    await new Promise(r => setTimeout(r, PILL_RESTART_MS));
    delete pillRestart[pillId];
    if (view !== "setup") return;
  }
  if (sensorSearch) return runSensorSearch();
  for (const s of (state.sensors || []).filter(x => x.pill === pillId)) await refreshSensor(s.id, true);
  render();
}

function sensorFormHTML(f) {
  const saving = busy.has("sensorform");
  const rooms = ['<option value="">– kein Raum –</option>'].concat(state.rooms.map(r =>
    `<option value="${esc(r.id)}" ${r.id === f.room ? "selected" : ""}>${esc(r.name)}</option>`)).join("");
  return `<div class="device-form sensor-form">
    ${f.mode === "new" ? `<h3>Fühler übernehmen</h3><p class="hint">${esc(f.placeholder)} · ${esc(f.where)}</p>` : ""}
    <label>Name<input data-sfield="name" value="${esc(f.name)}" placeholder="${esc(f.placeholder)}" maxlength="40"
      autocomplete="off"></label>
    <label>Raum<select data-sfield="room">${rooms}</select></label>
    ${state.rooms.length ? "" : '<p class="hint">Noch kein Raum angelegt – Räume legst du im Tab „Räume“ über „+ Raum“ an.</p>'}
    <div class="row">
      <button class="primary" data-action="sensor-save" ${saving ? "disabled" : ""}>${saving ? "speichere …" : "Speichern"}</button>
      <button data-action="sensor-cancel">Abbrechen</button>
      ${f.mode === "edit" ? '<button class="danger" data-action="sensor-delete">Fühler entfernen</button>' : ""}
    </div>
  </div>`;
}

function openSensorForm(sensorId, pillId = null, channel = null) {
  pillForm = null;
  if (sensorId) {
    const s = (state.sensors || []).find(x => x.id === sensorId);
    if (!s) return;
    sensorForm = { mode: "edit", id: s.id, name: s.custom_name, room: s.room || "",
                   placeholder: (s.device_name || s.model || s.id) + (s.probe_number ? ` · Fühler ${s.probe_number}` : "") };
  } else {
    const p = foundPill(pillId);
    const i = p ? p.probes.findIndex(x => x.channel === channel) : -1;
    if (i < 0) return;
    sensorForm = { mode: "new", id: `${p.id}-${channel}`, pill: p.id, channel, name: "", room: addHome(), where: p.address,
                   placeholder: (p.name || p.model) + (p.probes.length > 1 ? ` · Fühler ${i + 1}` : "") };
  }
  invalidate();
  render();
  const input = document.querySelector('.sensor-form input[data-sfield="name"]');
  if (input) { input.focus(); input.scrollIntoView({ block: "center", behavior: "smooth" }); }
}

async function saveSensor() {
  const f = sensorForm;
  if (!f || busy.has("sensorform")) return;
  const body = { name: f.name, room: f.room || null };
  busy.add("sensorform");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  try {
    const res = f.mode === "new" ? await api("POST", "api/sensors", Object.assign({ pill: f.pill, channel: f.channel }, body))
                                 : await api("PATCH", `api/sensors/${encodeURIComponent(f.id)}`, body);
    sensorForm = null;
    toast(res.message || "Gespeichert.");
    if (f.mode === "new" && addFlow) {
      deviceTab = "sensors";
      localStorage.setItem("heizung.deviceTab", deviceTab);
      closeAdd();
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete("sensorform");
  }
  invalidate();
  await refresh();
}

async function deleteSensor() {
  const f = sensorForm;
  if (!f) return;
  const yes = await ask({ title: "Fühler entfernen?", text: "Die App vergisst den Fühler. An der Pill selbst ändert sich nichts.",
    buttons: [{ label: "Nein", value: null }, { label: "Ja, entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  try {
    const res = await api("DELETE", `api/sensors/${encodeURIComponent(f.id)}`);
    sensorForm = null;
    toast(res.message || "Entfernt.");
  } catch (e) {
    toast(e.message, true);
  }
  invalidate();
  await refresh();
}

function thermostatFactsHTML(h, roomOptions) {  // a SONOFF thermostat over Zigbee: no address, token or clock
  const signal = zigbeeSignal(h.signal);
  const battery = h.battery == null ? "" : ` · Batterie ${h.battery} %${h.battery_low ? ' <span class="error-text">– bald wechseln</span>' : ""}`;
  const others = (h.faults || []).filter(f => !f.startsWith("Batterie") || !h.battery_low);
  return `<dl>
    <dt>Raum</dt><dd><select data-action="heater-room" aria-label="Raum">${roomOptions(h.room)}</select></dd>
    <dt>Verbindung</dt><dd>Zigbee${battery}${signal ? " · " + esc(signal) : ""}</dd>
    <dt>Status</dt><dd>${h.online ? "online" : "offline"}${h.error ? ` <span class="error-text">${esc(h.error)}</span>` : ""}</dd>
    ${others.length ? `<dt>Störung</dt><dd class="error-text">${esc(others.join(", "))}</dd>` : ""}
    <dt>Kalibrierung</dt><dd>${h.temp_corr == null ? "–" : `Korrektur ${fmtDelta(h.temp_corr)}`}
      ${h.online ? '<button class="small" data-action="calib-open">Kalibrieren</button>' : ""}</dd>
    <dt>Heizpläne</dt><dd>${h.plan_slot ? `Plan ${h.plan_slot} ausgewählt${h.mode === "auto" ? ", Automatik läuft" : ""}` : "–"}
      <span class="muted">· den Heizplan bekommt es über seinen Raum („Räume“ › Senden)</span></dd>
    ${regulationHTML(h)}
    <dt>Gemeldet</dt><dd>${h.last_seen ? esc(fmtTime(h.last_seen)) + " Uhr" : "–"}</dd>
    <dt>Zigbee-Adresse</dt><dd>${esc(h.serial ?? "–")}</dd>
    ${firmwareHTML(h)}
  </dl>`;
}

const STUFEN = [20, 40, 60, 80, 100];   // Stufe 1 … 5: per cent the valve opens at most while heating (heizapp.trv.STEPS)
const GAPS = [0, 5, 10, 15];            // per cent it stays open in the pauses – always less than Stufe 1
const SPANS = [1, 0.6, 0.4, 0.2];       // °C below the setpoint at which it heats again (1 = as from the factory)

function regulationHTML(h) {
  // „Regelung“: three buttons like Aus/Automatik/Ein, the chosen one marked – „Ab Werk“ (fully open until warm, then
  // closed), „Sanft“ (the thermostat's own smart control) or „Stufen“ (open and closed, but only as far as the Stufe
  // says, a gap open in the pauses, heating again sooner – three more rows of buttons); a short line says what it does
  const r = h.regulation || {}, sending = busy.has("regulation:" + h.id);
  const button = (active, data, label) => `<button class="${active ? "active" : ""}" data-action="trv-regulation" ${data}
    aria-pressed="${active}" ${h.online && !sending ? "" : "disabled"}>${label}</button>`;
  const row = (label, buttons, text) => `<dt>${label}</dt><dd><div class="segmented" role="group" aria-label="${label}">${
    buttons}</div>${text ? `<div class="segmented-text muted">${esc(text)}</div>` : ""}</dd>`;
  const comma = x => String(x).replace(".", ",");
  const text = sending ? "wird gesendet …"
    : r.mode === "werk" ? "Ganz auf, bis es warm ist, dann ganz zu."
    : r.mode === "sanft" ? "Öffnet das Ventil nur so weit wie nötig – wie ein normaler Thermostatkopf."
    : r.mode === "stufen" ? "Auf und zu wie ab Werk, aber sanfter. Die passenden Werte zeigt das Ausprobieren."
    : "Das Thermostat hat noch nicht gemeldet, wie es regelt.";
  let html = row("Regelung", [["werk", "Ab Werk"], ["sanft", "Sanft"], ["stufen", "Stufen"]]
    .map(([mode, label]) => button(r.mode === mode, `data-mode="${mode}"`, label)).join(""), text);
  if (r.mode === "stufen") {
    html += row("Stufe", STUFEN.map((p, i) => button(r.heat === p, `data-heat="${p}"`, i + 1)).join(""),
        r.heat == null ? "" : `Beim Heizen höchstens ${r.heat} % offen.`)
      + row("In Pausen", GAPS.map(p => button(r.idle === p, `data-idle="${p}"`, p ? `${p} %` : "zu")).join(""),
        r.idle == null ? "" : r.idle ? `Bleibt ${r.idle} % offen, der Heizkörper kühlt nicht ganz aus.` : "Ganz zu, wie ab Werk.")
      + row("Spielraum", SPANS.map(s => button(r.spielraum === s, `data-spielraum="${s}"`, comma(s))).join(""),
        r.spielraum == null ? "" : `Heizt wieder, sobald es ${comma(r.spielraum)} °C kälter ist als eingestellt.`);
  }
  return html;
}

const OTA_BATTERY = 70;   // per cent the battery needs for a firmware update (heizapp.trv.OTA_BATTERY)

function firmwareHTML(h) {  // the firmware and its update: start, progress (reported every 30 s), result
  const o = h.ota, running = o && o.state === "running";
  let more = "";
  if (running) {
    more = ` · <b>neue Firmware wird eingespielt</b>${o.progress != null ? ` – ${Math.floor(o.progress)} %` : " – startet …"}${
      o.remaining ? `, noch etwa ${Math.max(1, Math.round(o.remaining / 60))} min` : ""}
      <span class="muted">(das Thermostat heizt dabei weiter)</span>`;
  } else if (o && o.state === "done") {
    const r = h.restore;   // what the thermostat forgot with the update comes back a minute later
    more = " · ✓ neue Firmware eingespielt" + (!r ? ""
      : r.state === "waiting" ? ' <span class="muted">· die Einstellungen von vorher kommen gleich wieder …</span>'
      : r.state === "done" ? ` <span class="muted">· Einstellungen von vorher wieder eingestellt${r.text ? ` (${esc(r.text)})` : ""}</span>`
      : ` · <span class="error-text">${esc(r.text)}</span>`);
  } else if (o && o.state === "failed") {
    more = ` · <span class="error-text">Einspielen hat nicht geklappt: ${esc(o.message)}</span>`;
  }
  if (h.update && !running) {
    const other = state.heaters.find(x => x.id !== h.id && x.ota && x.ota.state === "running");
    const why = !h.online ? "einspielen, sobald es erreichbar ist"
      : h.battery == null || h.battery < OTA_BATTERY ? `zum Einspielen braucht die Batterie mindestens ${OTA_BATTERY} %`
      : other ? `einspielen, wenn „${other.name}“ fertig ist` : "";
    more += ` · Update verfügbar ${why ? `<span class="muted">(${esc(why)})</span>` : `<button class="small"
      data-action="trv-firmware">${o && o.state === "failed" ? "Noch einmal versuchen" : "Firmware einspielen …"}</button>`}`;
  }
  return `<dt>Firmware</dt><dd>${esc(h.firmware ?? "–")}${more}</dd>`;
}

async function setRegulation(heaterId, data) {
  // data: the button's data-mode („werk“, „sanft“, „stufen“) – or in Stufen one of data-heat, data-idle, data-spielraum
  const key = "regulation:" + heaterId;
  if (busy.has(key)) return;
  const body = { mode: data.mode || "stufen" };
  for (const name of ["heat", "idle", "spielraum"]) if (data[name] != null) body[name] = Number(data[name]);
  busy.add(key);
  render();
  try {
    toast((await api("POST", `api/heaters/${encodeURIComponent(heaterId)}/regulation`, body)).message || "Gespeichert.");
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy.delete(key);
  }
  await refresh();
}

async function startFirmware(heaterId) {
  const h = state.heaters.find(x => x.id === heaterId);
  if (!h) return;
  const yes = await ask({ title: "Neue Firmware einspielen?",
    text: `„${h.name}“ bekommt eine neue Firmware über Funk. Das dauert 10 bis 100 Minuten. Das Thermostat heizt dabei ` +
          "weiter und startet am Ende kurz neu. Bitte in der Zeit die Batterien nicht wechseln.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Einspielen", value: "yes", cls: "primary" }] });
  if (!yes) return;
  try {
    toast((await api("POST", `api/heaters/${encodeURIComponent(heaterId)}/firmware`)).message || "Gestartet.");
  } catch (e) {
    toast(e.message, true);
  }
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

function heaterTileHTML(h, roomOptions) {
  const editingThis = deviceForm && deviceForm.mode === "edit" && deviceForm.id === h.id;
  const calibThis = !editingThis && calibForm && calibForm.id === h.id;
  return `<div class="device ${busy.has("dev:" + h.id) ? "busy" : ""}" data-heater="${esc(h.id)}">
    <div class="device-head"><span class="dot ${h.online ? "ok" : "offline"}"></span>
      <strong class="dname">${esc(h.name)}</strong>${radioTag(h.kind === "zigbee")}
      ${h.custom_name && h.hostname ? `<span class="muted">${esc(h.hostname)}</span>` : ""}
      ${editingThis || calibThis ? "" : '<button class="small" data-action="dev-edit">Bearbeiten</button>'}</div>
    ${editingThis ? deviceFormHTML(deviceForm) : calibThis ? calibFormHTML(h)
      : h.kind === "zigbee" ? thermostatFactsHTML(h, roomOptions) : `<dl>
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
}

function heatersCardHTML(roomOptions) {   // AeroFlow heaters (WLAN) and SONOFF thermostats (Zigbee)
  const visible = state.heaters.filter(h => deviceFilter === "all" || (deviceFilter === "none" ? !h.room : h.room === deviceFilter));
  const empty = !visible.length && state.heaters.length ? `<p class="empty">${deviceFilter === "none"
    ? "Alle Heizungen sind einem Raum zugeordnet." : "Diesem Raum ist keine Heizung zugeordnet."}</p>` : "";
  const thermostats = state.app.zigbee || state.heaters.some(h => h.kind === "zigbee");
  return `<article class="card"><div class="card-head"><h2>${thermostats ? "Heizungen und Thermostate" : "Heizungen"}</h2>
      ${state.heaters.some(h => h.kind !== "zigbee") ? '<button class="small" data-action="clock-all">Alle Uhren stellen</button>' : ""}</div>
    <p class="hint">Die App stellt die Uhren der AeroFlow-Heizungen selbst: beim Start, bei mehr als 1 Minute Abweichung und
      täglich um 3 Uhr (deckt auch Sommer-/Winterzeit ab). Ist eine Heizung länger nicht erreichbar, sucht sie sie im Netz
      und übernimmt eine neue Adresse selbst. Räume legst du im Tab „Räume“ über „+ Raum“ an.</p>
    ${state.heaters.length ? deviceFilterBar() : '<p class="empty">Noch keine Heizung in der App.</p>'}
    ${visible.map(h => heaterTileHTML(h, roomOptions)).join("")}${empty}
  </article>`;
}

function sensorsCardHTML(roomOptions) {
  const sensors = state.sensors || [];
  return `<article class="card"><div class="card-head"><h2>Raumfühler</h2></div>
    <p class="hint">Als Raumfühler dient die <b>Shelly Pill</b> am USB-Netzteil, mit eingestecktem Temperaturfühler
      (DS18B20) oder einem DHT22. Sie schläft nie – die App liest sie jede Minute.</p>
    ${sensors.map(s => sensorTileHTML(s, roomOptions)).join("")}
    ${sensors.length ? "" : '<p class="empty">Noch kein Fühler gespeichert.</p>'}
  </article>`;
}

function zigbeeOthersHTML() {   // in the Zigbee network, but no thermostat, plug or lamp of the app: being paired, failed, unknown
  if (!(state.app && state.app.zigbee)) return "";
  const shown = new Set(state.heaters.map(h => h.id).concat((state.plugs || []).map(p => p.id),
                                                            (state.lights || []).map(l => l.id),
                                                            (state.buttons || []).map(b => b.id),
                                                            (state.detectors || []).map(x => x.id)));
  const others = ((zigbee && zigbee.net && zigbee.net.devices) || []).filter(d => !shown.has(d.ieee));
  if (!others.length) return "";
  return `<article class="card"><div class="card-head"><h2>Weitere Zigbee-Geräte</h2></div>
    <p class="hint">Diese Geräte sind im Zigbee-Netz, aber (noch) kein Thermostat, keine Steckdose, keine Lampe, kein
      Schalter und kein Bewegungsmelder der App.</p>
    <ul class="net-list zigbee-list">${others.map(zigbeeDeviceHTML).join("")}</ul>
  </article>`;
}

function renderDevices(el) {   // every device by its kind – new ones come with „+ Gerät hinzufügen“
  el.className = "tab single";
  if (deviceFilter !== "all" && deviceFilter !== "none" && !state.rooms.some(r => r.id === deviceFilter)) {
    deviceFilter = "all";  // the filtered room was deleted
  }
  const roomOptions = roomOptionsHTML;
  if (addFlow && addFlow.home == null) return setHTML(el, addFlowHTML(roomOptions));
  const subtab = (id, label, count) => `<button data-subtab="${id}" role="tab" aria-selected="${deviceTab === id}">${label}
      <span class="count">${count}</span></button>`;
  const lamps = !!(state.app && state.app.zigbee) || (state.lights || []).length > 0;   // light needs Zigbee
  if (deviceTab === "lights" && !lamps) deviceTab = "heaters";
  const bar = `<div class="devices-bar"><div class="subtabs" role="tablist" aria-label="Geräteart">${subtab("heaters", "Heizungen",
      state.heaters.length)}${subtab("sensors", "Raumfühler", (state.sensors || []).length)}${subtab("plugs", "Steckdosen",
      (state.plugs || []).length)}${lamps ? subtab("lights", "Licht", (state.lights || []).length
      + (state.buttons || []).length + (state.detectors || []).length) : ""}</div>
    <button class="primary" data-action="add-open">+ Gerät hinzufügen</button></div>`;
  const list = deviceTab === "plugs" ? plugsCardHTML() : deviceTab === "sensors" ? sensorsCardHTML(roomOptions)
    : deviceTab === "lights" ? lightsCardHTML() : heatersCardHTML(roomOptions);
  setHTML(el, bar + list + zigbeeOthersHTML());
}

// ------------------------------------------------------------------ „+ Gerät hinzufügen“
// 1 Was? – 2 So geht's: a short guide, and the app searches by itself (in the WLAN, over Zigbee or both) – 3 Gefunden:
// „Übernehmen“ (a Zigbee device joins by itself) – 4 Wohin?: the room, for a plug its heater
const ADD_STEPS = ["Was?", "So geht’s", "Gefunden", "Wohin?"];
const THERMOSTAT_STEPS = ["Batterien ins Thermostat legen, falls sie noch nicht drin sind.",
  "Das Drehrad unter 5 °C drehen, bis das Thermostat aus ist.",
  "Die mittlere Taste 2 Sekunden drücken, bis das Verbindungszeichen blinkt.",
  "Warten: Nach etwa einer Minute erscheint es hier."];
const SHELLY_STEPS = ["Die Steckdose in die Wanddose stecken, an der die Heizung hängt, und den Stecker der Heizung in die Steckdose.",
  "Eine neue Steckdose öffnet ein eigenes WLAN, dessen Name mit „Shelly“ beginnt. Sonst in der ersten Minute nach dem " +
  "Einstecken die Taste 5 Sekunden halten, bis sie blau blinkt.",
  "Mit dem Handy in dieses WLAN gehen, im Browser 192.168.33.1 öffnen und dort das Heim-WLAN mit seinem Passwort eintragen.",
  "Das Handy wieder ins Heim-WLAN bringen und hierher zurückkommen – die App sucht dann von selbst. Bei der Steckdose " +
  "„Übernehmen“ drücken; ist sie noch nicht dabei, „Erneut suchen“."];
const LIGHT_STEPS = ["Die Lampe einschalten – der Lichtschalter an der Wand bleibt an. Beim Anlernen sollte sie nicht " +
  "weit vom Pi weg sein.",
  "Die Lampe zurücksetzen, dann sucht sie ein neues Netz:<ul class=\"maker-steps\">" +
  "<li><b>Hue:</b> in der Hue-App löschen. Ohne Hue-App: 5-mal hintereinander 2 Sekunden aus und 8 Sekunden an.</li>" +
  "<li><b>IKEA FLOALT:</b> 6-mal kurz hintereinander aus- und wieder einschalten.</li>" +
  "<li><b>IKEA JETSTRÖM:</b> die Reset-Taste am Netzteil unter der Abdeckung drücken.</li>" +
  "<li><b>Innr:</b> so, wie es in der Anleitung von Innr steht.</li></ul>",
  "Warten: Nach etwa einer Minute erscheint sie hier."];
const BUTTON_STEPS = ["Den Schalter zum Anlernen nah an den Pi bringen.",
  "Den Schalter in den Anlern-Modus bringen:<ul class=\"maker-steps\">" +
  "<li><b>IKEA RODRET:</b> die hintere Abdeckung abnehmen und die kleine Taste neben der Batterie 4-mal drücken – " +
  "das rote Licht blinkt. Klappt es nicht: zuerst 6-mal „an“ drücken, dann wieder 4-mal die kleine Taste.</li>" +
  "<li><b>Hue Smart Button:</b> die hintere Abdeckung abnehmen und die Taste darunter kurz drücken – das Licht blinkt " +
  "langsam orange. War er schon angelernt: die Taste 10 Sekunden halten.</li>" +
  "<li><b>Hue Dimmschalter:</b> die Batterieklappe hinten öffnen und die Setup-Taste 10 Sekunden halten, bis das Licht " +
  "rot blinkt.</li></ul>",
  "Warten: Nach etwa einer Minute erscheint er hier."];
const DETECTOR_STEPS = ["Batterien in den Bewegungsmelder legen und ihn zum Anlernen nah an den Pi bringen.",
  "Den Melder in den Anlern-Modus bringen:<ul class=\"maker-steps\">" +
  "<li><b>Hue Bewegungsmelder:</b> die Setup-Taste hinten 3 bis 5 Sekunden halten, bis das Licht rot blinkt. Ein " +
  "älterer (SML001 – steht hinten drauf): danach die Taste noch einmal halten, bis es orange blinkt.</li>" +
  "<li><b>IKEA VALLHORN:</b> die Kopplungstaste 4-mal kurz drücken – das rote Licht vorn blinkt.</li></ul>",
  "Warten: Nach etwa einer Minute erscheint er hier. Ein Hue-Melder blinkt dann grün."];
const SONOFF_STEPS = ["Die Steckdose in die Wanddose stecken, an der die Heizung hängt, und den Stecker der Heizung in die Steckdose.",
  "Die Taste an der Steckdose 5 Sekunden halten, bis das blaue Licht langsam blinkt.",
  "Warten: Nach etwa einer Minute erscheint sie hier."];

function zigbeeReady() { return !!(zigbee && zigbee.supported && zigbee.net && zigbee.net.bridge === "online"); }
function zigbeeSearching() { return !!(zigbee && zigbee.net && zigbee.net.search && zigbee.net.search.active); }
function zigbeeAddresses() { return new Set(((zigbee && zigbee.net && zigbee.net.devices) || []).map(d => d.ieee)); }
function addNewZigbee() {   // the Zigbee devices that came since the guide opened
  const f = addFlow;
  return ((zigbee && zigbee.net && zigbee.net.devices) || []).filter(d => f && f.before && !f.before.has(d.ieee));
}

function addKinds() {   // what can be added here: a thermostat only on a Pi with Zigbee
  const zig = !!(state.app && state.app.zigbee);
  return [["heater", "Heizung", "AeroFlow-Heizung mit FlexiSmart-Pro-Regler – im WLAN"]]
    .concat(zig ? [["thermostat", "Thermostat", "SONOFF-Thermostat für einen Heizkörper – funkt über Zigbee"]] : [])
    .concat([["sensor", "Raumfühler", "Shelly Pill mit Temperaturfühler – im WLAN"],
             ["plug", "Steckdose", `mit Strommessung, für eine AeroFlow oder ein anderes Gerät – Shelly im WLAN${zig ? " oder SONOFF über Zigbee" : ""}`]])
    .concat(zig ? [["light", "Lampe", "Zigbee-Lampe von Hue, IKEA, Innr und anderen – direkt am Pi, ohne Bridge"],
                   ["button", "Schalter", "Funk-Schalter für das Licht – IKEA RODRET, Hue Smart Button, Hue Dimmschalter"],
                   ["detector", "Bewegungsmelder", "schaltet das Licht bei Bewegung – Hue Bewegungsmelder, IKEA VALLHORN"]] : []);
}

function addStep() {   // 0 … 3 for the steps above the guide
  const f = addFlow;
  if (f.step === "what") return 0;
  if (f.step === "where" || (sensorForm && sensorForm.mode === "new")) return 3;
  const found = f.kind === "heater" ? !!(deviceForm && deviceForm.mode === "new") || !!(scan && scan.data && scan.data.heaters.length)
    : f.kind === "sensor" ? !!(sensorSearch && sensorSearch.data && sensorSearch.data.pills.length)
    : addNewZigbee().length > 0 || (f.kind === "plug" && !!(plugSearch && plugSearch.data && plugSearch.data.plugs.length));
  return found ? 2 : 1;
}

function addHowHTML(steps, title = "So geht’s") {
  return `<div class="add-how"><h3>${title}</h3><ol class="zigbee-steps">${steps.map(s => `<li>${s}</li>`).join("")}</ol></div>`;
}

function zigbeeMissingHTML() {
  if (zigbeeStarting()) return '<p class="searching">Zigbee startet gerade – einen Moment …</p>';
  return `<div class="wifi-note"><p><b>Dafür braucht der Pi zuerst den Zigbee-Funk.</b></p>
    <p>Das richtest du einmal unter „Verbindungen“ › „Zigbee-Funk“ ein: Zigbee-Stick einstecken, die Zusatzprogramme
      installieren und einen Funkkanal wählen. Danach geht es hier weiter.</p>
    <button class="small primary" data-action="add-to-zigbee">Zu „Verbindungen“</button></div>`;
}

function zigbeeSearchStateHTML() {   // the Zigbee network open for new devices: time left – or search again
  const s = (zigbee.net && zigbee.net.search) || {}, f = addFlow;
  if (s.active) {
    const left = `${Math.floor(s.left / 60)}:${String(s.left % 60).padStart(2, "0")}`;
    return `<div class="scan"><div class="scan-head"><strong class="searching">Zigbee-Suche läuft – noch ${left}</strong>
        <button class="small" data-action="zigbee-unsearch" ${zigbeeNetPending ? "disabled" : ""}>${
          zigbeeNetPending === "unsearch" ? "beende …" : "Suche beenden"}</button></div>
      <p class="hint">Mehrere Geräte gehen nacheinander in derselben Suche. Klappt es nicht, das Gerät zum Anlernen näher
        an den Pi bringen. Eine Steckdose gibt das Funksignal auch weiter – Thermostate weiter weg erreichen den Pi dann
        besser.</p></div>`;
  }
  const text = zigbeeNetPending === "search" ? "Öffne das Zigbee-Netz …" : f.opened ? "Die Zigbee-Suche ist beendet." : "";
  return `<div class="scan"><div class="scan-head">${text ? `<strong>${text}</strong>` : ""}
    <button class="small ${f.opened ? "" : "primary"}" data-action="add-zigbee-again" ${zigbeeNetPending || zigbeeBusy()
      ? "disabled" : ""}>${f.opened ? "Noch einmal suchen" : "Suche starten"}</button></div></div>`;
}

function addZigbeeItemHTML(d) {   // a device that joined: being paired, ready („Weiter“), failed or unknown („Entfernen …“)
  const heater = state.heaters.find(h => h.id === d.ieee), plug = (state.plugs || []).find(p => p.id === d.ieee);
  const lamp = (state.lights || []).find(l => l.id === d.ieee), button = (state.buttons || []).find(b => b.id === d.ieee);
  const sensor = (state.detectors || []).find(x => x.id === d.ieee);
  const ready = d.interview === "done" && d.supported && !!(heater || plug || lamp || button || sensor);
  const [dot, words] = zigbeeDeviceState(d);
  const model = [d.vendor, d.model].filter(Boolean).join(" ") || zigbeeKindWord(d);
  const title = heater ? heater.name : plug ? plug.name : lamp ? lamp.name : button ? button.name : sensor ? sensor.name : model;
  const bad = d.interview === "failed" || (d.interview === "done" && !d.supported);
  return `<li data-ieee="${esc(d.ieee)}"><span class="dot ${ready ? "ok" : dot}"></span><span class="hname"><b>${esc(title)}</b>${
      title !== model ? ` <span class="muted">${esc(model)}</span>` : ""}
      <span class="muted zigbee-facts">${esc(ready ? "✓ angelernt" : words)}</span></span>
    ${ready ? `<button class="small primary" data-action="add-next" data-type="${heater ? "heater" : plug ? "plug" : lamp ? "light" : button ? "button" : "detector"}">Weiter</button>`
      : bad ? `<button class="small" data-action="zigbee-forget" ${zigbeeNetPending ? "disabled" : ""}>${
        zigbeeNetPending === d.ieee ? "entferne …" : "Entfernen …"}</button>` : ""}</li>`;
}

function addZigbeeHTML(steps, title) {   // guide, the open network and what joined
  const found = addNewZigbee();
  return addHowHTML(steps, title) + zigbeeSearchStateHTML()
    + (found.length ? `<ul class="net-list zigbee-list">${found.map(addZigbeeItemHTML).join("")}</ul>` : "");
}

function addFindHTML() {   // steps 2 and 3: the guide and what the search found
  const f = addFlow;
  if (f.kind === "heater") {
    if (deviceForm && deviceForm.mode === "new") return `<div class="device">${deviceFormHTML(deviceForm)}</div>`;
    return addHowHTML(["Die Heizung einschalten. Sie muss im selben WLAN sein wie die App – wie sie ins WLAN kommt, steht in ihrer Anleitung.",
        "Die App sucht im Heimnetz nach Heizungen.",
        "Bei deiner Heizung „Übernehmen“ drücken und den Token eintragen – wie du an ihn kommst, steht dann dabei."])
      + (scan ? scanPanelHTML() : "")
      + `<p class="hint">Nicht dabei? Adresse und Token <button class="small" data-action="add-manual">von Hand eintragen</button></p>`;
  }
  if (f.kind === "sensor") {
    if (sensorForm && sensorForm.mode === "new") return `<div class="device">${sensorFormHTML(sensorForm)}</div>`;
    return addHowHTML(["Den Temperaturfühler (DS18B20 oder DHT22) in die Pill stecken und die Pill an ihr USB-Netzteil.",
        "Eine neue Pill zuerst mit der Shelly-App ins WLAN bringen.",
        "Die App sucht im Heimnetz. Beim Fühler „Übernehmen“ drücken, dann Name und Raum wählen."])
      + (sensorSearch ? sensorPanelHTML() : "");
  }
  if (["thermostat", "light", "button", "detector"].includes(f.kind)) {
    if (!zigbee) return `<p class="${zigbeeError ? "error-text" : "searching"}">${esc(zigbeeError || "Frage den Pi …")}</p>`;
    return zigbeeReady() ? addZigbeeHTML(f.kind === "light" ? LIGHT_STEPS : f.kind === "button" ? BUTTON_STEPS
      : f.kind === "detector" ? DETECTOR_STEPS : THERMOSTAT_STEPS) : zigbeeMissingHTML();
  }
  // a plug: Shelly in the WLAN – and with Zigbee a SONOFF too; both searches run at once
  const zig = state.app && state.app.zigbee;
  return addHowHTML(SHELLY_STEPS, "Shelly-Steckdose im WLAN <span class=\"muted\">(zum Beispiel Shelly Plug M Gen3)</span>")
    + (plugSearch ? plugSearchPanelHTML() : "")
    + (!zig ? "" : !zigbee ? '<p class="searching">Frage den Pi nach Zigbee …</p>'
      : zigbeeReady() ? addZigbeeHTML(SONOFF_STEPS, "SONOFF-Steckdose über Zigbee <span class=\"muted\">(zum Beispiel S60ZBTPF)</span>")
      : '<p class="hint">Eine Zigbee-Steckdose geht, sobald der Zigbee-Funk eingerichtet ist – unter „Verbindungen“ › „Zigbee-Funk“.</p>');
}

function addWhereHTML(roomOptions) {   // step 4: the room – for a plug the heater it gives power
  const f = addFlow, w = f.where, saving = busy.has("addflow") || busy.has("plugheater:" + w.id);
  const done = `<div class="row"><button class="primary" data-action="add-finish" ${saving ? "disabled" : ""}>${
    saving ? "speichere …" : "Fertig"}</button></div>`;
  if (w.type === "light") {
    const l = (state.lights || []).find(x => x.id === w.id);
    return `<p><b>${esc(l ? l.name : "Die Lampe")}</b> ist in der App. Wie soll sie heißen, und in welchem Raum ist sie?</p>
      <div class="device-form">
        <label>Name<input data-afield="name" value="${esc(f.name)}" placeholder="${esc(l ? l.name : "")}" maxlength="40"
          autocomplete="off"></label>
        <label>Raum<select data-afield="room">${roomOptions(f.room)}</select></label>
        <p class="hint">${state.rooms.length ? "" : "Noch kein Raum angelegt – Räume legst du im Tab „Räume“ über „+ Raum“ an. "
          }Im Raum erscheint die Lampe auf der Startseite: mit „Licht einschalten“, „heller“ und „dunkler“.</p>
      </div>${done}`;
  }
  if (w.type === "button") {
    const b = (state.buttons || []).find(x => x.id === w.id);
    return `<p><b>${esc(b ? b.name : "Der Schalter")}</b> ist in der App. Wie soll er heißen, und welches Licht schaltet er?</p>
      <div class="device-form">
        <label>Name<input data-afield="name" value="${esc(f.name)}" placeholder="${esc(b ? b.name : "")}" maxlength="40"
          autocomplete="off"></label>
        <label>Schaltet das Licht im Raum<select data-afield="room">${roomOptions(f.room)}</select></label>
        <p class="hint">${state.rooms.length ? "" : "Noch kein Raum angelegt – Räume legst du im Tab „Räume“ über „+ Raum“ an. "
          }${b && !b.direct ? "Diesen Schalter kennt die App nicht genau: Er schaltet über die App, also nur, wenn der Pi läuft."
          : "Nach „Fertig“ einmal eine Taste am Schalter drücken – dann koppelt ihn die App mit den Lampen. Danach schaltet " +
            "er sie direkt, auch wenn der Pi aus ist."}</p>
      </div>${done}`;
  }
  if (w.type === "detector") {
    const x = (state.detectors || []).find(v => v.id === w.id);
    return `<p><b>${esc(x ? x.name : "Der Bewegungsmelder")}</b> ist in der App. Wie soll er heißen, und in welchem Raum
      ist er?</p>
      <div class="device-form">
        <label>Name<input data-afield="name" value="${esc(f.name)}" placeholder="${esc(x ? x.name : "")}" maxlength="40"
          autocomplete="off"></label>
        <label>Raum<select data-afield="room">${roomOptions(f.room)}</select></label>
        <label class="check"><input type="checkbox" data-afield="motion" ${f.motion ? "checked" : ""}> Dort das Licht bei
          Bewegung einschalten</label>
        <p class="hint">${state.rooms.length ? "" : "Noch kein Raum angelegt – Räume legst du im Tab „Räume“ über „+ Raum“ an. "
          }Wie hell, wie lange und ob nachts gedämpft, stellst du danach im Raum bei „Licht bei Bewegung“ ein.</p>
      </div>${done}`;
  }
  if (w.type === "plug") {
    const p = (state.plugs || []).find(x => x.id === w.id);
    const named = `<b>${esc(p ? p.name : "Die Steckdose")}</b> ist in der App.`;
    const heating = f.appliance === "heizung", aeroflows = state.heaters.some(h => h.kind !== "zigbee");
    const room = `<label>Raum<select data-afield="room">${roomOptions(f.room)}</select></label>`;
    return `<p>${named} Was hängt daran – und wo ist sie?</p>
      <div class="device-form"><label>Angeschlossen<select data-afield="appliance">${applianceOptions(f.appliance)}</select></label>
      ${heating && aeroflows ? `<label>Heizung<select data-afield="heater">${plugHeaterOptions(w.id, f.heater)}</select></label>` : ""}
      ${heating && f.heater ? '<p class="hint">Die App stellt die Steckdose dabei so ein, dass sie nach einem Stromausfall wieder angeht.</p>'
        : `${room}<p class="hint">${heating && !aeroflows ? "Noch keine AeroFlow-Heizung in der App. Hängt später eine an der "
          + "Steckdose, wählst du sie bei ihr unter „Steckdosen“. " : ""}Der Strom des Geräts zählt für den Raum, den du hier wählst.</p>`}
      </div>${done}`;
  }
  const h = state.heaters.find(x => x.id === w.id), thermostat = h ? h.kind === "zigbee" : String(w.id).startsWith("0x");
  return `<p><b>${esc(h ? h.name : thermostat ? "Das Thermostat" : "Die Heizung")}</b> ist in der App. In welchem Raum ${
      thermostat ? "hängt es" : "steht sie"}?</p>
    <div class="device-form">
      ${thermostat ? `<label>Name<input data-afield="name" value="${esc(f.name)}" placeholder="${esc(h ? h.name : "")}"
        maxlength="40" autocomplete="off"></label>` : ""}
      <label>Raum<select data-afield="room">${roomOptions(f.room)}</select></label>
      ${state.rooms.length ? "" : '<p class="hint">Noch kein Raum angelegt – Räume legst du im Tab „Räume“ über „+ Raum“ an.</p>'}
    </div>${done}`;
}

function addFlowHTML(roomOptions) {
  const f = addFlow, now = addStep();
  const kind = f.kind && addKinds().find(k => k[0] === f.kind);
  const steps = ADD_STEPS.map((label, i) => `<li class="${i < now ? "done" : i === now ? "now" : ""}"${
    i === now ? ' aria-current="step"' : ""}>${label}</li>`).join("");
  const body = f.step === "what"
    ? `<p>Was kommt dazu?</p><div class="choices">${addKinds().map(([id, label, text]) => `<button class="choice"
        data-action="add-kind" data-kind="${id}"><b>${label}</b><span>${esc(text)}</span></button>`).join("")}</div>`
    : f.step === "where" ? addWhereHTML(roomOptions) : addFindHTML();
  const form = (deviceForm && deviceForm.mode === "new") || (sensorForm && sensorForm.mode === "new");   // has its own „Abbrechen“
  const home = state.rooms.find(r => r.id === f.home);   // opened in a room
  return `<article class="card add-flow">
    <div class="card-head"><h2>${kind ? `${kind[1]} hinzufügen` : "Gerät hinzufügen"}${home
      ? ` <span class="muted">· ${esc(home.name)}</span>` : ""}</h2>
      ${f.step === "find" && !form ? '<button class="small" data-action="add-back">Zurück</button>' : ""}
      <button class="small" data-action="add-close">${f.step === "where" ? "Später" : "Abbrechen"}</button></div>
    <ol class="add-steps" aria-label="Schritte">${steps}</ol>
    ${body}
  </article>`;
}

function openAdd(home = null) {   // home: the room it is opened in
  addFlow = { step: "what", home };
  deviceForm = calibForm = sensorForm = pillForm = null;
  scan = sensorSearch = plugSearch = null;
  if (state.app.zigbee && !zigbee) loadZigbee();
  invalidate();
  render();
  window.scrollTo(0, 0);
}

function addReset() {   // leave the guide or go back to „Was?“: searches and half-filled forms go; an open Zigbee search closes
  const opened = addFlow && addFlow.opened;
  scan = sensorSearch = plugSearch = null;
  if (deviceForm && deviceForm.mode === "new") deviceForm = null;
  if (sensorForm && sensorForm.mode === "new") sensorForm = null;
  if (pillForm && pillForm.at === "search") pillForm = null;
  if (opened && zigbeeSearching()) zigbeeSearch(false);
}

function closeAdd() { addReset(); addFlow = null; invalidate(); render(); }

function addBack() { const home = addFlow && addFlow.home; addReset(); addFlow = { step: "what", home }; invalidate(); render(); }

function addHome() {   // opened in a room: that room is chosen at „Wohin?“
  const id = addFlow && addFlow.home;
  return id && state.rooms.some(r => r.id === id) ? id : "";
}

async function addKind(kind) {   // step 2: the guide – and the search starts by itself
  addFlow = { step: "find", kind, before: zigbeeAddresses(), home: addFlow && addFlow.home };
  invalidate();
  render();
  if (kind === "heater") return runScan(null);
  if (kind === "sensor") return runSensorSearch();
  if (kind === "plug") runPlugSearch();
  if (!state.app.zigbee) return;
  if (!zigbee) await loadZigbee();
  const f = addFlow;
  if (!f || f.kind !== kind || !zigbeeReady()) return render();
  f.before = zigbeeAddresses();
  addZigbeeSearch();
}

async function addZigbeeSearch() {   // open the Zigbee network – „Abbrechen“ closes it again
  const f = addFlow;
  if (!f || zigbeeSearching()) return;
  f.opened = true;
  await zigbeeSearch(true);
}

function addWhere(type, id) {   // step 4
  const h = type === "heater" ? state.heaters.find(x => x.id === id) : null;
  const p = type === "plug" ? (state.plugs || []).find(x => x.id === id) : null;
  const l = type === "light" ? (state.lights || []).find(x => x.id === id) : null;
  const b = type === "button" ? (state.buttons || []).find(x => x.id === id) : null;
  const x = type === "detector" ? (state.detectors || []).find(v => v.id === id) : null;
  addFlow = Object.assign(addFlow || { kind: type }, { step: "where", where: { type, id }, name: "",
    room: addHome() || (h && h.room) || (l && l.room) || (b && b.room) || (x && x.room) || "", heater: (p && p.heater) || "",
    appliance: (p && p.appliance) || "sonstiges", motion: true });
  invalidate();
  render();
}

async function addFinish() {   // „Fertig“: save the room (and a thermostat's name) – for a plug its heater
  const f = addFlow, w = f && f.where;
  if (!w || busy.has("addflow")) return;
  let said = null;   // a switch: what to do now (press a button)
  if (w.type === "plug") {
    const p = (state.plugs || []).find(x => x.id === w.id), heater = f.appliance === "heizung" ? f.heater : "";
    if (!heater && p && f.appliance !== p.appliance && !(await setPlugAppliance(w.id, f.appliance, true))) return;
    if (heater ? !(await setPlugHeater(w.id, heater, true)) : f.room && !(await setPlugRoom(w.id, f.room, true))) return;
    deviceTab = "plugs";
  } else if (w.type === "light") {   // its name (if one was typed) and its room
    busy.add("addflow");
    render();
    try {
      if (String(f.name || "").trim() && !(await renameLight(w.id, true, String(f.name).trim()))) return;
      if (f.room && !(await setLightRoom(w.id, f.room, true))) return;
    } finally {
      busy.delete("addflow");
    }
    deviceTab = "lights";
  } else if (w.type === "button") {   // its name (if one was typed) and the room whose light it switches
    busy.add("addflow");
    render();
    try {
      if (String(f.name || "").trim() && !(await renameButton(w.id, true, String(f.name).trim()))) return;
      const saved = f.room ? await setButtonRoom(w.id, f.room, true) : true;
      if (!saved) return;
      if (typeof saved === "string") said = saved;
    } finally {
      busy.delete("addflow");
    }
    deviceTab = "lights";
  } else if (w.type === "detector") {   // its name (if one was typed), its room – and the light there by motion
    busy.add("addflow");
    render();
    try {
      if (String(f.name || "").trim() && !(await renameDetector(w.id, true, String(f.name).trim()))) return;
      if (f.room && !(await setDetectorRoom(w.id, f.room, true))) return;
      if (f.room && f.motion) {
        try {
          said = (await api("PUT", `api/rooms/${encodeURIComponent(f.room)}/motion`, { on: true })).message;
        } catch (e) {
          toast(e.message, true);
          return;
        }
      }
    } finally {
      busy.delete("addflow");
    }
    deviceTab = "lights";
  } else {
    const body = {};
    if (f.room) body.room = f.room;
    if (String(f.name || "").trim()) body.name = String(f.name).trim();
    if (Object.keys(body).length) {
      busy.add("addflow");
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      render();
      try {
        await api("PATCH", `api/heaters/${encodeURIComponent(w.id)}`, body);
      } catch (e) {
        toast(e.message, true);
        return;
      } finally {
        busy.delete("addflow");
        invalidate();
        render();
      }
    }
    deviceTab = "heaters";
  }
  localStorage.setItem("heizung.deviceTab", deviceTab);
  toast(said || "Fertig – das Gerät ist in der App.");
  closeAdd();
  await refresh();
}

function addToZigbee() {   // no Zigbee yet: off to „Verbindungen“ › „Zigbee-Funk“
  closeAdd();
  closeRoom();
  tab = "connections";
  connTab = "zigbee";
  localStorage.setItem("heizung.tab", tab);
  localStorage.setItem("heizung.connTab", connTab);
  openConnTab();
  render();
  window.scrollTo(0, 0);
}

function openDeviceForm(heaterId, found = null) {  // found: a heater from the network search {address, hostname}
  calibForm = null;
  if (!heaterId) {
    deviceForm = { mode: "new", name: "", host: found ? found.address : "", token: "",
                   placeholderName: found && found.hostname ? found.hostname : "" };
  } else {
    const h = state.heaters.find(x => x.id === heaterId);
    if (!h) return;
    const zigbee = h.kind === "zigbee";   // its name without a name of its own: „Thermostat“ + the end of its address
    deviceForm = { mode: "edit", id: h.id, name: h.custom_name, host: h.host, token: "", hasToken: h.writable,
                   removeToken: false, zigbee, placeholderName: zigbee ? "Thermostat " + h.id.slice(-4).toUpperCase()
                                                                       : h.hostname || h.host };
  }
  render();
  const field = !heaterId ? (found ? "token" : "name") : deviceForm.zigbee ? "name" : "host";
  const first = document.querySelector('.device-form input[data-field="' + field + '"]');
  if (first) { first.focus(); first.scrollIntoView({ block: "center", behavior: "smooth" }); }
}

async function saveDevice() {
  const f = deviceForm;
  if (!f || busy.has("devform")) return;
  const body = f.zigbee ? { name: f.name } : { name: f.name, host: f.host };
  if (!f.zigbee && String(f.token || "").trim()) body.token = String(f.token).trim();
  if (!f.zigbee && f.removeToken) body.remove_token = true;
  busy.add("devform");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  try {
    const res = f.mode === "new" ? await api("POST", "api/heaters", body)
                                 : await api("PATCH", `api/heaters/${encodeURIComponent(f.id)}`, body);
    deviceForm = null;
    toast(res.message || "Gespeichert.", Boolean(res.warning));
    if (f.mode === "new" && addFlow && res.id) addWhere("heater", res.id);
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

function roundHalfEven(x) { const r = Math.round(x); return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r; }   // as Python

function calibPreview(h, measuredText) {
  const measured = parseFloat(String(measuredText || "").replace(",", "."));
  if (!(measured >= 0 && measured <= 40) || h.room_temp == null || h.temp_corr == null) {
    return "Gemessenen Wert eingeben – die neue Korrektur erscheint hier.";
  }
  const basis = calibBasis(h);
  const thermostat = h.kind === "zigbee";   // it takes 0.2 °C steps up to ±10 °C, the AeroFlow 0.1 °C up to ±9.9 °C
  let tenths = Math.round((h.temp_corr + measured - basis.temp) * 10);
  if (thermostat) tenths = roundHalfEven(tenths / 2) * 2;
  const next = tenths / 10, limit = thermostat ? 10 : 9.9;
  if (Math.abs(next) > limit) {
    return `Nötig wären ${fmtDelta(next)} – mehr als die möglichen ±${String(limit).replace(".", ",")} °C. Bitte Messung prüfen.`;
  }
  return `Neue Korrektur: <b>${fmtDelta(next)}</b> (bisher ${fmtDelta(h.temp_corr)}) – ${thermostat ? "das Thermostat"
    : "die Heizung"} zeigt danach etwa ${deg(measured)}.`;
}

function calibFormHTML(h) {
  const basis = calibBasis(h);
  const saving = busy.has("calib");
  return `<div class="device-form calib-form">
    <h3>Kalibrieren</h3>
    <p>${h.kind === "zigbee" ? "Das Thermostat" : "Die Heizung"} misst gerade <b>${deg(h.room_temp)}</b>${h.room_temp_avg != null
      ? `, im Mittel der letzten ${h.room_temp_avg_minutes} min <b>${deg(h.room_temp_avg)}</b>` : ""}.
      Aktuelle Korrektur: ${fmtDelta(h.temp_corr ?? 0)}.</p>
    <label>Gemessene Raumtemperatur in °C<input data-field="measured" type="number" step="0.1" min="0" max="40"
      inputmode="decimal" value="${esc(calibForm.measured)}" placeholder="z. B. 21,0"></label>
    <p class="calib-preview">${calibPreview(h, calibForm.measured)}</p>
    <p class="hint">Verglichen wird mit dem ${esc(basis.text)} ${h.kind === "zigbee"
      ? "des Thermostats (es nimmt die Korrektur in Schritten von 0,2 °C)" : "der Heizung"}. So misst du richtig: Thermometer in der
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
  if (h && h.kind === "zigbee") return removeThermostat(h);
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

async function removeThermostat(h) {  // paired: the Pi unpairs it (and the app forgets it with that) – else only the app forgets it
  const yes = await ask({ title: `„${h.name}“ entfernen?`,
    text: h.paired ? "Der Pi meldet das Thermostat im Zigbee-Netz ab, und die App vergisst Name und Raum. Willst du es " +
                     "später wieder nutzen, lernst du es mit „+ Gerät hinzufügen“ neu an."
                   : "Die App vergisst Name und Raum des Thermostats. Im Zigbee-Netz ist es schon nicht mehr angelernt.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  if (h.paired) {
    if (await zigbeeUnpair(h.id, "das Thermostat")) deviceForm = null;
  } else {
    try {
      toast((await api("DELETE", `api/heaters/${encodeURIComponent(h.id)}`)).message);
      deviceForm = null;
    } catch (e) {
      toast(e.message, true);
    }
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
    roomForm = { mode: "new", name: "", heaters: [], color: "" };
  } else {
    const r = state.rooms.find(x => x.id === roomId);
    if (!r) return;
    roomForm = { mode: "edit", id: r.id, name: r.name, heaters: r.heaters.slice(), color: r.color || "" };
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
    const body = { name, heaters: f.heaters, color: f.color || null };
    if (f.mode === "new") await api("POST", "api/rooms", body);
    else await api("PATCH", `api/rooms/${encodeURIComponent(f.id)}`, body);
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
    if (roomView === id) closeRoom();
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
    closeRoom();
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

// ------------------------------------------------------------------ WLAN ("Einrichtung › WLAN")
const WIFI_FRESH_MS = 60 * 1000;   // an older list of networks is searched again when the tab is opened
let wifi = null;          // what api/wifi answered: net (cable, WLAN), networks, job, via, hostname
let wifiError = null;     // api/wifi could not be loaded
let wifiScan = null;      // search started here: {running} | {error}
let wifiForm = null;      // connect form: {ssid, hidden, open, password, show, error, job}
let wifiSeen = null;      // id of the connection whose result was closed with "OK"
let wifiTimer = null;     // polls every 2 s while the Pi connects
let wifiFocus = false;    // after a failed attempt: cursor back into the password field

function wifiConnecting() { return !!(wifi && wifi.job && ["waiting", "connecting"].includes(wifi.job.state)); }
function wifiBusy() { return !!(wifi && wifi.busy) || wifiConnecting() || !!(wifiScan && wifiScan.running); }
function appURL(host) { return `http://${host}${location.port ? ":" + location.port : ""}`; }   // same port as this page
function signalWord(s) { return s >= 70 ? "sehr guter Empfang" : s >= 50 ? "guter Empfang" : s >= 30 ? "mäßiger Empfang" : "schwacher Empfang"; }
function signalBars(s) {
  const n = s >= 70 ? 4 : s >= 50 ? 3 : s >= 30 ? 2 : 1;
  return `<span class="bars" aria-hidden="true">${[1, 2, 3, 4].map(i => `<i${i <= n ? ' class="on"' : ""}></i>`).join("")}</span>`;
}

async function loadWifi() {
  try { wifi = await api("GET", "api/wifi"); wifiError = null; }
  catch (e) { wifiError = e.message; }
  followWifi();
  render();
}

function followWifi() {  // fast polling while the Pi connects; afterwards the result goes into the form or a note
  const j = wifi && wifi.job;
  const running = wifiConnecting();
  if (running && !wifiTimer) wifiTimer = setInterval(() => { if (!document.hidden && view === "setup") loadWifi(); }, 2000);
  if (!running && wifiTimer) { clearInterval(wifiTimer); wifiTimer = null; }
  if (!running && j && wifiForm && wifiForm.job === j.id) {
    if (j.state === "failed") {   // back to the form with the reason; the password is typed again
      Object.assign(wifiForm, { job: null, error: j.message || "Verbinden hat nicht geklappt.", password: "" });
      wifiSeen = j.id;
      wifiFocus = true;
    } else {
      wifiForm = null;
    }
    invalidate();
  }
}

async function openWifiTab() {  // fresh state – and a fresh search if the list is old
  await loadWifi();
  if (!wifi || !wifi.supported || wifiBusy()) return;
  const age = wifi.scanned ? Date.now() - Date.parse(wifi.scanned) : Infinity;
  if (!(age < WIFI_FRESH_MS)) scanWifi();
}

async function scanWifi() {
  wifiScan = { running: true };
  render();
  try { wifi = (await api("POST", "api/wifi/scan", {})).wifi; wifiScan = null; }
  catch (e) { wifiScan = { error: e.message }; }
  render();
}

function openWifiForm(ssid, hidden = false) {
  const n = !hidden && wifi && (wifi.networks || []).find(x => x.ssid === ssid);
  wifiForm = { ssid: hidden ? "" : ssid, hidden, open: !!n && !n.security, password: "", show: false, error: null, job: null };
  invalidate();
  render();
  const input = document.querySelector(".wifi-form input[data-wfield]");
  if (input) input.focus();
}

async function connectWifi() {
  const f = wifiForm;
  if (!f || f.job || f.sending) return;
  const ssid = f.hidden ? String(f.ssid || "").trim() : f.ssid;
  if (!ssid) { f.error = "Bitte den Namen des WLAN eingeben."; invalidate(); return render(); }
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  Object.assign(f, { error: null, sending: true });
  invalidate();
  render();
  try {
    const res = await api("POST", "api/wifi/connect", { ssid, password: f.open ? "" : f.password, hidden: f.hidden });
    wifi = res.wifi;
    Object.assign(f, { ssid, job: wifi.job && wifi.job.id, password: "" });   // the password is not kept in the page
  } catch (e) {
    f.error = e.message;
  }
  f.sending = false;
  invalidate();
  followWifi();
  render();
}

async function forgetWifi(ssid) {
  const n = wifi && (wifi.networks || []).find(x => x.ssid === ssid);
  const up = (wifi && wifi.net ? wifi.net.interfaces : []).filter(i => i.connected);
  if (!up.some(i => i.type === "ethernet") && up.some(i => i.type === "wifi" && i.ssid === ssid)) {   // the Pi's only way in
    return ask({ title: `„${ssid}“ lässt sich gerade nicht vergessen`,
      text: "Der Pi hängt gerade nur über dieses WLAN am Netz. Vergisst er es, ist er nicht mehr erreichbar – auch nicht " +
        "aus der Ferne. Bitte zuerst das Netzwerkkabel am Pi einstecken.",
      buttons: [{ label: "OK", value: null }] });
  }
  const yes = await ask({
    title: `„${ssid}“ vergessen?`,
    text: "Der Pi verbindet sich dann nicht mehr von selbst mit diesem WLAN." + (n && n.active ? " Er trennt sich sofort davon" +
      (wifi.via === "wifi" ? " – diese Seite ist danach nur noch über das Netzwerkkabel erreichbar." : ".") : ""),
    buttons: [{ label: "Abbrechen", value: null }, { label: "Vergessen", value: "yes", cls: "danger-solid" }],
  });
  if (!yes) return;
  try {
    const res = await api("POST", "api/wifi/forget", { ssid });
    wifi = res.wifi;
    toast(res.message || "Vergessen.");
  } catch (e) { toast(e.message, true); }
  invalidate();
  render();
}

function wifiStatusHTML(w) {
  const net = w.net, list = net ? net.interfaces : [];
  const cable = list.find(i => i.type === "ethernet"), air = list.find(i => i.type === "wifi");
  const here = type => w.via === type ? `<span class="tag ok">darüber bist du gerade verbunden</span>` : "";
  const addr = i => i.ipv4.length ? ` · ${esc(i.ipv4[0])}` : "";
  let lines = "";
  if (net) {
    const airText = !air ? "kein WLAN-Adapter gefunden"
      : air.connected ? `verbunden mit „${esc(air.ssid || "?")}“${air.signal != null ? " · " + signalWord(air.signal) : ""}${addr(air)}`
      : !net.country ? "aus – noch nicht eingerichtet"
      : air.state.startsWith("connecting") ? "verbindet …" : "nicht verbunden";
    lines = `<ul class="net-list">
      <li><span class="dot ${cable && cable.connected ? "ok" : ""}"></span><span class="hname"><b>Netzwerkkabel</b>
        <span class="muted">${cable && cable.connected ? "eingesteckt" + addr(cable) : "nicht eingesteckt"}</span></span>${here("ethernet")}</li>
      <li><span class="dot ${air && air.connected ? "ok" : ""}"></span><span class="hname"><b>WLAN</b>
        <span class="muted">${airText}</span></span>${here("wifi")}</li>
    </ul>`;
  } else {
    lines = `<p class="muted">${wifiScan && wifiScan.running ? "Frage den Pi …" : "Noch nicht abgefragt – „Neu suchen“ drücken."}</p>`;
  }
  const j = w.job;
  let note = "";
  if (wifiConnecting() && !(wifiForm && wifiForm.job === j.id)) {   // started in another browser
    note = `<div class="wifi-note"><p class="searching"><b>Verbinde mit „${esc(j.ssid)}“ …</b></p></div>`;
  } else if (j && j.id !== wifiSeen && j.finished && Date.now() - Date.parse(j.finished) < 30 * 60 * 1000 &&
             (j.state === "failed" || (air && air.connected && air.ssid === j.ssid))) {   // not once that WLAN is gone again
    const ip = air && air.connected ? air.ipv4[0] : null;
    note = j.state === "done" ? `<div class="wifi-note ok">
        <p><b>✓ Der Pi ist jetzt im WLAN „${esc(j.ssid)}“.</b></p>
        ${cable && cable.connected ? `<p>Du kannst das Netzwerkkabel jetzt abziehen. Die App ist danach über das WLAN erreichbar:
          <b>${esc(appURL(w.hostname + ".local"))}</b>${ip ? ` oder <b>${esc(appURL(ip))}</b>` : ""}</p>` : ""}
        <button class="small" data-action="wifi-ok">OK</button></div>`
      : `<div class="wifi-note bad"><p><b>Verbinden mit „${esc(j.ssid)}“ hat nicht geklappt.</b></p>
        <p>${esc(j.message || "")}</p><button class="small" data-action="wifi-ok">OK</button></div>`;
  }
  return `<div class="card wifi-status">
    <h2>Verbindung des Pi</h2>
    ${lines}${note}
    <p class="hint">Im Heimnetz erreichbar unter <b>${esc(appURL(w.hostname + ".local"))}</b>${w.at ? ` · Stand ${esc(fmtTime(w.at))}` : ""}</p>
  </div>`;
}

function wifiFormHTML(f, w) {
  if (f.job || f.sending) {
    return `<div class="device-form wifi-form"><p class="searching"><b>Verbinde mit „${esc(f.ssid)}“ …</b></p>
      <p class="muted">Das dauert bis zu einer Minute.${w.via === "wifi" ? " Diese Seite kann dabei kurz die Verbindung verlieren." : ""}</p></div>`;
  }
  return `<div class="device-form wifi-form">
    ${f.hidden ? `<h3>Verstecktes WLAN</h3>
      <label>Name des WLAN (genau wie am Router)<input data-wfield="ssid" value="${esc(f.ssid)}" maxlength="32"
        autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"></label>` : `<h3>Mit „${esc(f.ssid)}“ verbinden</h3>`}
    ${f.open ? `<p class="hint">Dieses WLAN ist offen, also ohne Passwort. Jeder in der Nähe kann dort mitlesen – ein WLAN mit
        Passwort ist besser.</p>`
      : `<label>WLAN-Passwort${f.hidden ? " (leer lassen, wenn es keins hat)" : ""}<input data-wfield="password"
          type="${f.show ? "text" : "password"}" value="${esc(f.password)}" maxlength="64" autocomplete="off"
          autocapitalize="off" autocorrect="off" spellcheck="false"></label>
        <label class="check"><input type="checkbox" data-wfield="show" ${f.show ? "checked" : ""}> Passwort anzeigen</label>
        <p class="hint">Das Passwort steht meist auf der Rück- oder Unterseite des Routers, bei der FRITZ!Box als
          „WLAN-Netzwerkschlüssel“. Auf Groß- und Kleinschreibung achten.</p>`}
    ${w.via === "wifi" ? `<p class="hint warn">Du bist gerade über das WLAN mit dem Pi verbunden. Wechselt er in ein anderes
      WLAN, ist diese Seite kurz weg. Danach wieder öffnen: ${esc(appURL(w.hostname + ".local"))}</p>` : ""}
    ${f.error ? `<p class="error-text">${esc(f.error)}</p>` : ""}
    <div class="row">
      <button class="primary" data-action="wifi-connect">Verbinden</button>
      <button data-action="wifi-cancel">Abbrechen</button>
    </div>
  </div>`;
}

function wifiNetworksHTML(w) {
  const scanning = !!(wifiScan && wifiScan.running), busy = wifiBusy(), f = wifiForm, nets = w.networks;
  const items = (nets || []).map(n => {
    const tags = (n.active ? `<span class="tag ok">verbunden</span>` : n.saved ? `<span class="tag">gespeichert</span>` : "") +
      (!n.security ? `<span class="tag warn">offen</span>` : "");
    const formHere = f && !f.hidden && f.ssid === n.ssid;
    const buttons = n.enterprise ? `<span class="muted wifi-no">Firmen-WLAN – hier nicht möglich</span>`
      : (n.active || formHere ? "" : `<button class="small primary" data-action="wifi-pick" data-ssid="${esc(n.ssid)}" ${busy ? "disabled" : ""}>Verbinden</button>`) +
        (n.saved ? `<button class="small" data-action="wifi-forget" data-ssid="${esc(n.ssid)}" ${busy ? "disabled" : ""}>Vergessen</button>` : "");
    const form = formHere ? `<li class="wifi-form-row">${wifiFormHTML(f, w)}</li>` : "";
    return `<li>${signalBars(n.signal)}<span class="hname"><b>${esc(n.ssid)}</b> <span class="muted">${signalWord(n.signal)}</span></span>
      ${tags}<span class="wifi-buttons">${buttons}</span></li>${form}`;
  }).join("");
  const lost = f && !f.hidden && nets && !nets.some(n => n.ssid === f.ssid) ? wifiFormHTML(f, w) : "";   // gone from the list
  return `<div class="card wifi-card">
    <div class="card-head"><h2>WLAN-Netze in der Nähe</h2>
      <button class="small" data-action="wifi-scan" ${busy ? "disabled" : ""}>${scanning ? "suche …" : "Neu suchen"}</button></div>
    ${scanning && !nets ? `<p class="searching">Suche WLAN-Netze … das dauert ein paar Sekunden.</p>` : ""}
    ${wifiScan && wifiScan.error ? `<p class="error-text">${esc(wifiScan.error)}</p>` : ""}
    ${nets && !nets.length ? `<p class="muted">Kein WLAN gefunden. Ist der Router eingeschaltet und nicht zu weit weg?</p>` : ""}
    ${items ? `<ul class="wifi-list">${items}</ul>` : ""}${lost}
    ${f && f.hidden ? wifiFormHTML(f, w)
      : `<p class="hint">Steht dein WLAN nicht in der Liste? <button class="small" data-action="wifi-hidden" ${busy ? "disabled" : ""}>Namen selbst eingeben</button></p>`}
  </div>`;
}

function wifiHTML() {   // „Verbindungen“ › „Heimnetz“: the Pi's own WLAN
  const w = wifi;
  if (!w) return `<div class="card"><p class="${wifiError ? "error-text" : "searching"}">${esc(wifiError || "Frage den Pi …")}</p></div>`;
  if (!w.supported) return `<div class="card"><p>Das WLAN lässt sich hier nur einrichten, wenn FlowThermPi mit dem
    Installationsbefehl auf dem Raspberry Pi läuft.</p></div>`;
  return wifiStatusHTML(w) + wifiNetworksHTML(w);
}

// ------------------------------------------------------------------ Fernwartung ("Einrichtung › Fernwartung")
let remote = null;          // what api/remote answered: tailscale (state, name, addresses, login link), job, device
let remoteError = null;     // api/remote could not be loaded
let remotePending = null;   // a button pressed here whose answer is still out: "refresh" | "up" | "down" | "logout" | "mode"
let remoteSeen = null;      // id of the setup whose failure was closed with "OK"
let remoteTimer = null;     // polls every 2 s while the Pi installs, connects or waits for the login

function remoteRunning() { return !!(remote && remote.job && ["waiting", "running"].includes(remote.job.state)); }
function remoteBusy() {     // a quick look at the Pi ("status") does not count: the server waits for it
  return !!remotePending || !!(remote && remote.busy && remote.busy !== "status") || remoteRunning();
}

async function loadRemote() {
  try { remote = await api("GET", "api/remote"); remoteError = null; }
  catch (e) { remoteError = e.message; }
  followRemote();
  render();
}

function followRemote() {
  const on = remoteRunning() || !!(remote && remote.watching);
  if (on && !remoteTimer) remoteTimer = setInterval(() => {
    if (!document.hidden && view === "setup" && tab === "connections" && connTab === "remote") loadRemote();
  }, 2000);
  if (!on && remoteTimer) { clearInterval(remoteTimer); remoteTimer = null; }
}

async function openRemoteTab() {  // fresh state from the Pi: Tailscale may have changed since
  await loadRemote();
  if (remote && remote.supported && !remoteBusy()) remoteDo("refresh", { quiet: true });
}

async function remoteDo(action, { quiet = false, okText = null } = {}) {
  remotePending = action;
  render();
  try {
    const res = await api("POST", "api/remote/" + action);
    remote = res.remote;
    if (!quiet && (okText || res.message)) toast(okText || res.message);
  } catch (e) {
    if (!quiet) toast(e.message, true);
    try { remote = await api("GET", "api/remote"); } catch (_) { /* shown with the next look */ }
  }
  remotePending = null;
  followRemote();
  invalidate();
  render();
}

async function remoteSetup() {
  const t = remote && remote.tailscale;
  if (!t || !t.installed) {
    const yes = await ask({ title: "Fernwartung einrichten?",
      text: "Der Pi lädt dafür Tailscale aus dessen offiziellem Paketarchiv (pkgs.tailscale.com, etwa 30 MB) und hält es " +
        "danach selbst aktuell. Das dauert ein paar Minuten, die Heizungen heizen dabei ganz normal weiter. Danach meldest " +
        "du den Pi mit deinem Tailscale-Konto an.",
      buttons: [{ label: "Abbrechen", value: null }, { label: "Einrichten", value: "yes", cls: "primary" }] });
    if (!yes) return;
  }
  remoteSeen = null;
  remoteDo("up");
}

function farAway() { return remote && remote.via_tailscale ? "Du bist gerade aus der Ferne hier – diese Seite ist danach weg. " : ""; }

async function remoteOff() {
  const ask_ = remote && remote.mode === "ask";
  const yes = await ask({ title: ask_ ? "Fernhilfe beenden?" : "Fernwartung ausschalten?",
    text: farAway() + (ask_ ? "Der Pi ist dann nicht mehr aus der Ferne erreichbar, bis die Bewohner wieder Fernhilfe erlauben."
      : "Der Pi ist dann nicht mehr aus der Ferne erreichbar. Wieder einschalten lässt sie sich nur hier im Heimnetz, in der " +
        "Einrichtung.") + " Angemeldet bleibt der Pi trotzdem.",
    buttons: [{ label: "Abbrechen", value: null }, { label: ask_ ? "Beenden" : "Ausschalten", value: "yes", cls: "danger-solid" }] });
  if (yes) remoteDo("down");
}

async function remoteMode(mode) {
  if (!remote || remote.mode === mode || remoteBusy()) return;
  if (mode === "always") {
    const yes = await ask({ title: "Immer erreichbar?",
      text: "Der Pi ist dann dauerhaft aus der Ferne erreichbar, ohne dass die Bewohner es jedes Mal erlauben. Sie sehen in " +
        "der Heizungsansicht nur den Hinweis „Fernwartung ist eingeschaltet“.",
      buttons: [{ label: "Abbrechen", value: null }, { label: "Immer erreichbar", value: "yes", cls: "primary" }] });
    if (!yes) return;
  }
  remotePending = "mode";
  render();
  try {
    const res = await api("PUT", "api/remote/mode", { mode });
    remote = res.remote;
    if (res.message) toast(res.message);
  } catch (e) {
    toast(e.message, true);
    try { remote = await api("GET", "api/remote"); } catch (_) { /* shown with the next look */ }
  }
  remotePending = null;
  invalidate();
  await refresh();
}

async function remoteLogout() {
  const yes = await ask({ title: "Pi bei Tailscale abmelden?",
    text: farAway() + "Der Pi verlässt dann das Tailscale-Netz. Für die Fernwartung muss er danach hier im Heimnetz neu " +
      "angemeldet werden, mit QR-Code. In der Tailscale-Verwaltung lässt er sich anschließend löschen.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Abmelden", value: "yes", cls: "danger-solid" }] });
  if (yes) remoteDo("logout");
}

function remoteStateText(t, r) {   // [dot, words]
  if (!t.installed) return ["", "Aus – noch nicht eingerichtet"];
  if (!t.daemon) return ["offline", "Tailscale läuft gerade nicht"];
  const until = helpUntil(r);
  switch (t.state) {
    case "Running": return ["ok", until ? `An – Fernhilfe bis ${clockOf(until)} Uhr` : "An – der Pi ist aus der Ferne erreichbar"];
    case "Stopped": return ["", r.mode === "ask" ? "Aus – bis die Bewohner Fernhilfe erlauben" : "Aus – angemeldet, aber ausgeschaltet"];
    case "Starting": return ["on", "Verbindet …"];
    case "NeedsMachineAuth": return ["on", "Wartet auf die Freigabe in der Tailscale-Verwaltung"];
    case "NeedsLogin": return t.auth_url ? ["on", "Wartet auf die Anmeldung"] : ["", "Aus – nicht angemeldet"];
    default: return ["", "Tailscale startet …"];
  }
}

function remoteButtonsHTML(t, busy, r) {
  const b = (action, label, cls = "") => `<button class="${cls}" data-action="${action}" ${busy ? "disabled" : ""}>${label}</button>`;
  if (!t.installed || !t.daemon) return b("remote-setup", "Fernwartung einrichten", "primary");
  const ask_ = r.mode === "ask";
  switch (t.state) {
    case "Running": return b("remote-off", ask_ ? "Fernhilfe beenden …" : "Ausschalten …") + b("remote-logout", "Abmelden …");
    case "Stopped": return b("remote-on", ask_ ? `Für ${helpTime(r.help_minutes)} einschalten` : "Einschalten", "primary") +
      b("remote-logout", "Abmelden …");
    case "Starting": case "NeedsMachineAuth": return b("remote-cancel", "Abbrechen");
    case "NeedsLogin": return t.auth_url ? "" : b("remote-setup", "Fernwartung einrichten", "primary");   // the login card has its own
    default: return "";
  }
}

function remoteFactsHTML(t) {
  const far = [t.magic_dns && t.name ? appURL(t.name) : null, t.ipv4 ? appURL(t.ipv4) : null].filter(Boolean);
  const tags = t.tags || [];   // a tagged Pi belongs to its tag, no longer to the account that signed it in
  return `<dl class="about-facts">
    <dt>Name</dt><dd>${esc(t.name || "–")}</dd>
    ${t.state === "Running" && far.length ? `<dt>Aus der Ferne</dt><dd>${far.map(u => `<b>${esc(u)}</b>`).join(" oder ")}</dd>` : ""}
    ${tags.length ? `<dt>Tag</dt><dd>${esc(tags.join(", "))}${t.tailnet ? ` · Netz ${esc(t.tailnet)}` : ""}</dd>`
      : `<dt>Konto</dt><dd>${esc(t.user || t.tailnet || "–")}${t.tailnet && t.user && t.tailnet !== t.user ? ` · Netz ${esc(t.tailnet)}` : ""}</dd>`}
    <dt>Anmeldung</dt><dd>${t.key_expiry ? "gültig bis " + esc(fmtDate(t.key_expiry)) : "läuft nicht ab"}</dd>
    ${t.version ? `<dt>Tailscale</dt><dd>Version ${esc(t.version)}</dd>` : ""}
  </dl>
  ${t.key_expiry ? `<p class="hint">Tipp: In der Tailscale-Verwaltung bei diesem Pi „Disable key expiry“ wählen –
    sonst muss er etwa alle sechs Monate hier vor Ort neu angemeldet werden.</p>` : ""}`;
}

function remoteStatusHTML(r) {
  const t = r.tailscale, j = r.job, busy = remoteBusy();
  let body;
  if (remoteRunning()) {
    const installing = j.phase === "installing";
    const signedIn = t && t.daemon && ["Stopped", "Running", "Starting"].includes(t.state);   // only switched on
    body = `<p class="searching"><b>${installing ? "Tailscale wird installiert …" : signedIn ? "Schalte die Fernwartung ein …"
        : "Verbinde mit Tailscale …"}</b></p>
      <p class="muted">${installing ? "Das dauert ein paar Minuten. Die Heizungen heizen dabei ganz normal weiter."
        : signedIn ? "Einen Moment bitte." : "Gleich erscheint hier der Anmelde-Link."}</p>`;
  } else if (!t) {
    body = `<p class="${r.error ? "error-text" : "searching"}">${esc(r.error || "Frage den Pi …")}</p>`;
  } else {
    const [dot, words] = remoteStateText(t, r);
    body = `<ul class="net-list"><li><span class="dot ${dot}"></span><span class="hname"><b>${esc(words)}</b>
        ${!t.daemon && t.problem ? `<span class="muted">${esc(t.problem)}</span>` : ""}</span>
        ${r.via_tailscale ? `<span class="tag ok">darüber bist du gerade verbunden</span>` : ""}</li></ul>
      ${t.other_login ? `<p class="hint">Auf dem Pi ist noch eine ältere Anmeldung offen, zum Beispiel von Hand gestartet. Sie wird
        nicht verwendet: „Fernwartung einrichten“ startet eine neue als „${esc(r.hostname)}“.</p>` : ""}
      ${t.daemon && ["Running", "Stopped"].includes(t.state) ? remoteFactsHTML(t) : ""}
      ${r.error ? `<p class="error-text">${esc(r.error)}</p>` : ""}
      <div class="row">${remoteButtonsHTML(t, busy, r)}</div>
      ${r.via_tailscale && helpUntil(r) ? `<p class="hint">Verlängern können nur die Bewohner, in der Heizungsansicht mit
        „Fernhilfe verlängern“.</p>` : ""}`;
  }
  const failed = j && !remoteRunning() && j.state === "failed" && j.id !== remoteSeen && j.finished &&
    Date.now() - Date.parse(j.finished) < 30 * 60 * 1000;
  return `<div class="card remote-status">
    <div class="card-head"><h2>Fernwartung</h2>
      <button class="small" data-action="remote-refresh" ${busy ? "disabled" : ""}>${remotePending === "refresh" ? "frage …" : "Aktualisieren"}</button></div>
    ${body}
    ${failed ? `<div class="wifi-note bad"><p><b>Das hat nicht geklappt.</b></p><p>${esc(j.message || "")}</p>
      <button class="small" data-action="remote-ok">OK</button></div>` : ""}
    <p class="hint">Gerätenummer <b>${esc(r.device || "–")}</b> · im Tailscale-Netz „${esc(r.hostname)}“${r.at ? ` · Stand ${esc(fmtTime(r.at))}` : ""}</p>
  </div>`;
}

function remoteLoginHTML(r) {
  const t = r.tailscale, busy = remoteBusy();
  if (remoteRunning() || !t || !t.daemon || t.state !== "NeedsLogin" || !t.auth_url) return "";
  return `<div class="card remote-login">
    <h2>Pi bei Tailscale anmelden</h2>
    <p>Mit dem Handy den Code scannen oder den Link öffnen und dort mit dem Tailscale-Konto anmelden, über das du den Pi
      aus der Ferne erreichen willst.</p>
    ${t.qr ? `<img class="remote-qr" src="${esc(t.qr)}" width="128" height="128" alt="QR-Code mit dem Anmelde-Link">` : ""}
    <p class="remote-link"><a href="${esc(t.auth_url)}" target="_blank" rel="noopener noreferrer">${esc(t.auth_url)}</a></p>
    ${r.watching ? `<p class="searching">Warte auf die Anmeldung …</p>`
      : `<p class="muted">Noch nicht angemeldet. <button class="small" data-action="remote-refresh" ${busy ? "disabled" : ""}>Neu prüfen</button></p>`}
    <p class="hint warn">Wer sich mit diesem Link anmeldet, kann die Heizungs-App danach aus der Ferne öffnen. Link und Code
      deshalb nicht weitergeben.</p>
    <p class="hint">Der Pi meldet sich mit dem Tag <b>tag:flowthermpi</b> an. Das klappt nur, wenn das Tailscale-Netz diesen
      Tag kennt – einmal einzurichten, wie in der Anleitung unter „Tailscale-Netz einrichten“.</p>
    <div class="row"><button data-action="remote-cancel" ${busy ? "disabled" : ""}>Abbrechen</button></div>
  </div>`;
}

function remoteModeHTML(r) {
  const off = r.via_tailscale || remoteBusy() ? "disabled" : "";
  const choice = (mode, title, text) => `<button class="choice ${r.mode === mode ? "active" : ""}" data-action="remote-mode"
      data-mode="${mode}" aria-pressed="${r.mode === mode}" ${off}><b>${title}</b><span>${text}</span></button>`;
  return `<div class="card remote-mode">
    <h2>Wann ist der Pi erreichbar?</h2>
    <div class="choices">
      ${choice("ask", "Nur nach Freigabe (empfohlen)", `Die Bewohner tippen in der Heizungsansicht auf „Fernhilfe erlauben“. Dann
        ist der Pi ${esc(helpTime(r.help_minutes))} lang erreichbar, danach schaltet er die Fernwartung von selbst aus.`)}
      ${choice("always", "Immer", "Der Pi ist dauerhaft aus der Ferne erreichbar. Die Bewohner sehen in der Heizungsansicht " +
        "„Fernwartung ist eingeschaltet“.")}
    </div>
    ${r.via_tailscale ? `<p class="hint">Das lässt sich nur im Heimnetz ändern, nicht aus der Ferne.</p>` : ""}
  </div>`;
}

function remoteAboutHTML(r) {
  return `<div class="card">
    <h2>So funktioniert die Fernwartung</h2>
    <p>Der Pi kommt in dein privates Netz bei <b>Tailscale</b>. Von deinem Rechner oder Handy aus öffnest du die
      Heizungs-App dann von überall – ohne Freigaben im Router. Für private Nutzung ist Tailscale kostenlos.</p>
    <p>Was aus der Ferne erreichbar ist, legen die Regeln deines Tailscale-Netzes fest. Mit denen aus der Anleitung sind
      es nur die Heizungs-App und SSH – und der Pi selbst erreicht keines deiner Geräte.</p>
    <p>Im Heimnetz bleibt die App wie gewohnt unter <b>${esc(appURL(r.lan + ".local"))}</b> erreichbar.</p>
  </div>`;
}

function remoteHTML() {   // „Verbindungen“ › „Fernwartung“
  const r = remote;
  if (!r) return `<div class="card"><p class="${remoteError ? "error-text" : "searching"}">${esc(remoteError || "Frage den Pi …")}</p></div>`;
  if (!r.supported) return `<div class="card"><p>Fernwartung gibt es nur, wenn FlowThermPi mit dem
    Installationsbefehl auf dem Raspberry Pi läuft.</p></div>`;
  return remoteStatusHTML(r) + remoteLoginHTML(r) + remoteModeHTML(r) + remoteAboutHTML(r);
}

// ------------------------------------------------------------------ Zigbee ("Einrichtung › Zigbee")
let zigbee = null;          // what api/zigbee answered: programs (versions, Mosquitto, Zigbee2MQTT, USB devices), job, busy, net
let zigbeeError = null;     // api/zigbee could not be loaded
let zigbeePending = null;   // a button pressed here whose answer is still out: "refresh" | "install" | "remove" | "setup" | "start" | "stop"
let zigbeeNetPending = null;   // the same for the network: "search" | "unsearch" | the IEEE address of a device being removed
let zigbeeSeen = null;      // id of the job whose failure was closed with "OK"
let zigbeeTimer = null;     // looks every 2 s while something changes by itself (installing, searching, starting) …
let zigbeeEvery = 0;        // … else every 15 s
let zigbeeAsked = 0;        // when the Pi itself was last asked („Neu prüfen“)
const ZIGBEE_PHASES = { check: "Prüfe den Pi", mosquitto: "Richte Mosquitto ein", node: "Lade Node.js", pnpm: "Lade pnpm",
  zigbee2mqtt: "Lade Zigbee2MQTT", dependencies: "Lade die Bausteine von Zigbee2MQTT", build: "Baue Zigbee2MQTT",
  finish: "Räume auf", remove: "Entferne die Programme" };
const ZIGBEE_DOING = { setup: "richte ein …", start: "starte …", stop: "halte an …" };

function zigbeeRunning() { return !!(zigbee && zigbee.job && ["waiting", "running"].includes(zigbee.job.state)); }
function zigbeeBusy() {     // a quick look at the Pi ("status") does not count: the server waits for it
  return !!zigbeePending || !!(zigbee && zigbee.busy && zigbee.busy !== "status") || zigbeeRunning();
}
function zigbeeStarting() { // Zigbee2MQTT should run, Mosquitto is there, but Zigbee2MQTT has not said "online" yet
  const p = zigbee && zigbee.programs, net = zigbee && zigbee.net;
  return !!(p && net && p.service.present && (p.service.enabled || p.service.active) && !p.service.failed &&
    net.broker === "connected" && net.bridge !== "online");
}
function zigbeeLive() {
  if (zigbeeRunning() || zigbeeStarting()) return true;
  const net = zigbee && zigbee.net;
  return !!(net && ((net.search && net.search.active) ||
    (net.devices || []).some(d => d.interview === "pending" || d.interview === "running")));
}

async function loadZigbee() {
  try { zigbee = await api("GET", "api/zigbee"); zigbeeError = null; }
  catch (e) { zigbeeError = e.message; }
  // still not running after half a minute: ask the Pi – if it fails, systemd knows and the page shows its messages
  if (zigbeeStarting() && !zigbeeBusy() && Date.now() - zigbeeAsked > 30000) zigbeeDo("refresh", { quiet: true });
  followZigbee();
  render();
}

function followZigbee() {
  const every = !(zigbee && zigbee.supported) ? 0 : zigbeeLive() ? 2000 : 15000;
  if (zigbeeTimer && every !== zigbeeEvery) { clearInterval(zigbeeTimer); zigbeeTimer = null; }
  if (every && !zigbeeTimer) zigbeeTimer = setInterval(() => { if (!document.hidden && zigbeeInView()) loadZigbee(); }, every);
  zigbeeEvery = every;
}

function zigbeeInView() {   // „Verbindungen“ › „Zigbee-Funk“ – and „Geräte“ and a room, which show the Zigbee devices
                            // and add new ones
  return view === "setup" && (tab === "devices" || (tab === "rooms" && roomView != null)
    || (tab === "connections" && connTab === "zigbee"));
}

async function openZigbeeTab() {  // fresh state from the Pi: the stick may have been plugged in since
  await loadZigbee();
  if (zigbee && zigbee.supported && !zigbeeBusy()) zigbeeDo("refresh", { quiet: true });
}

async function zigbeeDo(action, { quiet = false, path = action, body } = {}) {
  zigbeePending = action;
  if (action === "refresh") zigbeeAsked = Date.now();
  render();
  try {
    const res = await api("POST", "api/zigbee/" + path, body);
    zigbee = res.zigbee;
    if (!quiet && res.message) toast(res.message);
  } catch (e) {
    if (!quiet || action !== "refresh") toast(e.message, true);
    try { zigbee = await api("GET", "api/zigbee"); } catch (_) { /* shown with the next look */ }
  }
  if (action !== "refresh") zigbeeAsked = Date.now();   // the answer brought the Pi's state along
  zigbeePending = null;
  followZigbee();
  invalidate();
  render();
}

async function zigbeeInstall() {
  const update = !!(zigbee && zigbee.programs && zigbee.programs.complete);
  const yes = await ask({ title: update ? "Zigbee-Programme aktualisieren?" : "Zigbee-Programme installieren?",
    text: (update ? "Der Pi lädt die neueren Versionen und prüft jede Datei. "
      : "Der Pi lädt dafür Mosquitto (aus dem Paketarchiv von Raspberry Pi OS), Node.js (von nodejs.org) und Zigbee2MQTT " +
        "mit seinen Bausteinen (von GitHub und npm) – zusammen etwa 200 MB – und prüft jede Datei. ") +
      "Das dauert etwa 10 bis 20 Minuten. Die Heizungen heizen dabei ganz normal weiter, und du kannst diese Seite " +
      "in der Zeit schließen.",
    buttons: [{ label: "Abbrechen", value: null }, { label: update ? "Aktualisieren" : "Installieren", value: "yes", cls: "primary" }] });
  if (!yes) return;
  zigbeeSeen = null;
  zigbeeDo("install", { quiet: true });
}

async function zigbeeRemove() {
  const yes = await ask({ title: "Zigbee-Programme entfernen?",
    text: "Zigbee2MQTT, Node.js und Mosquitto werden wieder vom Pi gelöscht. Zigbee-Thermostate lassen sich danach nicht mehr " +
      "steuern, bis die Programme wieder installiert sind. Angelernte Thermostate bleiben gespeichert.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  zigbeeSeen = null;
  zigbeeDo("remove", { quiet: true });
}

function zigbeeSticks(p) { return p.ports.filter(x => x.stick); }

async function zigbeeSetup() {
  const p = zigbee && zigbee.programs;
  if (!p) return;
  const sticks = zigbeeSticks(p), change = p.service.present;
  const port = sticks.length === 1 ? sticks[0].id : p.network.port;
  const channel = await ask({ title: change ? "Funkkanal ändern" : "Zigbee einrichten",
    text: "Zigbee funkt wie das WLAN auf 2,4 GHz. Damit sich beide nicht stören, bekommt Zigbee einen eigenen Funkkanal. " +
      "Welchen Kanal dein WLAN nutzt, zeigt die FRITZ!Box unter WLAN › Funkkanal.",
    html: `<ul class="zigbee-channels">
        <li><b>Kanal 25</b> passt, wenn dein WLAN auf Kanal 1 bis 10 funkt – das ist meistens so.</li>
        <li><b>Kanal 15</b> passt, wenn dein WLAN auf Kanal 7 bis 13 funkt.</li>
        <li><b>Kanal 20</b> passt, wenn dein WLAN auf Kanal 1 bis 5 oder 12 bis 13 funkt.</li>
      </ul>
      <p class="hint">Gibt es im Haus Philips Hue, nimm einen anderen Kanal als die Hue-Bridge – welchen sie nutzt, steht in der Hue-App.
        ${change ? "Ein neuer Kanal heißt neues Zigbee-Netz: Das geht nur, solange kein Gerät angelernt ist."
          : "Später lässt sich der Kanal nur ändern, solange noch kein Gerät angelernt ist."}</p>`,
    buttons: [{ label: "Abbrechen", value: null }, { label: "Kanal 15", value: 15 }, { label: "Kanal 20", value: 20 },
      { label: "Kanal 25", value: 25, cls: "primary" }] });
  if (!channel) return;
  zigbeeDo("setup", { body: { channel, port: port || null } });
}

async function zigbeeStop() {
  const yes = await ask({ title: "Zigbee2MQTT anhalten?",
    text: "Solange Zigbee2MQTT angehalten ist, erreicht der Pi keine Zigbee-Geräte. Die Thermostate behalten so lange ihre " +
      "letzte Einstellung. Zigbee2MQTT bleibt auch nach einem Neustart des Pi aus, bis du hier „Starten“ drückst.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Anhalten", value: "yes", cls: "danger-solid" }] });
  if (yes) zigbeeDo("stop", { path: "service", body: { on: false } });
}

async function zigbeeSearch(on) {
  zigbeeNetPending = on ? "search" : "unsearch";
  render();
  try { zigbee = (await api("POST", "api/zigbee/search", { on })).zigbee; }
  catch (e) { toast(e.message, true); }
  zigbeeNetPending = null;
  followZigbee();
  invalidate();
  render();
}

async function zigbeeRemoveDevice(ieee, force) {   // null when done, else the error
  zigbeeNetPending = ieee;
  render();
  let error = null;
  try {
    const res = await api("POST", `api/zigbee/devices/${ieee}/remove`, { force });
    zigbee = res.zigbee;
    if (res.message) toast(res.message);
  } catch (e) { error = e; }
  zigbeeNetPending = null;
  invalidate();
  render();
  return error;
}

async function zigbeeForget(ieee) {
  const d = ((zigbee && zigbee.net && zigbee.net.devices) || []).find(x => x.ieee === ieee);
  if (!d) return;
  const it = d.thermostat ? "das Thermostat" : d.plug ? "die Steckdose" : d.light ? "die Lampe" : "das Gerät";
  const pron = d.plug || d.light ? "sie" : "es";
  const owner = d.plug && ((state && state.heaters) || []).find(h => h.plug && h.plug.id === ieee);
  const yes = await ask({ title: d.thermostat ? "Thermostat entfernen?" : d.plug ? "Steckdose entfernen?" : d.light
    ? "Lampe entfernen?" : "Gerät entfernen?",
    text: `Der Pi meldet ${it} im Zigbee-Netz ab.${owner ? ` Sie gehört dann auch nicht mehr zu ${owner.name}.` : ""} Willst du ${
      pron} später wieder nutzen, lernst du ${pron} einfach neu an.`,
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (yes) await zigbeeUnpair(ieee, it, pron);
}

async function zigbeeUnpair(ieee, it, pron = "es", them = pron) {   // true when it is gone; it: "das Thermostat",
                                                                    // "die Steckdose", "den Schalter" or "das Gerät"
  const error = await zigbeeRemoveDevice(ieee, false);
  if (!error) return true;
  if (![502, 504].includes(error.status)) { toast(error.message, true); return false; }   // forcing only helps if the device did not answer
  const force = await ask({ title: "Trotzdem entfernen?",
    text: `${error.message} Du kannst ${it} trotzdem entfernen: Dann vergisst der Pi ${them}, ohne auf die Antwort zu warten. ` +
      `Meldet ${pron} sich später noch einmal, erscheint ${pron} wieder in der Liste.`,
    buttons: [{ label: "Abbrechen", value: null }, { label: "Trotzdem entfernen", value: "yes", cls: "danger-solid" }] });
  if (!force) return false;
  const again = await zigbeeRemoveDevice(ieee, true);
  if (again) toast(again.message, true);
  return !again;
}

function zigbeeSize(mb) { return mb >= 1024 ? (mb / 1024).toFixed(1).replace(".", ",") + " GB" : mb + " MB"; }

function zigbeeStateText(p) {   // [dot, words]
  if (!p.arch_ok) return ["offline", "Für Zigbee braucht der Pi ein 64-Bit-System (Raspberry Pi OS 64-bit)"];
  if (!p.complete) return ["", Object.values(p.installed).some(Boolean) ? "Nicht vollständig installiert" : "Noch nicht installiert"];
  if (!p.current) return ["on", "Installiert – neuere Versionen sind da"];
  if (!p.mosquitto.active) return ["offline", "Installiert, aber Mosquitto läuft nicht"];
  return ["ok", "Installiert"];
}

function zigbeePortName(id) {   // usb-SONOFF_SONOFF_Dongle_Plus_MG24_8a2c1d0e-if00-port0 → SONOFF Dongle Plus MG24
  const words = id.replace(/^usb-/, "").replace(/-if\d+(-port\d+)?$/, "").split("_").filter(Boolean);
  const last = words[words.length - 1] || "";
  if (words.length > 1 && last.length >= 6 && /\d/.test(last) && /^[0-9A-Za-z]+$/.test(last)) words.pop();   // serial number
  return words.filter((w, i) => i === 0 || w.toLowerCase() !== words[i - 1].toLowerCase()).join(" ") || id;
}

function zigbeeStickText(p) {
  const sticks = zigbeeSticks(p);
  if (sticks.length) return "eingesteckt – " + sticks.map(x => zigbeePortName(x.id)).join(", ");
  if (p.ports.length) return "nicht erkannt (eingesteckt: " + p.ports.map(x => zigbeePortName(x.id)).join(", ") + ")";
  return "nicht eingesteckt";
}

function zigbeeFactsHTML(p) {
  const row = (part, name) => {
    const have = p.installed[part], want = p.wanted[part];
    return `<dt>${name}</dt><dd>${have ? esc(have) : "–"}${have && want && have !== want ? ` <span class="tag warn">neu: ${esc(want)}</span>` : ""}</dd>`;
  };
  return `<dl class="about-facts">
    ${row("zigbee2mqtt", "Zigbee2MQTT")}
    ${row("node", "Node.js")}
    <dt>Mosquitto</dt><dd>${p.mosquitto.version ? `${esc(p.mosquitto.version)} · ${p.mosquitto.active ? "läuft" : "läuft nicht"}` : "–"}</dd>
    <dt>Zigbee-Stick</dt><dd>${esc(zigbeeStickText(p))}</dd>
    ${p.free_mb != null ? `<dt>Speicherkarte</dt><dd>${esc(zigbeeSize(p.free_mb))} frei</dd>` : ""}
  </dl>`;
}

function zigbeeButtonsHTML(p, busy) {
  const b = (action, label, cls = "") => `<button class="${cls}" data-action="${action}" ${busy ? "disabled" : ""}>${label}</button>`;
  if (!p.arch_ok) return "";
  const any = Object.values(p.installed).some(Boolean) || !!p.mosquitto.version;
  if (!p.complete) return b("zigbee-install", "Zigbee-Programme installieren", "primary") + (any ? b("zigbee-remove", "Entfernen …") : "");
  if (!p.current) return b("zigbee-install", "Neue Versionen installieren", "primary") + b("zigbee-remove", "Entfernen …");
  if (!p.mosquitto.active) return b("zigbee-install", "Reparieren", "primary") + b("zigbee-remove", "Entfernen …");
  return b("zigbee-remove", "Entfernen …");
}

function zigbeeProgressHTML(j) {
  if (j.action !== "install") return `<p class="searching"><b>Entferne die Zigbee-Programme …</b></p><p class="muted">Einen Moment bitte.</p>`;
  const what = j.phase ? (ZIGBEE_PHASES[j.phase] || "Installiere") + " …" : "Gleich geht es los …";
  const share = j.step && j.steps ? Math.round(100 * (j.step - 1 + (j.progress || 0) / 100) / j.steps) : 0;
  return `<p class="searching"><b>${esc(what)}</b>${j.progress != null ? ` <span class="muted">${j.progress} %</span>` : ""}</p>
    <div class="progress" role="progressbar" aria-valuenow="${share}" aria-valuemin="0" aria-valuemax="100"><span style="width:${share}%"></span></div>
    <p class="muted">${j.step && j.steps ? `Schritt ${j.step} von ${j.steps}` : "Der Pi beginnt gleich"}${j.started ? ` · gestartet ${esc(fmtTime(j.started))}` : ""}.
      Das dauert etwa 10 bis 20 Minuten. Die Heizungen heizen dabei ganz normal weiter.</p>`;
}

function zigbeeStatusHTML(z) {
  const p = z.programs, j = z.job, busy = zigbeeBusy();
  let body;
  if (zigbeeRunning()) body = zigbeeProgressHTML(j);
  else if (!p) body = `<p class="${z.error ? "error-text" : "searching"}">${esc(z.error || "Frage den Pi …")}</p>`;
  else {
    const [dot, words] = zigbeeStateText(p);
    body = `<ul class="net-list"><li><span class="dot ${dot}"></span><span class="hname"><b>${esc(words)}</b>
        ${!p.arch_ok && p.arch ? `<span class="muted">(dieser hat „${esc(p.arch)}“)</span>` : ""}</span></li></ul>
      ${p.arch_ok ? zigbeeFactsHTML(p) : ""}
      ${z.error ? `<p class="error-text">${esc(z.error)}</p>` : ""}
      <div class="row">${zigbeeButtonsHTML(p, busy)}</div>`;
  }
  const failed = j && !zigbeeRunning() && j.state === "failed" && j.id !== zigbeeSeen && j.finished &&
    Date.now() - Date.parse(j.finished) < 60 * 60 * 1000;
  return `<div class="card zigbee-status">
    <div class="card-head"><h2>Zigbee-Programme</h2>
      <button class="small" data-action="zigbee-refresh" ${busy ? "disabled" : ""}>${zigbeePending === "refresh" ? "frage …" : "Neu prüfen"}</button></div>
    ${body}
    ${failed ? `<div class="wifi-note bad"><p><b>${j.action === "remove" ? "Entfernen" : "Installieren"} hat nicht geklappt.</b></p>
      <p>${esc(j.message || "")}</p>
      ${j.details ? `<details><summary>Meldungen des Pi</summary><pre class="zigbee-log">${esc(j.details)}</pre></details>` : ""}
      <button class="small" data-action="zigbee-ok">OK</button></div>` : ""}
    ${z.at && !zigbeeRunning() ? `<p class="hint">Stand ${esc(fmtTime(z.at))}</p>` : ""}
  </div>`;
}

function zigbeeNetState(p, net) {   // [dot, words] for Zigbee2MQTT
  const s = p.service;
  if (s.failed) return ["offline", "Startet nicht"];
  if (!s.enabled && !s.active) return ["", "Angehalten"];
  if (net.broker === "error") return ["offline", "Keine Verbindung zu Mosquitto"];
  if (net.broker !== "connected") return ["on", "Verbinde mit Mosquitto …"];
  if (net.bridge === "online") return ["ok", "Läuft"];
  return ["on", "Startet …"];
}

function zigbeeNetHTML(z) {
  const p = z.programs, net = z.net || {};
  if (!p || !p.complete || zigbeeRunning()) return "";
  const s = p.service, n = p.network, busy = zigbeeBusy(), sticks = zigbeeSticks(p);
  const b = (action, label, cls = "") => `<button class="${cls}" data-action="zigbee-${action}" ${busy ? "disabled" : ""}>${
    zigbeePending === action ? ZIGBEE_DOING[action] : label}</button>`;
  let body;
  if (!s.present) {
    body = sticks.length
      ? `<p>Der Stick steckt (${esc(zigbeePortName(sticks[0].id))}). Jetzt noch einen Funkkanal wählen – dann startet
          Zigbee2MQTT, und die Thermostate lassen sich anlernen.</p>
        <div class="row">${b("setup", "Zigbee einrichten …", "primary")}</div>`
      : `<p>Bitte den Zigbee-Stick (SONOFF Dongle Plus MG24) in einen USB-Anschluss des Pi stecken – am besten über ein kurzes
          USB-Verlängerungskabel, ein Stück weg vom Pi. Dann oben „Neu prüfen“ drücken.</p>`;
  } else {
    const [dot, words] = zigbeeNetState(p, net);
    const online = net.bridge === "online", count = online ? (net.devices || []).length : n.devices;
    const stickIn = !n.port || p.ports.some(x => x.id === n.port);
    const buttons = s.failed ? b("start", "Neu starten", "primary") + b("stop", "Anhalten")
      : s.enabled || s.active ? b("stop", "Anhalten") : b("start", "Starten", "primary");
    body = `<ul class="net-list"><li><span class="dot ${dot}"></span><span class="hname"><b>${esc(words)}</b></span></li></ul>
      <dl class="about-facts">
        <dt>Funkkanal</dt><dd>${esc(String(n.channel || net.channel || "–"))}</dd>
        <dt>Stick</dt><dd>${n.port ? esc(zigbeePortName(n.port)) : "–"}${stickIn ? "" : ` <span class="tag bad">steckt nicht</span>`}</dd>
        <dt>Angelernt</dt><dd>${count === 1 ? "1 Gerät" : `${count} Geräte`}</dd>
        ${net.version ? `<dt>Zigbee2MQTT</dt><dd>${esc(net.version)}</dd>` : ""}
      </dl>
      ${online ? `<p class="hint">Die Geräte stehen unter „Geräte“. Ein neues Thermostat oder eine neue Steckdose lernst du dort
        mit „+ Gerät hinzufügen“ an.</p>` : ""}
      ${zigbeeStarting() ? `<p class="hint">Das dauert etwa eine halbe Minute.</p>` : ""}
      ${s.failed ? `<p class="error-text">Zigbee2MQTT startet nicht${stickIn ? "" : " – der Stick steckt nicht"}.</p>` : ""}
      ${s.log ? `<details><summary>Meldungen von Zigbee2MQTT</summary><pre class="zigbee-log">${esc(s.log)}</pre></details>` : ""}
      ${net.broker === "error" && net.problem ? `<p class="error-text">${esc(net.problem)}</p>` : ""}
      <div class="row">${buttons}${count === 0 && !s.failed ? b("setup", "Funkkanal ändern …") : ""}</div>`;
  }
  return `<div class="card zigbee-net"><div class="card-head"><h2>Zigbee-Netz</h2></div>${body}</div>`;
}

function zigbeeSignal(q) { return q == null ? "" : q >= 100 ? "Funk gut" : q >= 40 ? "Funk mittel" : "Funk schwach"; }

function zigbeeDeviceState(d) {   // [dot, words]
  if (d.interview === "pending" || d.interview === "running") return ["on", "wird angelernt …"];
  if (d.interview === "failed") return ["offline", "Anlernen hat nicht geklappt – bitte noch einmal suchen und am Gerät wieder die Taste drücken (Thermostat: mittlere Taste 2 Sekunden, Steckdose: 5 Sekunden; eine Lampe noch einmal zurücksetzen)"];
  if (!d.supported) return ["offline", "wird von Zigbee2MQTT nicht unterstützt"];
  return ["ok", "bereit"];
}

function plugName(ieee) { return "Steckdose " + ieee.slice(-4).toUpperCase(); }   // as the server names it

function zigbeeKindWord(d) {
  return d.thermostat ? "Thermostat" : d.plug ? "Steckdose" : d.light ? "Lampe" : d.button ? "Schalter"
    : d.detector ? "Bewegungsmelder" : "Unbekanntes Gerät";
}

function zigbeeDeviceHTML(d) {
  const [dot, words] = zigbeeDeviceState(d);
  const model = [d.vendor, d.model].filter(Boolean).join(" ") || zigbeeKindWord(d);
  const heaters = (state && state.heaters) || [];
  const named = heaters.find(h => h.id === d.ieee && h.custom_name);   // its name in the app
  const lamp = d.light && ((state && state.lights) || []).find(l => l.id === d.ieee);
  const keep = s => s.replace(/ /g, " ");   // "20,5 °C" and the like stay on one line
  const plug = d.plug && d.interview === "done" && d.supported;   // a plug that is ready: its switch, power and day's energy
  const facts = [words, d.battery != null ? keep(`Batterie ${Math.round(d.battery)} %`) : "",
    plug && d.switch ? (d.switch === "ON" ? "an" : "aus") : "", plug && d.power_w != null ? keep(wattText(d.power_w)) : "",
    plug && d.energy_today != null ? keep(`heute ${kwhText(d.energy_today)}`) : "",
    lamp && !lamp.lost && lamp.state ? keep(lightFacts(lamp)) : "", keep(zigbeeSignal(d.signal)),
    d.temperature != null ? keep(deg(d.temperature)) : ""].filter(Boolean).join(" · ");
  const title = named ? named.custom_name : d.plug ? plugName(d.ieee) : lamp ? lamp.name : model;
  return `<li data-ieee="${esc(d.ieee)}"><span class="dot ${dot}"></span><span class="hname"><b>${esc(title)}</b>${
      title !== model ? ` <span class="muted">${esc(model)}</span>` : ""}
      ${d.new ? `<span class="tag ok">neu</span>` : ""}
      <span class="muted zigbee-facts">${esc(facts)}</span>
      <span class="muted zigbee-ieee">${esc(d.ieee)}</span></span>
    <button class="small" data-action="zigbee-forget" ${zigbeeNetPending || zigbeeBusy() ? "disabled" : ""}>${
      zigbeeNetPending === d.ieee ? "entferne …" : "Entfernen …"}</button></li>`;
}

function zigbeeAboutHTML() {
  return `<div class="card">
    <h2>Wofür ist das?</h2>
    <p>Die SONOFF-Thermostate (TRV Gen2) funken mit <b>Zigbee</b>. Der Pi spricht mit ihnen über einen USB-Stick
      (SONOFF Dongle Plus MG24). Dafür braucht er drei freie Zusatzprogramme:</p>
    <ul class="zigbee-parts">
      <li><b>Zigbee2MQTT</b> spricht über den Stick mit den Thermostaten.</li>
      <li><b>Mosquitto</b> reicht die Nachrichten zwischen Zigbee2MQTT und dieser App weiter – nur hier auf dem Pi, mit Passwort.</li>
      <li><b>Node.js</b> – darauf läuft Zigbee2MQTT.</li>
    </ul>
    <p class="hint">Welche Versionen der Pi lädt, legt die jeweilige Version von FlowThermPi fest, und er prüft jede Datei.
      Nach einem Update von FlowThermPi kann hier deshalb „Neue Versionen installieren“ erscheinen.</p>
  </div>`;
}

function zigbeeHTML() {   // „Verbindungen“ › „Zigbee-Funk“: programs, stick, channel – the devices are under „Geräte“
  const z = zigbee;
  if (!z) return `<div class="card"><p class="${zigbeeError ? "error-text" : "searching"}">${esc(zigbeeError || "Frage den Pi …")}</p></div>`;
  if (!z.supported) return `<div class="card"><p>Zigbee gibt es nur, wenn FlowThermPi mit dem
    Installationsbefehl auf dem Raspberry Pi läuft.</p></div>`;
  return zigbeeStatusHTML(z) + zigbeeNetHTML(z) + zigbeeAboutHTML();
}

// ------------------------------------------------------------------ events
function planOf(t) { return state.plans.find(p => p.id === t.closest("[data-plan]").dataset.plan); }
function dayOf(t) { return t.closest(".day").dataset.day; }

document.addEventListener("click", e => {
  if (e.target.id === "dialog") return closeDialog(null);   // tap next to the dialog = cancel
  if (e.target.id === "history") return closeHistory();     // tap next to the Verlauf = close
  if (e.target.id === "lightsheet") return closeLightSheet();   // … next to „Einstellen“ too
  if (!e.target.closest("button, a, select, input, label, summary")) {   // a tap on a room's name, or anywhere on a closed
    const at = e.target.closest("article.tile .tile-head, article.tile.collapsed");   // tile: it opens or closes
    if (at) return toggleTile(at.closest("article.tile"));
  }
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
    case "plug-on": return plugOn(groupFor(tile), t.dataset.heater);
    case "light-room": return lightRoom(groupFor(tile), t.dataset.on === "1");
    case "light-dim": return lightDim(groupFor(tile), Number(t.dataset.step));
    case "light-one": return lightOne(t.closest("[data-light]").dataset.light, t.dataset.on === "1");
    case "light-sheet": return openLightSheet(groupFor(tile));
    case "ls-close": return closeLightSheet();
    case "lamp-step": return lampSet(t.closest("[data-light]").dataset.light, { step: Number(t.dataset.step) });
    case "lamp-white": return lampSet(t.closest("[data-light]").dataset.light, { white: t.dataset.white });
    case "lamp-hue": return lampSet(t.closest("[data-light]").dataset.light, { hue: t.dataset.hue });
    case "notice-ok": return noticeOk(t.dataset.notice);
    case "tile-toggle": return toggleTile(tile);
    case "tiles-all": return setTilesOpen(homeGroups().map(keyOf), t.dataset.open === "1");
    case "help-start": return helpStart();
    case "help-end": return helpEnd();
    case "logs-save": return logsBusy ? undefined : logsSave();
    case "history": return openHistory(t.closest("article.tile, article.card"), view);
    case "hist-close": return closeHistory();
    case "hist-day": return loadHistory(t.dataset.day);
    case "power-day": return t.dataset.day ? loadPower(t.dataset.day) : undefined;
    case "power-plug": return openPlugEnergy(t.dataset.plug);
    case "run-open": return openRun(t.dataset.plug, t.dataset.start);
    case "costs-save": return saveCosts();
    case "costs-cancel": costForm = null; return render();
    case "cpower-take": editCosts().powers[t.dataset.heater] = t.dataset.watts; return render();   // saved with „Speichern“
  }
  if (t.dataset.subtab) {
    deviceTab = t.dataset.subtab;
    localStorage.setItem("heizung.deviceTab", deviceTab);
    render();
    return;
  }
  if (t.dataset.tab) {
    closeRoom();   // a tab – "Räume" too – starts at its top: the list of rooms
    tab = t.dataset.tab;
    localStorage.setItem("heizung.tab", tab);
    if (tab === "plans" && editing) renderEditor();
    openTab();
    render();
    return;
  }
  if (t.dataset.powerspan) {
    powerSpan = t.dataset.powerspan;
    localStorage.setItem("heizung.powerSpan", powerSpan);
    loadPower(state.now.slice(0, 10));   // a new span starts at today
    return;
  }
  if (t.dataset.conntab) {
    connTab = t.dataset.conntab;
    localStorage.setItem("heizung.connTab", connTab);
    openConnTab();
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
    case "room-edit": return openRoomForm(t.dataset.room || card.dataset.id);
    case "room-open": return openRoom(t.dataset.room);
    case "room-back": closeRoom(); return render();
    case "room-go": return goToRoom(t.dataset.room);
    case "room-sec": return toggleRoomSection(t.dataset.sec);
    case "room-cancel": roomForm = null; return render();
    case "room-color":   // Safari leaves the focus in the name field – the form would not be drawn again
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      roomForm.color = t.dataset.color;
      return render();
    case "room-save": return saveRoom();
    case "room-delete": return deleteRoom(roomForm && roomForm.id);
    case "clock-one": return syncClock(t.closest(".device").dataset.heater);
    case "clock-all": return syncClock(null);
    case "import-plans": return importPlans(t.closest(".device").dataset.heater);
    case "dev-filter":
      deviceFilter = t.dataset.filter;
      localStorage.setItem("heizung.deviceFilter", deviceFilter);
      return render();
    case "add-open": return openAdd(t.dataset.home || null);
    case "add-close": return closeAdd();
    case "add-back": return addBack();
    case "add-kind": return addKind(t.dataset.kind);
    case "add-manual": return openDeviceForm(null);
    case "add-next": return addWhere(t.dataset.type, t.closest("[data-ieee]").dataset.ieee);
    case "add-finish": return addFinish();
    case "add-to-zigbee": return addToZigbee();
    case "add-zigbee-again": return addZigbeeSearch();
    case "scan-run": return scan && scan.running ? undefined : runScan(null);
    case "scan-other": return runScan(scanNet.trim() || null);
    case "scan-add": return openDeviceForm(null, { address: t.dataset.address, hostname: t.dataset.hostname });
    case "plug-search": return plugSearch && plugSearch.running ? undefined : runPlugSearch();
    case "plug-adopt": return adoptPlug(t.dataset.plug);
    case "plug-remove": return removePlug(t.closest("[data-plug]").dataset.plug);
    case "plug-rename": return renamePlug(t.closest("[data-plug]").dataset.plug);
    case "plug-switch": return switchPlug(t.closest("[data-plug]").dataset.plug, t.dataset.on === "1");
    case "plug-energy": return openPlugEnergy(t.closest("[data-plug]").dataset.plug);
    case "light-rename": return renameLight(t.closest("[data-light]").dataset.light);
    case "light-remove": return removeLight(t.closest("[data-light]").dataset.light);
    case "button-rename": return renameButton(t.closest("[data-button]").dataset.button);
    case "detector-rename": return renameDetector(t.closest("[data-detector]").dataset.detector);
    case "detector-remove": return removeDetector(t.closest("[data-detector]").dataset.detector);
    case "button-remove": return removeButton(t.closest("[data-button]").dataset.button);
    case "sensor-search": return sensorSearch && sensorSearch.running ? undefined : runSensorSearch();
    case "sensor-adopt": return openSensorForm(null, t.dataset.pill, Number(t.dataset.channel));
    case "sensor-edit": return openSensorForm(t.closest(".device").dataset.sensor);
    case "sensor-refresh": return refreshSensor(t.closest(".device").dataset.sensor);
    case "pill-setup": return openPillForm(t.dataset.pill, t.closest(".device") ? t.closest(".device").dataset.sensor : "search");
    case "pill-mode-save": return pillAction("mode");
    case "pill-restart": return pillAction("restart");
    case "pill-cancel": pillForm = null; invalidate(); return render();
    case "sensor-cancel": sensorForm = null; invalidate(); return render();
    case "sensor-save": return saveSensor();
    case "sensor-delete": return deleteSensor();
    case "crun-start": return calibRun(t.closest(".device").dataset.sensor, "start");
    case "crun-finish": return calibRun(t.closest(".device").dataset.sensor, "finish");
    case "crun-discard": return calibRun(t.closest(".device").dataset.sensor, "discard");
    case "crun-apply": return calibRun(t.closest(".device").dataset.sensor, "apply", t.dataset.heater);
    case "dev-edit": return openDeviceForm(t.closest(".device").dataset.heater);
    case "dev-cancel": deviceForm = null; return render();
    case "dev-save": return saveDevice();
    case "dev-delete": return deleteDevice();
    case "calib-open": return openCalib(t.closest(".device").dataset.heater);
    case "trv-regulation":   // the chosen one again: nothing to do
      return t.classList.contains("active") ? undefined : setRegulation(t.closest(".device").dataset.heater, t.dataset);
    case "trv-firmware": return startFirmware(t.closest(".device").dataset.heater);
    case "calib-cancel": calibForm = null; return render();
    case "calib-save": return saveCalib(null);
    case "calib-reset": return saveCalib({ correction: 0 });
    case "update-check": return checkUpdates();
    case "update-start": return startUpdate();
    case "wifi-scan": return scanWifi();
    case "wifi-pick": return openWifiForm(t.dataset.ssid);
    case "wifi-hidden": return openWifiForm("", true);
    case "wifi-cancel": wifiForm = null; invalidate(); return render();
    case "wifi-connect": return connectWifi();
    case "wifi-forget": return forgetWifi(t.dataset.ssid);
    case "wifi-ok": wifiSeen = wifi && wifi.job && wifi.job.id; return render();
    case "remote-refresh": return remoteDo("refresh");
    case "remote-setup": return remoteSetup();
    case "remote-on": return remoteDo("up");
    case "remote-off": return remoteOff();
    case "remote-logout": return remoteLogout();
    case "remote-cancel": return remoteDo("logout", { okText: "Anmeldung abgebrochen." });
    case "remote-ok": remoteSeen = remote && remote.job && remote.job.id; return render();
    case "remote-mode": return remoteMode(t.dataset.mode);
    case "zigbee-refresh": return zigbeeDo("refresh");
    case "zigbee-install": return zigbeeInstall();
    case "zigbee-remove": return zigbeeRemove();
    case "zigbee-ok": zigbeeSeen = zigbee && zigbee.job && zigbee.job.id; return render();
    case "zigbee-setup": return zigbeeSetup();
    case "zigbee-start": return zigbeeDo("start", { path: "service", body: { on: true } });
    case "zigbee-stop": return zigbeeStop();
    case "zigbee-unsearch": return zigbeeSearch(false);
    case "zigbee-forget": return zigbeeForget(t.closest("[data-ieee]").dataset.ieee);
    case "logs-email": return logsEmailSave();
    case "logs-detail": return logsSettings({ detail: t.dataset.on === "1" });
  }
});

document.addEventListener("change", e => {
  const t = e.target;
  if (editing && t.closest(".win")) {
    const w = editing.days[dayOf(t)][Number(t.closest(".win").dataset.idx)];
    w[t.dataset.field] = t.dataset.field === "temp" ? parseFloat(String(t.value).replace(",", ".")) : t.value;
    return t.dataset.field === "temp" ? updateDayBar(t.closest(".day")) : renderEditor();
  }
  if (sensorForm && t.dataset.sfield) { sensorForm[t.dataset.sfield] = t.value; return; }
  if (pillForm && t.dataset.pfield) { pillForm[t.dataset.pfield] = t.value; return; }
  if (wifiForm && t.dataset.wfield === "show") {   // switch the field directly: a redraw would wait for the focus to leave
    wifiForm.show = t.checked;
    const pw = document.querySelector('.wifi-form input[data-wfield="password"]');
    if (pw) pw.type = t.checked ? "text" : "password";
    return;
  }
  if (t.dataset.cfield === "show" || t.dataset.cfield === "show_power") { editCosts()[t.dataset.cfield] = t.checked; return; }
  if (addFlow && t.dataset.afield) {   // what hangs on a plug, its heater: the heater is asked for only at „Heizung“,
    addFlow[t.dataset.afield] = t.type === "checkbox" ? t.checked : t.value;   // the room only without one
    if (t.dataset.afield === "heater" || t.dataset.afield === "appliance") { t.blur(); render(); }
    return;
  }
  if (deviceForm && t.closest(".device-form")) return updateDeviceForm(t);
  if (roomForm && t.dataset.field === "color" && t.closest(".room-form")) {   // „Eigene Farbe …“ chosen
    roomForm.color = t.value.toLowerCase();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();   // may be the name
    return render();
  }
  if (roomForm && t.closest(".room-form")) return updateRoomForm(t);
  if (t.closest("[data-motion-room]")) return motionChange(t);   // „Licht bei Bewegung“
  switch (t.dataset.action) {
    case "room-plan": t.blur(); return assignPlan(t.closest("article.card"), t.value);
    case "heater-room": {
      const id = t.closest(".device").dataset.heater, h = state.heaters.find(x => x.id === id);
      return patch(`api/heaters/${encodeURIComponent(id)}`, { room: t.value || null }, movedText(h ? h.name : "", t.value));
    }
    case "sensor-room": {
      const id = t.closest(".device").dataset.sensor, s = (state.sensors || []).find(x => x.id === id);
      return patch(`api/sensors/${encodeURIComponent(id)}`, { room: t.value || null }, movedText(s ? s.name : "", t.value));
    }
    case "plug-heater": return setPlugHeater(t.closest("[data-plug]").dataset.plug, t.value);
    case "plug-room": return setPlugRoom(t.closest("[data-plug]").dataset.plug, t.value);
    case "plug-appliance": return setPlugAppliance(t.closest("[data-plug]").dataset.plug, t.value);
    case "light-room-set": return setLightRoom(t.closest("[data-light]").dataset.light, t.value);
    case "button-room-set": return setButtonRoom(t.closest("[data-button]").dataset.button, t.value);
    case "detector-room-set": return setDetectorRoom(t.closest("[data-detector]").dataset.detector, t.value);
    case "detector-sens": t.blur(); return setDetector(t.closest("[data-detector]").dataset.detector, { sensitivity: t.value });
    case "detector-led": return setDetector(t.closest("[data-detector]").dataset.detector, { led: t.checked });
    case "light-color-set": t.blur(); return setLightColor(t.closest("[data-light]").dataset.light, t.value);
    case "light-level-set": t.blur(); return setLightLevel(t.closest("[data-light]").dataset.light, t.value);
    case "lamp-range":   // let go of a slider: now the lamp gets the value
      lampDrag = false;
      t.blur();
      return lampSet(t.closest("[data-light]").dataset.light, { [t.dataset.what]: Number(t.value) });
    case "crun-hours": calibHours[t.closest(".device").dataset.sensor] = Number(t.value); t.blur(); return;
  }
});

function movedText(name, roomId) {   // a device in another room – said like the server says it for a lamp or a plug
  const r = state.rooms.find(x => x.id === roomId);
  return !name ? "Raum zugeordnet." : r ? `„${name}“ ist jetzt im Raum „${r.name}“.` : `„${name}“ ist in keinem Raum mehr.`;
}

function updateDeviceForm(t) {
  const field = t.dataset.field;
  if (field) deviceForm[field] = t.type === "checkbox" ? t.checked : t.value;
}

function updateRoomForm(t) {
  if (t.dataset.field === "name") roomForm.name = t.value;
  if (t.dataset.field === "color") roomForm.color = t.value.toLowerCase();   // while the colour is being picked
  if (t.dataset.heater) {
    const id = t.dataset.heater;
    roomForm.heaters = roomForm.heaters.filter(x => x !== id).concat(t.checked ? [id] : []);
  }
}

document.addEventListener("input", e => {
  if (e.target.dataset.action === "lamp-range") {   // the number follows the slider; the lamp gets it when it is let go
    const out = e.target.closest(".lamp-control").querySelector(`[data-lval="${e.target.dataset.what}"]`);
    if (out) out.textContent = `${e.target.value} ${e.target.dataset.what === "kelvin" ? "K" : "%"}`;
    return;
  }
  if (editing && e.target.id === "plan-name") editing.name = e.target.value;
  if (calibForm && e.target.closest(".calib-form")) {
    calibForm.measured = e.target.value;
    const h = state && state.heaters.find(x => x.id === calibForm.id);
    const preview = document.querySelector(".calib-preview");
    if (h && preview) preview.innerHTML = calibPreview(h, calibForm.measured);  // live, without re-rendering the form
    return;
  }
  if (e.target.dataset.field === "scan-network") scanNet = e.target.value;
  if (e.target.dataset.lfield === "email") { logsEmail = e.target.value; return; }
  if (e.target.dataset.cfield === "price") { editCosts().price = e.target.value; return; }
  if (e.target.dataset.cpower) { editCosts().powers[e.target.dataset.cpower] = e.target.value; return; }
  if (wifiForm && ["ssid", "password"].includes(e.target.dataset.wfield)) { wifiForm[e.target.dataset.wfield] = e.target.value; return; }
  if (addFlow && e.target.dataset.afield) { addFlow[e.target.dataset.afield] = e.target.value; return; }
  if (sensorForm && e.target.dataset.sfield) { sensorForm[e.target.dataset.sfield] = e.target.value; return; }
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
  if (hist && e.key === "Escape") return closeHistory();
  if (lightSheet && e.key === "Escape") return closeLightSheet();
  if (e.key === "Enter" && (e.target.dataset.cfield === "price" || e.target.dataset.cpower)) { e.target.blur(); return saveCosts(); }
  if (e.key === "Enter" && calibForm && e.target.closest(".calib-form")) return saveCalib(null);
  if (e.key === "Enter" && wifiForm && e.target.closest(".wifi-form")) return connectWifi();
  if (e.key === "Enter" && e.target.dataset.field === "scan-network") { e.target.blur(); return runScan(scanNet.trim() || null); }
  if (e.key === "Enter" && e.target.dataset.lfield === "email") { e.target.blur(); return logsEmailSave(); }
  if (e.key === "Enter" && addFlow && e.target.dataset.afield === "name") { e.target.blur(); return addFinish(); }
  if (e.key === "Enter" && deviceForm && e.target.closest(".device-form")) saveDevice();
  if (e.key === "Enter" && roomForm && e.target.closest(".room-form") && e.target.dataset.field === "name") saveRoom();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { searchAgain(true); refresh(); } });
window.addEventListener("popstate", () => {   // the phone's back button: from a room back to the list – and forward again
  const id = roomFromHash();
  if (id === roomView) return;
  leaveRoomState();
  roomView = id;
  if (id != null && state && state.app.zigbee && view === "setup") loadZigbee();
  invalidate();
  render();
  window.scrollTo(0, 0);
});
document.addEventListener("toggle", e => {   // „Stunde für Stunde“ stays open when the Verlauf is redrawn
  if (hist && e.target.classList && e.target.classList.contains("hist-table")) hist.table = e.target.open;
}, true);
window.addEventListener("resize", () => { cancelAnimationFrame(histResize); histResize = requestAnimationFrame(drawSheetChart); });
document.addEventListener("pointerdown", touchSetup, true);
document.addEventListener("keydown", touchSetup, true);

(async () => {
  const wanted = view;   // after a reload: back into the setup only if its session is still open
  showView("home");
  await refresh();
  if (wanted === "setup" && state && state.setup.unlocked) { showView("setup"); render(); }
})();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
setInterval(() => {   // today's Verlauf goes on every minute while it is open
  if (hist && !document.hidden && state && hist.day === state.now.slice(0, 10)) loadHistory();
}, 60000);

// a finger or the mouse on a slider of a lamp: the page is not redrawn under it (the refresh every few seconds)
document.addEventListener("pointerdown", e => { if (e.target.closest && e.target.closest(".lamp-range")) lampDrag = true; });
document.addEventListener("pointerup", () => { lampDrag = false; });
document.addEventListener("pointercancel", () => { lampDrag = false; });
