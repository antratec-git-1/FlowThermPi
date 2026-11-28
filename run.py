#!/usr/bin/env python3
"""Startet HoLiTherm, die lokale Web-App für Thermotec-Heizungen.

    python3 run.py                      # nur auf diesem Rechner: http://127.0.0.1:8080
    python3 run.py --host 0.0.0.0       # im ganzen Heimnetz erreichbar (dann Passwort setzen!)

Passwort (optional, empfohlen sobald --host 0.0.0.0): Umgebungsvariable HEIZAPP_PASSWORD,
Benutzername im Browser: heizung.

Protokoll: neben data.json im Ordner logs/ (holitherm.log), anderer Ordner mit --log-dir.
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import signal
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from heizapp import APP_NAME, __version__, logs  # noqa: E402
from heizapp.remote import Remote  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import Service  # noqa: E402
from heizapp.store import ConfigError, DataStore  # noqa: E402
from heizapp.support import Support  # noqa: E402
from heizapp.update import Updater  # noqa: E402
from heizapp.wifi import Wifi  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description=f"{APP_NAME} – lokale Steuerung für Thermotec-Heizungen")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--host", default=os.environ.get("HEIZAPP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HEIZAPP_PORT", "8080")))
    parser.add_argument("--config", default=os.environ.get("HEIZAPP_CONFIG", str(BASE / "heizungen.conf")))
    parser.add_argument("--data", default=os.environ.get("HEIZAPP_DATA", str(BASE / "data" / "data.json")))
    parser.add_argument("--interval", type=float, default=10.0, help="Abfrageintervall in Sekunden")
    parser.add_argument("--log-dir", default=os.environ.get("HEIZAPP_LOG_DIR") or None,
                        help="Ordner für das Protokoll (Standard: logs/ neben data.json)")
    parser.add_argument("--update-dir", default=os.environ.get("HEIZAPP_UPDATE_DIR") or None,
                        help=argparse.SUPPRESS)  # set by install.py: enables the update button
    parser.add_argument("--wifi-dir", default=os.environ.get("HEIZAPP_WIFI_DIR") or None,
                        help=argparse.SUPPRESS)  # set by install.py: enables Einrichtung › WLAN
    parser.add_argument("--remote-dir", default=os.environ.get("HEIZAPP_REMOTE_DIR") or None,
                        help=argparse.SUPPRESS)  # set by install.py: enables Einrichtung › Fernwartung
    parser.add_argument("--zigbee-dir", default=os.environ.get("HEIZAPP_ZIGBEE_DIR") or None,
                        help=argparse.SUPPRESS)  # set by install.py: enables Einrichtung › Zigbee
    parser.add_argument("--syslog-dir", default=os.environ.get("HEIZAPP_SYSLOG_DIR") or None,
                        help=argparse.SUPPRESS)  # set by install.py: „Protokoll speichern“ adds the Pi's system logs
    parser.add_argument("--reset-pin", action="store_true",
                        help="PIN der Einrichtung löschen (bei gestoppter App) und beenden")
    args = parser.parse_args()

    if args.reset_pin:
        store = DataStore(args.data)
        with store.edit() as data:
            (data.get("settings") or {}).pop("pin", None)
        print("PIN gelöscht – die Einrichtung ist wieder ohne PIN zugänglich.")
        return 0

    log_dir = args.log_dir or str(Path(args.data).resolve().parent / "logs")
    logs.setup(log_dir)
    unclean = logs.mark_running()   # before anything else is written: the last entry of the run before
    log = logging.getLogger("heizapp")
    log.info("%s %s startet – Python %s, %s %s (%s), Prozess %d", APP_NAME, __version__, platform.python_version(),
             platform.system(), platform.release(), platform.machine(), os.getpid())
    if unclean is not None:
        log.warning("Der letzte Lauf der App endete unerwartet%s – Stromausfall, Absturz oder Neustart des Pi ohne "
                    "Herunterfahren?", f" (letzter Eintrag {unclean})" if unclean else "")
    password = os.environ.get("HEIZAPP_PASSWORD") or None
    logs.keep_secret(password)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not password:
        log.warning("Ohne HEIZAPP_PASSWORD kann jeder im Netz die Heizungen über die App steuern.")

    try:
        service = Service(args.config, args.data, poll_interval=args.interval)
    except ConfigError as exc:
        log.error("Fehler in der Konfiguration: %s", exc)
        logs.mark_stopped()
        return 2

    updater, wifi = Updater(args.update_dir), Wifi(args.wifi_dir)
    remote, zigbee = Remote(args.remote_dir), Zigbee(args.zigbee_dir)
    service.attach_zigbee(zigbee.net)   # the thermostats paired in Zigbee2MQTT are heaters too
    support = Support(service, updater, wifi, remote, zigbee, args.syslog_dir, password=bool(password))
    describe(service, updater, wifi, remote, zigbee, support, password)
    service.start()
    remote.start()   # looks at Tailscale once, in the background
    zigbee.start()   # looks at the Zigbee programs once, in the background
    support.start()  # an Ausführliches Protokoll that was switched on goes on until its end
    server = make_server(service, args.host, args.port, password, updater, wifi, remote, zigbee, support)
    log.info("%s %s läuft auf http://%s:%d – Protokoll: %s", APP_NAME, __version__, args.host, args.port,
             logs.files()[-1] if logs.files() else "nur auf der Konsole")
    reason = "beendet"
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))   # systemd stops the app: end cleanly
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        reason = "beendet (Strg+C)"
    except SystemExit:
        reason = "beendet (vom System gestoppt)"
    finally:
        service.stop()
        support.stop()
        zigbee.stop()
        server.server_close()
        log.info("%s %s", APP_NAME, reason)
        logs.mark_stopped()
    return 0


def describe(service: Service, updater: Updater, wifi: Wifi, remote: Remote, zigbee: Zigbee, support: Support,
             password: str | None) -> None:
    """What the protocol should say right at the start: the setup, without any secret."""
    log = logging.getLogger("heizapp")
    data, entries = service.store.data, service.entries
    aeroflows = [e for e in entries if not e.zigbee]
    log.info("Eingerichtet: %d Heizung(en), davon %d mit Token · %d Thermostat(e) · %d Raum/Räume · %d Heizplan/-pläne · "
             "%d Fühler · PIN %s · Passwort für die App %s · Zeitzone %s",
             len(aeroflows), sum(1 for e in aeroflows if e.device.writable), len(entries) - len(aeroflows),
             len(data["rooms"]), len(data["plans"]),
             len(data["sensors"]), "gesetzt" if (data.get("settings") or {}).get("pin") else "keine",
             "gesetzt" if password else "keins", getattr(service.tz, "key", None) or "Systemzeit")
    for e in entries:
        meta = data["heaters"].get(e.id, {})
        log.info("%s %s: %s%s%s", "Thermostat" if e.zigbee else "Heizung", e.key if e.zigbee else e.label,
                 meta.get("name") or "(ohne Namen)",
                 f", Raum {data['rooms'][meta['room']]['name']}" if meta.get("room") in data["rooms"] else "",
                 "" if e.device.writable else ", ohne Token (nur Anzeige)")
    yes = {True: "ja", False: "nein"}
    log.info("Zusatzfunktionen: Update-Knopf %s · WLAN %s · Fernwartung %s · Zigbee %s · Systemprotokolle %s",
             yes[updater.supported], yes[wifi.supported], yes[remote.supported], yes[zigbee.supported],
             yes[support.syslog.supported])
    installed = updater.installed
    if installed["commit"] or installed["installed"]:
        log.info("Installiert: Commit %s, am %s", (installed["commit"] or "?")[:7], installed["installed"] or "?")
    status = updater.status()
    if status:
        log.info("Letztes Update per Knopf: %s – %s", status.get("state"), status.get("message") or "ohne Meldung")


if __name__ == "__main__":
    sys.exit(main())
