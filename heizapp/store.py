"""Configuration file (one heater per line: IP and token) and the app's own data (data.json)."""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from .coap import DEFAULT_PORT


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class HeaterConfig:
    host: str
    port: int
    token: str | None
    line: int


def load_config(path: str) -> list[HeaterConfig]:
    """heizungen.conf: "<IP oder Hostname>[:Port] [Token]" per line, '#' starts a comment."""
    if not os.path.exists(path):
        raise ConfigError(f"Konfiguration {path} fehlt – Vorlage: heizungen.conf.example")
    entries: list[HeaterConfig] = []
    with open(path, encoding="utf-8") as fh:
        for number, raw in enumerate(fh, start=1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) > 2:
                raise ConfigError(f"{path}, Zeile {number}: erwartet „IP Token“, gefunden „{line}“")
            host, port = parts[0], DEFAULT_PORT
            if host.count(":") == 1:
                host, port_text = host.split(":")
                port = int(port_text)
            token = parts[1] if len(parts) == 2 else None
            entries.append(HeaterConfig(host, port, token, number))
    seen = set()
    for e in entries:
        if (e.host, e.port) in seen:
            raise ConfigError(f"{path}, Zeile {e.line}: {e.host} ist doppelt eingetragen")
        seen.add((e.host, e.port))
    return entries


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class DataStore:
    """Rooms, plans and heater names, persisted as JSON. Thread-safe."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"version": 1, "heaters": {}, "rooms": {}, "plans": {}, "sensors": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    @contextmanager
    def edit(self):
        with self._lock:
            yield self.data
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.data))
