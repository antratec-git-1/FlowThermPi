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
let shownTab = null;     // tab last brought into view in the tab bar
let editing = null;      // plan in the editor: {id|null, name, days}
let roomForm = null;     // room form in "Räume": {mode: "new"|"edit", id, name, heaters: [ids]}
let deviceForm = null;   // heater form in "Geräte": {mode: "new"|"edit", id, name, host, token, hasToken, removeToken}
let deviceFilter = localStorage.getItem("heizung.deviceFilter") || "all";  // "all" | "none" | room id
let calibForm = null;    // calibration in "Geräte": {id, measured}
let scan = null;         // network search in "Geräte": {running} | {data} | {error}
let scanNet = "";        // network typed for "Anderes Netz"
let sensorSearch = null; // „Fühler suchen“ in "Geräte": {running, waiting, data: {networks, seconds, pills}, error}
let sensorForm = null;   // sensor form in "Geräte": {mode: "new"|"edit", id, pill, channel, name, room, placeholder}
let pillForm = null;     // „Einrichten“ of a Pill: {pill, at: "search"|sensor id, mode}
const calibHours = {};   // Kalibrierlauf: sensor id -> duration (hours) chosen before „starten“
let deviceTab = localStorage.getItem("heizung.deviceTab") === "sensors" ? "sensors" : "heaters";  // sub-tab of "Geräte"
const pillRestart = {};  // Pill id -> true while it restarts after „Einrichten“
const UPDATE_MAX_MS = 6 * 60 * 1000;   // stop waiting for an update after 6 minutes
let updateCheck = null;  // "Nach Updates suchen" in "Über": {running} | {result} | {error}
let updateRun = loadUpdateRun();   // update started in this browser: {from: build, at}; survives the reload
let loadedBuild = null;  // version|commit this page came with – another one answering means: reload
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
function localTime(iso) {  // "2026-11-21T20:06:59" – the server's local time, as the API sends it – as a Date
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)/.exec(iso || "");
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}
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
  view = name;
  if (name === "setup") sessionStorage.setItem("heizung.view", "setup");
  else sessionStorage.removeItem("heizung.view");
  document.body.className = "view-" + name;
  if (name === "setup" && tab === "wifi") openWifiTab();
  if (name === "setup" && tab === "remote") openRemoteTab();
  if (name === "setup" && tab === "zigbee") openZigbeeTab();
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
  const wifiTab = document.querySelector('#tabs [data-tab="wifi"]');
  if (state) wifiTab.hidden = !state.app.wifi;           // only on the Pi, set up with the installer
  if (state && tab === "wifi" && !state.app.wifi) tab = "rooms";
  const remoteTab = document.querySelector('#tabs [data-tab="remote"]');
  if (state) remoteTab.hidden = !state.app.remote;       // only on the Pi, set up with the installer
  if (state && tab === "remote" && !state.app.remote) tab = "rooms";
  const zigbeeTab = document.querySelector('#tabs [data-tab="zigbee"]');
  if (state) zigbeeTab.hidden = !state.app.zigbee;       // only on the Pi, set up with the installer
  if (state && tab === "zigbee" && !state.app.zigbee) tab = "rooms";
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  if (tab !== shownTab) {   // a tab bar that scrolls (phone): bring the chosen tab into view – only when it changes
    shownTab = tab;
    const b = document.querySelector('#tabs button[aria-selected="true"]'), nav = b && b.parentElement;
    if (nav && (b.offsetLeft < nav.scrollLeft || b.offsetLeft + b.offsetWidth > nav.scrollLeft + nav.clientWidth))
      nav.scrollLeft = b.offsetLeft - 8;
  }
  for (const name of ["rooms", "plans", "devices", "wifi", "remote", "zigbee", "about"]) document.getElementById("tab-" + name).hidden = name !== tab;
  if (!state) return;
  const el = document.getElementById("tab-" + tab);
  const focus = document.activeElement;
  if (el.contains(focus) && focus.matches("input, select")) return;  // never disturb typing
  if (tab === "rooms") renderRooms(el);
  else if (tab === "plans") { if (!editing) renderPlans(el); }       // the editor renders itself
  else if (tab === "about") renderAbout(el);
  else if (tab === "wifi") renderWifi(el);
  else if (tab === "remote") renderRemote(el);
  else if (tab === "zigbee") renderZigbee(el);
  else renderDevices(el);
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
function tempWord(t) { return t == null ? "aus" : degShort(t); }   // null = gap in the plan (frost protection)
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

const SENSOR_FRESH_MS = 15 * 60 * 1000;   // an older reading stays off the home page (the setup shows why)

function homeSensors(g) {  // the room's sensors with a fresh reading, for its home tile
  if (g.kind !== "room") return [];
  const now = serverNow().getTime();
  return (state.sensors || []).filter(s => {
    const at = localTime(s.measured_at);
    return s.room === g.id && s.temp != null && at && now - at.getTime() <= SENSOR_FRESH_MS;
  });
}

function homeSensorsHTML(list) {  // "Fühler: 21,3 °C · 48 % Luftfeuchte", one line per sensor
  return list.map(s => {
    const label = s.custom_name ? `Fühler „${esc(s.custom_name)}“` : list.length < 2 ? "Fühler"
      : /Fühler/.test(s.name) ? esc(s.name) : `Fühler ${esc(s.name)}`;   // "pill-1 · Fühler 2" names itself
    const humidity = s.humidity == null ? "" : ` · ${Math.round(s.humidity)} % Luftfeuchte`;
    return `<p class="tile-sensor">${label}: <b>${esc(deg(s.temp))}</b>${humidity}</p>`;
  }).join("");
}

function homeTile(g) {
  const key = keyOf(g);
  const online = g.heaters.filter(h => h.online);
  const temps = online.map(h => h.room_temp).filter(t => t != null);
  const sensors = homeSensors(g);
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
    <div class="tile-measure">
      <div class="tile-now"><span class="now-temp">${deg(temps.length ? avg(temps) : null)}</span><span class="now-label">${sensors.length ? "an der Heizung" : "im Raum"}</span></div>
      ${homeSensorsHTML(sensors)}
    </div>
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
  renderHelp();
  renderLogFoot();
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
  if ((editing || roomForm || deviceForm || calibForm || sensorForm || pillForm || (wifiForm && !wifiForm.job)) && !auto) {
    const yes = await ask({ title: "Einrichtung schließen?", text: "Nicht gespeicherte Änderungen gehen dabei verloren.",
      buttons: [{ label: "Weiter bearbeiten", value: null }, { label: "Schließen", value: "yes", cls: "primary" }] });
    if (!yes) return;
  }
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
    ${g.kind === "room" ? roomSensorsHTML(g) : ""}
    ${!writable && online.length ? '<p class="hint">Nur Anzeige: In heizungen.conf fehlt der Token.</p>' : ""}
  </article>`;
}

function roomSensorsHTML(g) {  // the room's sensors with their last reading (setup view)
  const list = (state.sensors || []).filter(s => s.room === g.id);
  if (!list.length) return "";
  return `<p class="hint">Fühler: ${list.map(s => esc(s.name) + (s.temp == null ? ""
    : ` <span class="muted">(${esc(deg(s.temp))}${s.measured_at ? ", gemessen " + esc(fmtTime(s.measured_at)) : ""})</span>`)).join(" · ")}</p>`;
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
    return `<div class="scan"><div class="scan-head"><strong>Fühler suchen</strong>
      <button class="small" data-action="sensor-close">Schließen</button></div><p class="error-text">${esc(s.error)}</p></div>`;
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
      <button class="small" data-action="sensor-search">Erneut suchen</button>
      <button class="small" data-action="sensor-close">Schließen</button></div>
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
  }
  s.running = false;
  if (sensorSearch !== s) return;
  invalidate();
  await refresh();
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
    sensorForm = { mode: "new", id: `${p.id}-${channel}`, pill: p.id, channel, name: "", room: "", where: p.address,
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
  const subtab = (id, label, count) => `<button data-subtab="${id}" role="tab" aria-selected="${deviceTab === id}">${label}
      <span class="count">${count}</span></button>`;
  const subtabs = `<div class="subtabs" role="tablist" aria-label="Geräteart">${subtab("heaters", "Heizung", state.heaters.length)}
      ${subtab("sensors", "Fühler", (state.sensors || []).length)}</div>`;
  if (deviceTab === "sensors") {
    return setHTML(el, `${subtabs}<article class="card"><div class="card-head"><h2>Raumfühler</h2>
      <button class="small primary" data-action="sensor-search" ${sensorSearch && sensorSearch.running ? "disabled" : ""}>Fühler suchen</button></div>
      <p class="hint">Als Raumfühler dient die <b>Shelly Pill</b> am USB-Netzteil, mit eingestecktem Temperaturfühler
      (DS18B20) oder einem DHT22. Sie schläft nie – die App liest sie jede Minute. „Fühler suchen“ findet Pills im WLAN;
      gefundene Fühler mit „Übernehmen“ speichern und einem Raum zuordnen. Eine neue Pill zuerst mit der Shelly-App ins
      WLAN bringen.</p>
      ${sensorSearch ? sensorPanelHTML() : ""}
      ${sensorForm && sensorForm.mode === "new" ? `<div class="device">${sensorFormHTML(sensorForm)}</div>` : ""}
      ${(state.sensors || []).map(s => sensorTileHTML(s, roomOptions)).join("")}
      ${!(state.sensors || []).length && !sensorForm ? '<p class="empty">Noch kein Fühler gespeichert.</p>' : ""}
    </article>`);
  }
  setHTML(el, `${subtabs}<article class="card"><div class="card-head"><h2>Heizungen</h2>
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

function renderWifi(el) {
  const w = wifi;
  if (!w) return setHTML(el, `<div class="card"><p class="${wifiError ? "error-text" : "searching"}">${esc(wifiError || "Frage den Pi …")}</p></div>`);
  if (!w.supported) return setHTML(el, `<div class="card"><p>Das WLAN lässt sich hier nur einrichten, wenn FlowThermPi mit dem
    Installationsbefehl auf dem Raspberry Pi läuft.</p></div>`);
  setHTML(el, wifiStatusHTML(w) + wifiNetworksHTML(w));
  if (wifiFocus) {
    wifiFocus = false;
    const input = el.querySelector('.wifi-form input[data-wfield="password"]');
    if (input) input.focus();
  }
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
  if (on && !remoteTimer) remoteTimer = setInterval(() => { if (!document.hidden && view === "setup" && tab === "remote") loadRemote(); }, 2000);
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

function renderRemote(el) {
  const r = remote;
  if (!r) return setHTML(el, `<div class="card"><p class="${remoteError ? "error-text" : "searching"}">${esc(remoteError || "Frage den Pi …")}</p></div>`);
  if (!r.supported) return setHTML(el, `<div class="card"><p>Fernwartung gibt es nur, wenn FlowThermPi mit dem
    Installationsbefehl auf dem Raspberry Pi läuft.</p></div>`);
  setHTML(el, remoteStatusHTML(r) + remoteLoginHTML(r) + remoteModeHTML(r) + remoteAboutHTML(r));
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
  if (every && !zigbeeTimer) zigbeeTimer = setInterval(() => { if (!document.hidden && view === "setup" && tab === "zigbee") loadZigbee(); }, every);
  zigbeeEvery = every;
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
  const it = d.thermostat ? "das Thermostat" : "das Gerät";
  const yes = await ask({ title: d.thermostat ? "Thermostat entfernen?" : "Gerät entfernen?",
    text: `Der Pi meldet ${it} im Zigbee-Netz ab. Willst du es später wieder nutzen, lernst du es einfach neu an.`,
    buttons: [{ label: "Abbrechen", value: null }, { label: "Entfernen", value: "yes", cls: "danger-solid" }] });
  if (!yes) return;
  const error = await zigbeeRemoveDevice(ieee, false);
  if (!error) return;
  if (![502, 504].includes(error.status)) { toast(error.message, true); return; }   // forcing only helps if the device did not answer
  const force = await ask({ title: "Trotzdem entfernen?",
    text: `${error.message} Du kannst ${it} trotzdem entfernen: Dann vergisst der Pi es, ohne auf seine Antwort zu warten. ` +
      "Meldet es sich später noch einmal, erscheint es wieder in der Liste.",
    buttons: [{ label: "Abbrechen", value: null }, { label: "Trotzdem entfernen", value: "yes", cls: "danger-solid" }] });
  if (!force) return;
  const again = await zigbeeRemoveDevice(ieee, true);
  if (again) toast(again.message, true);
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
  if (d.interview === "failed") return ["offline", "Anlernen hat nicht geklappt – bitte noch einmal suchen und am Thermostat die mittlere Taste 2 Sekunden drücken"];
  if (!d.supported) return ["offline", "wird von Zigbee2MQTT nicht unterstützt"];
  return ["ok", "bereit"];
}

function zigbeeDeviceHTML(d) {
  const [dot, words] = zigbeeDeviceState(d);
  const model = [d.vendor, d.model].filter(Boolean).join(" ") || (d.thermostat ? "Thermostat" : "Unbekanntes Gerät");
  const keep = s => s.replace(/ /g, " ");   // "20,5 °C" and the like stay on one line
  const facts = [words, d.battery != null ? keep(`Batterie ${Math.round(d.battery)} %`) : "", keep(zigbeeSignal(d.signal)),
    d.temperature != null ? keep(deg(d.temperature)) : ""].filter(Boolean).join(" · ");
  return `<li data-ieee="${esc(d.ieee)}"><span class="dot ${dot}"></span><span class="hname"><b>${esc(model)}</b>
      ${d.new ? `<span class="tag ok">neu</span>` : ""}
      <span class="muted zigbee-facts">${esc(facts)}</span>
      <span class="muted zigbee-ieee">${esc(d.ieee)}</span></span>
    <button class="small" data-action="zigbee-forget" ${zigbeeNetPending || zigbeeBusy() ? "disabled" : ""}>${
      zigbeeNetPending === d.ieee ? "entferne …" : "Entfernen …"}</button></li>`;
}

function zigbeeSearchHTML(net) {
  const s = net.search || {};
  if (s.active) {
    const left = `${Math.floor(s.left / 60)}:${String(s.left % 60).padStart(2, "0")}`;
    return `<div class="wifi-note zigbee-search">
      <p class="searching"><b>Suche läuft – noch ${left}</b></p>
      <ol class="zigbee-steps">
        <li>Batterien ins Thermostat legen, falls sie noch nicht drin sind.</li>
        <li>Das Drehrad unter 5 °C drehen, bis das Thermostat aus ist.</li>
        <li>Die mittlere Taste 2 Sekunden drücken, bis das Verbindungszeichen blinkt.</li>
        <li>Warten: Nach etwa einer Minute erscheint es unten in der Liste.</li>
      </ol>
      <p class="hint">Mehrere Thermostate gehen nacheinander in derselben Suche. Klappt es nicht, das Thermostat zum Anlernen
        näher an den Pi bringen.</p>
      <button class="small" data-action="zigbee-unsearch" ${zigbeeNetPending ? "disabled" : ""}>${
        zigbeeNetPending === "unsearch" ? "beende …" : "Suche beenden"}</button>
    </div>`;
  }
  return `<p>„Thermostate suchen“ öffnet das Zigbee-Netz für ${Math.round((s.seconds || 180) / 60)} Minuten, und die App sagt dir,
      was am Thermostat zu tun ist.</p>
    <div class="row"><button class="primary" data-action="zigbee-search" ${zigbeeNetPending || zigbeeBusy() ? "disabled" : ""}>${
      zigbeeNetPending === "search" ? "öffne das Netz …" : "Thermostate suchen"}</button></div>`;
}

function zigbeeDevicesHTML(z) {
  const p = z.programs, net = z.net;
  if (!p || !net || !p.complete || !p.service.present || zigbeeRunning()) return "";
  const online = net.bridge === "online", devices = net.devices || [];
  return `<div class="card zigbee-devices"><div class="card-head"><h2>Thermostate</h2></div>
    ${online ? zigbeeSearchHTML(net) : `<p class="muted">Thermostate suchen geht, sobald Zigbee2MQTT läuft.</p>`}
    ${devices.length ? `<ul class="net-list zigbee-list">${devices.map(zigbeeDeviceHTML).join("")}</ul>`
      : online ? `<p class="muted">Noch kein Thermostat angelernt.</p>` : ""}
    ${devices.length ? `<p class="hint">Räume und Heizpläne für die Thermostate kommen mit einem der nächsten Updates.</p>` : ""}
  </div>`;
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

function renderZigbee(el) {
  const z = zigbee;
  if (!z) return setHTML(el, `<div class="card"><p class="${zigbeeError ? "error-text" : "searching"}">${esc(zigbeeError || "Frage den Pi …")}</p></div>`);
  if (!z.supported) return setHTML(el, `<div class="card"><p>Zigbee gibt es nur, wenn FlowThermPi mit dem
    Installationsbefehl auf dem Raspberry Pi läuft.</p></div>`);
  setHTML(el, zigbeeStatusHTML(z) + zigbeeNetHTML(z) + zigbeeDevicesHTML(z) + zigbeeAboutHTML());
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
    case "help-start": return helpStart();
    case "help-end": return helpEnd();
    case "logs-save": return logsBusy ? undefined : logsSave();
  }
  if (t.dataset.subtab) {
    deviceTab = t.dataset.subtab;
    localStorage.setItem("heizung.deviceTab", deviceTab);
    render();
    return;
  }
  if (t.dataset.tab) {
    tab = t.dataset.tab;
    localStorage.setItem("heizung.tab", tab);
    if (tab === "plans" && editing) renderEditor();
    if (tab === "wifi") openWifiTab();
    if (tab === "remote") openRemoteTab();
    if (tab === "zigbee") openZigbeeTab();
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
    case "sensor-search": return sensorSearch && sensorSearch.running ? undefined : runSensorSearch();
    case "sensor-close": sensorSearch = null; if (pillForm && pillForm.at === "search") pillForm = null; return render();
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
    case "zigbee-search": return zigbeeSearch(true);
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
    return renderEditor();
  }
  if (sensorForm && t.dataset.sfield) { sensorForm[t.dataset.sfield] = t.value; return; }
  if (pillForm && t.dataset.pfield) { pillForm[t.dataset.pfield] = t.value; return; }
  if (wifiForm && t.dataset.wfield === "show") {   // switch the field directly: a redraw would wait for the focus to leave
    wifiForm.show = t.checked;
    const pw = document.querySelector('.wifi-form input[data-wfield="password"]');
    if (pw) pw.type = t.checked ? "text" : "password";
    return;
  }
  if (deviceForm && t.closest(".device-form")) return updateDeviceForm(t);
  if (roomForm && t.closest(".room-form")) return updateRoomForm(t);
  switch (t.dataset.action) {
    case "room-plan": t.blur(); return assignPlan(t.closest("article.card"), t.value);
    case "heater-room":
      return patch(`api/heaters/${encodeURIComponent(t.closest(".device").dataset.heater)}`, { room: t.value || null }, "Raum zugeordnet.");
    case "sensor-room":
      return patch(`api/sensors/${encodeURIComponent(t.closest(".device").dataset.sensor)}`, { room: t.value || null }, "Raum zugeordnet.");
    case "crun-hours": calibHours[t.closest(".device").dataset.sensor] = Number(t.value); t.blur(); return;
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
  if (e.target.dataset.lfield === "email") { logsEmail = e.target.value; return; }
  if (wifiForm && ["ssid", "password"].includes(e.target.dataset.wfield)) { wifiForm[e.target.dataset.wfield] = e.target.value; return; }
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
  if (e.key === "Enter" && calibForm && e.target.closest(".calib-form")) return saveCalib(null);
  if (e.key === "Enter" && wifiForm && e.target.closest(".wifi-form")) return connectWifi();
  if (e.key === "Enter" && e.target.dataset.field === "scan-network") { e.target.blur(); return runScan(scanNet.trim() || null); }
  if (e.key === "Enter" && e.target.dataset.lfield === "email") { e.target.blur(); return logsEmailSave(); }
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
