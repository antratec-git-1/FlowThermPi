"""Shelly devices in the home network: „The Pill“ as room sensor, plugs with power metering for the heaters.

The Pill (model S3SN-0U53X, Shelly generation 3) runs on a USB power supply and never sleeps, so the app can read
it at any time. Its 3.5 mm jack takes the probes: up to five DS18B20 (mode "onewire", several through the 1→5
splitter) or one DHT22 for temperature and humidity (mode "dht22", e.g. through the 5-terminal add-on). The Pill
finds plugged-in probes itself and reports each as a dynamic component "temperature:<id>" (a DHT22 also
"humidity:<id>"); the first DS18B20 was "temperature:200" on the real device. Shelly.GetStatus leaves dynamic
components out (firmware 1.8.99), so the app also reads Shelly.GetComponents.

A Shelly plug (generation 2 or newer: Plug M Gen3 S3PL-30110EU, up to 3000 W; Plug S Gen3, Plus Plug S …) is on
mains power and never sleeps either. Its one switch "switch:0" measures what flows through it: "apower" (W) and the
counter "aenergy.total" (Wh). The app reads "Shelly.GetStatus", whose "sys.mac" tells it is still the same plug.
Shelly's documentation was the source (11/2026); no real plug has been tried yet.

Search and reading only send GET requests: "GET /shelly" (Shelly's identification, never password protected) to
every address of the home network, then "Shelly.GetStatus", "Pill.GetConfig" and "Shelly.GetComponents" to each
Pill, "Shelly.GetStatus" and "Switch.GetConfig" to each plug. Only set_mode() and restart() write to a Pill
("Pill.SetConfig", "Shelly.Reboot"), only switch_on() and set_on_after_outage() to a plug ("Switch.Set" on – never
off –, "Switch.SetConfig" initial_state „on“) – and only when someone asks for it in the app.
Standard library only.
"""
from __future__ import annotations

import http.client
import json
from concurrent.futures import ThreadPoolExecutor

PORT = 80
TIMEOUT = 1.0     # seconds per address in a sweep; a missing device simply does not answer in time
WORKERS = 96      # parallel requests – 254 addresses take about three seconds
APP, MODEL, MODEL_NAME = "Pill", "S3SN-0U53X", "Shelly Pill"
MODES = {"onewire": "DS18B20", "dht22": "DHT22"}   # what a room sensor plugs in (the Pill knows more, e.g. analog_in)
DYNAMIC = "/rpc/Shelly.GetComponents?dynamic_only=true&include=%5B%22status%22%5D"   # include=["status"]
# plugs: model codes as Home Assistant's aioshelly lists them (11/2026); other "…PL-" models count too
PLUG_MODELS = {"S3PL-30110EU": "Shelly Plug M Gen3", "S3PL-00112EU": "Shelly Plug S Gen3",
               "S3PL-30116EU": "Shelly Plug PM Gen3", "S3PL-20112EU": "Shelly Outdoor Plug S Gen3",
               "S3PL-10112EU": "Shelly AZ Plug", "SNPL-00112EU": "Shelly Plus Plug S", "SNPL-10112EU": "Shelly Plus Plug S"}
PLUG_PREFIXES = ("SNPL-", "S3PL-", "S4PL-")   # Plus, Gen3, Gen4 plugs; Gen1 ("SHPLG-") speaks another language
# what the words of an error say, per device
WORDS = {"pill": ("Die Pill", "Ist sie am Netzteil und im WLAN?", "einrichten"),
         "plug": ("Die Steckdose", "Ist sie eingesteckt und im WLAN?", "schalten")}


class ShellyError(Exception):
    """A Pill or plug did not answer or refused; status is what the app's API answers with."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _get_json(host: str, port: int, path: str, timeout: float):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        res = conn.getresponse()
        return res.status, (json.loads(res.read(65536) or b"null") if res.status == 200 else None)
    finally:
        conn.close()


def _rpc(host: str, port: int, method: str, params: dict | None, timeout: float, device: str = "pill"):
    """One call that changes something: POST /rpc with a JSON-RPC frame, as in Shelly's documentation."""
    who, check, verb = WORDS[device]
    frame = {"id": 1, "method": method, **({"params": params} if params else {})}
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", "/rpc", body=json.dumps(frame).encode(),
                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        res = conn.getresponse()
        status, raw = res.status, res.read(65536)
    except (OSError, http.client.HTTPException) as exc:
        raise ShellyError(f"{who} antwortet nicht. {check}") from exc
    finally:
        conn.close()
    if status == 401:
        raise ShellyError(f"{who} ist mit einem Passwort geschützt – die App kann sie nicht {verb}.", 409)
    try:
        answer = json.loads(raw or b"null")
    except ValueError:
        answer = None
    error = answer.get("error") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(answer, dict) or error is not None:
        text = str(error.get("message") or "")[:200] if isinstance(error, dict) else ""
        raise ShellyError(f"{who} hat das abgelehnt{': ' + text if text else ''}.")
    return answer.get("result")


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_pill(info: dict) -> bool:
    return info.get("app") == APP or info.get("model") == MODEL


def is_plug(info: dict) -> bool:
    """A Shelly plug of generation 2 or newer – by its model code (or, for models the app does not know yet, its app
    name). Whether it is one switch that measures, Shelly.GetStatus tells (plug_reading)."""
    gen = info.get("gen")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 2:
        return False
    return str(info.get("model") or "").startswith(PLUG_PREFIXES) or "plug" in str(info.get("app") or "").lower()


def kind_of(info: dict) -> str | None:
    return "pill" if is_pill(info) else "plug" if is_plug(info) else None


def probe(host: str, port: int = PORT, timeout: float = TIMEOUT, kinds: tuple = ("pill",)) -> dict | None:
    """Shelly's identification of the device at host if it is of one of these kinds ("pill", "plug"), else None."""
    try:
        status, info = _get_json(host, port, "/shelly", timeout)
    except (OSError, http.client.HTTPException, ValueError):
        return None
    if status != 200 or not isinstance(info, dict) or not info.get("id"):
        return None
    return info if kind_of(info) in kinds else None


def _dynamic_components(host: str, port: int, timeout: float) -> tuple[int | None, list]:
    """The Pill's dynamic components with their status – where it lists its probes. Read page by page."""
    found, offset = [], 0
    for _ in range(10):   # a handful of probes fit on one page; never loop forever
        status, page = _get_json(host, port, f"{DYNAMIC}&offset={offset}", timeout)
        if status != 200 or not isinstance(page, dict):
            return (status if not found else 200), found
        batch = [c for c in page.get("components") or [] if isinstance(c, dict)]
        found += batch
        offset += len(batch)
        total = page.get("total")
        if not batch or not isinstance(total, int) or offset >= total:
            break
    return 200, found


def readings(host: str, port: int = PORT, timeout: float = 2.0) -> dict:
    """What a Pill measures right now: mode, one entry per probe ({"channel", "temp", "humidity", "error"}), Wi-Fi
    signal and a waiting firmware update. {"protected": True} with a password, {"offline": True} without an answer."""
    try:
        status, st = _get_json(host, port, "/rpc/Shelly.GetStatus", timeout)
        cfg_status, cfg = _get_json(host, port, "/rpc/Pill.GetConfig", timeout) if status == 200 else (None, None)
        dyn_status, dynamic = _dynamic_components(host, port, timeout) if status == 200 else (None, [])
    except (OSError, http.client.HTTPException, ValueError):
        return {"offline": True}
    if 401 in (status, cfg_status, dyn_status):
        return {"protected": True}
    if status != 200 or not isinstance(st, dict):
        return {"offline": True}
    parts = dict(st)
    for comp in dynamic:   # the probes (not in Shelly.GetStatus on firmware 1.8.99)
        if isinstance(comp.get("key"), str) and isinstance(comp.get("status"), dict):
            parts.setdefault(comp["key"], comp["status"])
    temps, hums = {}, {}
    for key, value in parts.items():
        kind, _, cid = key.partition(":")
        if cid.isdigit() and isinstance(value, dict) and kind in ("temperature", "humidity"):
            (temps if kind == "temperature" else hums)[int(cid)] = value
    lone = next(iter(hums.values())) if len(temps) == 1 and len(hums) == 1 else {}   # one DHT22, whatever its ids
    probes = []
    for cid in sorted(temps):
        errors = temps[cid].get("errors")
        probes.append({"channel": cid, "temp": _number(temps[cid].get("tC")),
                       "humidity": _number((hums.get(cid) or lone).get("rh")),
                       "error": ", ".join(map(str, errors))[:100] if isinstance(errors, list) and errors else None})
    wifi = st.get("wifi") if isinstance(st.get("wifi"), dict) else {}
    system = st.get("sys") if isinstance(st.get("sys"), dict) else {}
    updates = system.get("available_updates") if isinstance(system.get("available_updates"), dict) else {}
    stable = updates.get("stable") if isinstance(updates.get("stable"), dict) else {}
    mode = cfg.get("mode") if isinstance(cfg, dict) and isinstance(cfg.get("mode"), str) else None
    return {"mode": mode, "probes": probes, "rssi": _number(wifi.get("rssi")),
            "update": str(stable["version"])[:40] if stable.get("version") else None}


def describe(host: str, port: int, info: dict) -> dict:
    """One Pill for the app: where it is, who it is, what is plugged in and what it measures."""
    values = readings(host, port)
    return {"address": host if port == PORT else f"{host}:{port}", "id": str(info["id"]),
            "name": info.get("name") or None, "model": MODEL_NAME, "mac": info.get("mac"), "firmware": info.get("ver"),
            "mode": values.get("mode"), "probes": values.get("probes") or [], "rssi": values.get("rssi"),
            "update": values.get("update"), "protected": bool(values.get("protected") or info.get("auth_en")),
            "offline": bool(values.get("offline"))}


def plug_model(info: dict) -> str:
    model = str(info.get("model") or "")
    return PLUG_MODELS.get(model) or f"Shelly {info.get('app') or model or 'Steckdose'}"


def plug_reading(host: str, port: int = PORT, timeout: float = 2.0) -> dict:
    """What a Shelly plug reports now, from one "Shelly.GetStatus": its MAC, whether it is on, the power (W) and the
    energy counter (Wh) of its switch, errors such as "overtemp", the Wi-Fi signal. {"protected": True} with a
    password, {"offline": True} without an answer, {"unsuitable": True} if it is not one switch that measures (a power
    strip, say)."""
    try:
        status, st = _get_json(host, port, "/rpc/Shelly.GetStatus", timeout)
    except (OSError, http.client.HTTPException, ValueError):
        return {"offline": True}
    if status == 401:
        return {"protected": True}
    if status != 200 or not isinstance(st, dict):
        return {"offline": True}
    sw = st.get("switch:0")
    if sorted(k for k in st if k.startswith("switch:")) != ["switch:0"] or not isinstance(sw, dict) \
            or _number(sw.get("apower")) is None:
        return {"unsuitable": True}
    energy = sw.get("aenergy") if isinstance(sw.get("aenergy"), dict) else {}
    wifi = st.get("wifi") if isinstance(st.get("wifi"), dict) else {}
    system = st.get("sys") if isinstance(st.get("sys"), dict) else {}
    errors = sw.get("errors")
    return {"mac": str(system["mac"]).upper()[:20] if system.get("mac") else None,
            "on": sw["output"] if isinstance(sw.get("output"), bool) else None,
            "power": _number(sw.get("apower")), "energy_wh": _number(energy.get("total")),
            "errors": [str(x)[:40] for x in errors][:5] if isinstance(errors, list) else [],
            "rssi": _number(wifi.get("rssi"))}


def describe_plug(host: str, port: int, info: dict) -> dict:
    """One plug for the app: where it is, who it is, what flows through it now."""
    values = plug_reading(host, port)
    return {"address": host if port == PORT else f"{host}:{port}", "id": str(info["id"]),
            "name": info.get("name") or None, "model": plug_model(info), "mac": str(info.get("mac") or "").upper() or None,
            "firmware": info.get("ver"), "on": values.get("on"), "power": values.get("power"),
            "energy_wh": values.get("energy_wh"), "errors": values.get("errors") or [], "rssi": values.get("rssi"),
            "protected": bool(values.get("protected") or info.get("auth_en")), "offline": bool(values.get("offline")),
            "unsuitable": bool(values.get("unsuitable"))}


def scan(targets: list[tuple[str, int]], workers: int = WORKERS, timeout: float = TIMEOUT,
         kinds: tuple = ("pill",)) -> list[dict]:
    """One sweep over all (ip, port): the Pills (or plugs, or both – kinds) that answer. A plug that is not one switch
    with power metering is left out."""
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(targets)), thread_name_prefix="fuehler") as pool:
        infos = list(pool.map(lambda t: probe(t[0], t[1], timeout, kinds), targets))
        hits = [(t, i) for t, i in zip(targets, infos) if i]
        found = list(pool.map(lambda hit: (describe if is_pill(hit[1]) else describe_plug)(hit[0][0], hit[0][1], hit[1]),
                              hits))
    return [d for d in found if not d.get("unsuitable")]


def set_mode(host: str, port: int, mode: str, timeout: float = 5.0) -> bool:
    """Tell the Pill what is plugged in ("onewire" or "dht22"). True if it must restart for it."""
    if mode not in MODES:
        raise ShellyError("Diese Fühlerart kennt die App nicht.", 400)
    result = _rpc(host, port, "Pill.SetConfig", {"config": {"mode": mode}}, timeout)
    return bool(isinstance(result, dict) and result.get("restart_required"))


def restart(host: str, port: int, timeout: float = 5.0) -> None:
    """Restart the Pill – it then looks for plugged-in probes again."""
    _rpc(host, port, "Shelly.Reboot", None, timeout)


def switch_on(host: str, port: int, timeout: float = 5.0) -> None:
    """The plug lets the power through again. (The app never switches a plug off.)"""
    _rpc(host, port, "Switch.Set", {"id": 0, "on": True}, timeout, device="plug")


def after_outage(host: str, port: int, timeout: float = 3.0) -> str | None:
    """What the plug does when the power comes back after a cut: "on", "off", "restore_last" or "match_input"."""
    try:
        status, cfg = _get_json(host, port, "/rpc/Switch.GetConfig?id=0", timeout)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise ShellyError(f"{WORDS['plug'][0]} antwortet nicht. {WORDS['plug'][1]}") from exc
    if status == 401:
        raise ShellyError("Die Steckdose ist mit einem Passwort geschützt – die App kann sie nicht schalten.", 409)
    value = cfg.get("initial_state") if status == 200 and isinstance(cfg, dict) else None
    return value if isinstance(value, str) else None


def set_on_after_outage(host: str, port: int, timeout: float = 5.0) -> None:
    """After a power cut the plug comes on again – the heater behind it must not stay cold."""
    _rpc(host, port, "Switch.SetConfig", {"id": 0, "config": {"initial_state": "on"}}, timeout, device="plug")
