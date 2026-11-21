"""Shelly „The Pill“ as room sensor: find it in the home network, read it, set up what is plugged in.

The Pill (model S3SN-0U53X, Shelly generation 3) runs on a USB power supply and never sleeps, so the app can read
it at any time. Its 3.5 mm jack takes the probes: up to five DS18B20 (mode "onewire", several through the 1→5
splitter) or one DHT22 for temperature and humidity (mode "dht22", e.g. through the 5-terminal add-on). The Pill
finds plugged-in probes itself and reports each as a dynamic component "temperature:<id>" (a DHT22 also
"humidity:<id>"); the first DS18B20 was "temperature:200" on the real device. Shelly.GetStatus leaves dynamic
components out (firmware 1.8.99), so the app also reads Shelly.GetComponents.

Search and reading only send GET requests: "GET /shelly" (Shelly's identification, never password protected) to
every address of the home network, then "Shelly.GetStatus", "Pill.GetConfig" and "Shelly.GetComponents" to each
Pill. Only set_mode() and restart() write ("Pill.SetConfig", "Shelly.Reboot") – and only when someone asks for it in
the app's setup.
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


class ShellyError(Exception):
    """A Pill did not answer or refused; status is what the app's API answers with."""

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


def _rpc(host: str, port: int, method: str, params: dict | None, timeout: float):
    """One call that changes something: POST /rpc with a JSON-RPC frame, as in Shelly's documentation."""
    frame = {"id": 1, "method": method, **({"params": params} if params else {})}
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", "/rpc", body=json.dumps(frame).encode(),
                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        res = conn.getresponse()
        status, raw = res.status, res.read(65536)
    except (OSError, http.client.HTTPException) as exc:
        raise ShellyError("Die Pill antwortet nicht. Ist sie am Netzteil und im WLAN?") from exc
    finally:
        conn.close()
    if status == 401:
        raise ShellyError("Die Pill ist mit einem Passwort geschützt – die App kann sie nicht einrichten.", 409)
    try:
        answer = json.loads(raw or b"null")
    except ValueError:
        answer = None
    error = answer.get("error") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(answer, dict) or error is not None:
        text = str(error.get("message") or "")[:200] if isinstance(error, dict) else ""
        raise ShellyError(f"Die Pill hat das abgelehnt{': ' + text if text else ''}.")
    return answer.get("result")


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_pill(info: dict) -> bool:
    return info.get("app") == APP or info.get("model") == MODEL


def probe(host: str, port: int = PORT, timeout: float = TIMEOUT) -> dict | None:
    """Shelly's identification of the device at host if it is a Pill, else None."""
    try:
        status, info = _get_json(host, port, "/shelly", timeout)
    except (OSError, http.client.HTTPException, ValueError):
        return None
    if status != 200 or not isinstance(info, dict) or not info.get("id"):
        return None
    return info if is_pill(info) else None


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


def scan(targets: list[tuple[str, int]], workers: int = WORKERS, timeout: float = TIMEOUT) -> list[dict]:
    """One sweep over all (ip, port): the Pills that answer."""
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(targets)), thread_name_prefix="fuehler") as pool:
        infos = list(pool.map(lambda t: probe(t[0], t[1], timeout), targets))
        hits = [(t, i) for t, i in zip(targets, infos) if i]
        return list(pool.map(lambda hit: describe(hit[0][0], hit[0][1], hit[1]), hits))


def set_mode(host: str, port: int, mode: str, timeout: float = 5.0) -> bool:
    """Tell the Pill what is plugged in ("onewire" or "dht22"). True if it must restart for it."""
    if mode not in MODES:
        raise ShellyError("Diese Fühlerart kennt die App nicht.", 400)
    result = _rpc(host, port, "Pill.SetConfig", {"config": {"mode": mode}}, timeout)
    return bool(isinstance(result, dict) and result.get("restart_required"))


def restart(host: str, port: int, timeout: float = 5.0) -> None:
    """Restart the Pill – it then looks for plugged-in probes again."""
    _rpc(host, port, "Shelly.Reboot", None, timeout)
