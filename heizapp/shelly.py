"""Find Shelly room sensors (e.g. Shelly H&T Gen3) in the local network. Read only, standard library only.

The H&T sleeps between its measurements and only answers while it is awake: a short press on the button
behind its back cover wakes it up for a while (setup mode), on USB power it also wakes every 5 minutes.
So the app's search mode repeats a quick sweep: every address of the home network gets one short
"GET /shelly" (Shelly's identification, never password protected). Shelly devices of generation 2 or
newer that measure a temperature are read once ("Shelly.GetStatus") and listed. Nothing is written.
"""
from __future__ import annotations

import http.client
import json
from concurrent.futures import ThreadPoolExecutor

PORT = 80
TIMEOUT = 1.0     # seconds per address; a sleeping or missing device simply does not answer in time
WORKERS = 96      # parallel requests – 254 addresses take about three seconds
MODELS = {"HTG3": "Shelly H&T Gen3", "PlusHT": "Shelly Plus H&T"}


def _get_json(host: str, port: int, path: str, timeout: float):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        res = conn.getresponse()
        return res.status, (json.loads(res.read(65536) or b"null") if res.status == 200 else None)
    finally:
        conn.close()


def probe(host: str, port: int = PORT, timeout: float = TIMEOUT) -> dict | None:
    """Shelly's identification (generation 2 or newer) of the device at host, else None."""
    try:
        status, info = _get_json(host, port, "/shelly", timeout)
    except (OSError, http.client.HTTPException, ValueError):
        return None
    if status != 200 or not isinstance(info, dict) or not info.get("id"):
        return None
    gen = info.get("gen")
    return info if isinstance(gen, int) and gen >= 2 else None


def readings(host: str, port: int = PORT, timeout: float = 2.0) -> dict:
    """Temperature, humidity and power of a Shelly sensor; {"protected": True} if it has a password."""
    try:
        status, st = _get_json(host, port, "/rpc/Shelly.GetStatus", timeout)
    except (OSError, http.client.HTTPException, ValueError):
        return {"asleep": True}   # went back to sleep between identification and reading
    if status == 401:
        return {"protected": True}
    if status != 200 or not isinstance(st, dict):
        return {}

    def first(prefix: str, field: str):
        for key in sorted(st):
            value = st[key]
            if key.startswith(prefix) and isinstance(value, dict) and isinstance(value.get(field), (int, float)):
                return value[field]
        return None
    power = st.get("devicepower:0") if isinstance(st.get("devicepower:0"), dict) else {}
    battery = power.get("battery") if isinstance(power.get("battery"), dict) else {}
    external = power.get("external") if isinstance(power.get("external"), dict) else {}
    wifi = st.get("wifi") if isinstance(st.get("wifi"), dict) else {}
    return {"temp": first("temperature:", "tC"), "humidity": first("humidity:", "rh"),
            "battery": battery.get("percent"), "usb": external.get("present"), "rssi": wifi.get("rssi")}


def describe(host: str, port: int, info: dict) -> dict | None:
    """One found device for the app, or None if it is no temperature sensor (e.g. a Shelly plug)."""
    values = readings(host, port)
    app = str(info.get("app") or "")
    if values.get("temp") is None and app not in MODELS:
        return None
    return {"address": host if port == PORT else f"{host}:{port}", "id": str(info["id"]),
            "name": info.get("name") or None, "model": MODELS.get(app, f"Shelly {app}".strip()),
            "mac": info.get("mac"), "firmware": info.get("ver"),
            "temp": values.get("temp"), "humidity": values.get("humidity"), "battery": values.get("battery"),
            "usb": values.get("usb"), "rssi": values.get("rssi"),
            "protected": bool(values.get("protected") or info.get("auth_en")), "asleep": bool(values.get("asleep"))}


def scan(targets: list[tuple[str, int]], workers: int = WORKERS, timeout: float = TIMEOUT) -> list[dict]:
    """One sweep over all (ip, port): the Shelly temperature sensors that are awake right now."""
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(targets)), thread_name_prefix="fuehler") as pool:
        infos = list(pool.map(lambda t: probe(t[0], t[1], timeout), targets))
        hits = [(t, i) for t, i in zip(targets, infos) if i]
        found = list(pool.map(lambda hit: describe(hit[0][0], hit[0][1], hit[1]), hits))
    return [f for f in found if f]
