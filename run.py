#!/usr/bin/env python3
"""Startet FlowThermPi, die lokale Web-App für Thermotec-Heizungen.

    python3 run.py                      # nur auf diesem Rechner: http://127.0.0.1:8080
    python3 run.py --host 0.0.0.0       # im ganzen Heimnetz erreichbar (dann Passwort setzen!)

Passwort (optional, empfohlen sobald --host 0.0.0.0): Umgebungsvariable HEIZAPP_PASSWORD,
Benutzername im Browser: heizung.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from heizapp import APP_NAME, __version__  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import Service  # noqa: E402
from heizapp.store import ConfigError, DataStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description=f"{APP_NAME} – lokale Steuerung für Thermotec-Heizungen")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--host", default=os.environ.get("HEIZAPP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HEIZAPP_PORT", "8080")))
    parser.add_argument("--config", default=os.environ.get("HEIZAPP_CONFIG", str(BASE / "heizungen.conf")))
    parser.add_argument("--data", default=os.environ.get("HEIZAPP_DATA", str(BASE / "data" / "data.json")))
    parser.add_argument("--interval", type=float, default=10.0, help="Abfrageintervall in Sekunden")
    parser.add_argument("--reset-pin", action="store_true",
                        help="PIN der Einrichtung löschen (bei gestoppter App) und beenden")
    args = parser.parse_args()

    if args.reset_pin:
        store = DataStore(args.data)
        with store.edit() as data:
            (data.get("settings") or {}).pop("pin", None)
        print("PIN gelöscht – die Einrichtung ist wieder ohne PIN zugänglich.")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    password = os.environ.get("HEIZAPP_PASSWORD") or None
    if args.host not in ("127.0.0.1", "localhost", "::1") and not password:
        logging.warning("Ohne HEIZAPP_PASSWORD kann jeder im Netz die Heizungen über die App steuern.")

    try:
        service = Service(args.config, args.data, poll_interval=args.interval)
    except ConfigError as exc:
        print(f"Fehler in der Konfiguration: {exc}", file=sys.stderr)
        return 2

    service.start()
    server = make_server(service, args.host, args.port, password)
    logging.info("%s %s – %d Heizung(en) konfiguriert – läuft auf http://%s:%d",
                 APP_NAME, __version__, len(service.entries), args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
