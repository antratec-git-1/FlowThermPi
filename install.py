#!/usr/bin/env python3
"""HoLiTherm auf einem Raspberry Pi einrichten oder aktualisieren – ein Befehl per SSH:

    curl -fsSL https://raw.githubusercontent.com/antratec-git-1/HoLiTherm/main/install.py | sudo python3 -

Updates danach in der App: Einrichtung › Über › „Nach Updates suchen“ – oder derselbe Befehl noch
einmal. WLAN einrichten: Einrichtung › WLAN (der Pi kann also mit Netzwerkkabel ausgeliefert werden).
Fernwartung: Einrichtung › Fernwartung – Tailscale wird erst installiert, wenn man dort darum bittet.
Zigbee-Thermostate: Einrichtung › Zigbee – auch deren Zusatzprogramme erst, wenn man dort darum bittet.
Heizungen, Tokens, Räume, Pläne und PIN liegen getrennt vom Programm in /var/lib/holitherm und
bleiben bei Updates erhalten.

Dieses Skript ist der einzige Teil von HoLiTherm, der als root läuft – auch für den Update-Knopf
(--from-app), die WLAN-Einrichtung (--wifi), die Fernwartung (--remote), die Zigbee-Programme
(--zigbee) und die Systemprotokolle für „Protokoll speichern“ (--logs). Die App selbst hat keine
Rechte am System.

Optionen hinter „python3 -“ anhängen, z. B. „… | sudo python3 - --port 8080“:
    --port N           Port der App (Standard 80: http://<name>.local ohne Portangabe)
    --ref NAME         Version (Git-Tag) oder Branch statt „main“
    --import ORDNER    heizungen.conf und data.json aus diesem Ordner übernehmen (z. B. vom Mac kopiert)
    --source ORDNER    aus einem lokalen Ordner statt von GitHub installieren
    --reset-pin        PIN der Einrichtung löschen (falls vergessen)
    --uninstall        Dienst und Programm entfernen; mit --purge auch alle Daten

Braucht nur Python 3.11+ und systemd – beides bringt Raspberry Pi OS mit.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

REPO = "antratec-git-1/HoLiTherm"
NAME = "holitherm"
APP_DIR = f"/opt/{NAME}"
DATA_DIR = f"/var/lib/{NAME}"
# Until 0.4.28 the app was called FlowThermPi, and on the Pi everything was „flowthermpi“: folders, services, users,
# files, the Pi's name. move_old_installation() moves such an installation over to NAME.
OLD_NAME = "flowthermpi"
UNIT = f"/etc/systemd/system/{NAME}.service"
UPDATE_UNIT = f"/etc/systemd/system/{NAME}-update.service"   # runs this script when the app asks for an update
UPDATE_PATH = f"/etc/systemd/system/{NAME}-update.path"      # watches for the app's request
UPDATE_REQUEST = "update-request.json"   # in DATA_DIR: placed by the app (button „Jetzt aktualisieren“)
UPDATE_STATUS = "update-status.json"     # in DATA_DIR: progress and result, for the app
WIFI_UNIT = f"/etc/systemd/system/{NAME}-wifi.service"       # runs this script for the WLAN setup in the app
WIFI_PATH = f"/etc/systemd/system/{NAME}-wifi.path"          # watches for the app's WLAN request
WIFI_REQUEST = "wifi-request.json"       # in DATA_DIR: placed by the app (Einrichtung › WLAN), may hold a password
WIFI_STATUS = "wifi-status.json"         # in DATA_DIR: result and network state, for the app – never a password
WIFI_ACTIONS = ("scan", "connect", "forget")
WIFI_COUNTRIES = ("DE", "AT", "CH", "LU", "LI", "NL", "BE", "FR", "IT", "DK", "PL", "CZ", "ES", "GB", "US")
WIFI_PROFILE = "HoLiTherm WLAN"          # name of the NetworkManager connections made here
OLD_WIFI_PROFILE = "FlowThermPi WLAN"    # their name until 0.4.28 – renamed by move_old_installation()
WIFI_ONLY_LINK = ("Der Pi hängt gerade nur über „{ssid}“ am Netz. Vergisst er dieses WLAN, ist er nicht mehr "
                  "erreichbar – auch nicht aus der Ferne. Bitte zuerst das Netzwerkkabel am Pi einstecken.")
REMOTE_UNIT = f"/etc/systemd/system/{NAME}-remote.service"   # runs this script for the remote maintenance in the app
REMOTE_PATH = f"/etc/systemd/system/{NAME}-remote.path"      # watches for the app's requests about it
REMOTE_REQUEST = "remote-request.json"   # in DATA_DIR: placed by the app (Einrichtung › Fernwartung) – just an action
REMOTE_STATUS = "remote-status.json"     # in DATA_DIR: Tailscale's state for the app, the login link while one is open
REMOTE_ACTIONS = ("status", "up", "on", "down", "logout")   # „on“: Fernhilfe from the residents' view
REMOTE_TAG = f"tag:{NAME}"               # asked for at login: the tailnet's rules then say what reaches the Pi
TAILSCALE_PACKAGES = "https://pkgs.tailscale.com/stable"   # Tailscale's own package archive, signed by Tailscale
TAILSCALE_KEYRING = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
TAILSCALE_SOURCE = "/etc/apt/sources.list.d/tailscale.list"
TAILSCALE_LOGIN_WAIT = 8                 # seconds „tailscale up“ waits; the login link comes within a second or two
ZIGBEE_UNIT = f"/etc/systemd/system/{NAME}-zigbee.service"   # runs this script for Einrichtung › Zigbee
ZIGBEE_PATH = f"/etc/systemd/system/{NAME}-zigbee.path"      # watches for the app's requests about it
ZIGBEE_REQUEST = "zigbee-request.json"   # in DATA_DIR: placed by the app (Einrichtung › Zigbee) – an action, for
                                         # „setup“ also the radio channel and the stick
ZIGBEE_STATUS = "zigbee-status.json"     # in DATA_DIR: progress, result and what is installed, for the app
ZIGBEE_ACTIONS = ("status", "install", "remove", "setup", "start", "stop")
ZIGBEE_DIR = f"/opt/{NAME}-zigbee"       # Node.js, pnpm and Zigbee2MQTT – belongs to root
ZIGBEE_DATA = f"/var/lib/{NAME}-zigbee"  # Zigbee2MQTT's own data (network key, paired devices) – kept on removal
ZIGBEE_USER = f"{NAME}-zigbee"           # builds and runs Zigbee2MQTT, may use the USB stick (group dialout)
ZIGBEE_SERVICE = f"{NAME}-zigbee2mqtt"   # Zigbee2MQTT itself – set up with „Zigbee einrichten“, once the stick is there
ZIGBEE_SERVICE_UNIT = f"/etc/systemd/system/{ZIGBEE_SERVICE}.service"
ZIGBEE_CONFIG = "configuration.yaml"     # in ZIGBEE_DATA: Zigbee2MQTT's settings – it adds the network key itself
ZIGBEE_NETWORK = ("database.db", "database.db.backup", "coordinator_backup.json", "state.json")   # in ZIGBEE_DATA
ZIGBEE_CHANNELS = range(11, 27)          # Zigbee's radio channels on 2.4 GHz; the app offers 15, 20 and 25
ZIGBEE_ENV = f"/etc/{NAME}/zigbee2mqtt.env"   # Zigbee2MQTT's login at Mosquitto – only root reads it, systemd hands it over
ZIGBEE_LOGIN = "zigbee-mqtt.json"        # in DATA_DIR: the app's login at Mosquitto
ZIGBEE_INFO = "installed.json"           # in ZIGBEE_DIR: what is installed there
ZIGBEE_FREE_MB = 1500                    # free space on the SD card needed before an installation starts
ZIGBEE_STEPS = ("check", "mosquitto", "node", "pnpm", "zigbee2mqtt", "dependencies", "build", "finish")
MOSQUITTO_CONF = f"/etc/mosquitto/conf.d/{NAME}.conf"
MOSQUITTO_PASSWD = f"/etc/mosquitto/{NAME}.passwd"
MQTT_PORT = 1883
MQTT_USERS = {"app": NAME, "zigbee2mqtt": "zigbee2mqtt"}   # one login each, made anew at every installation
LOGS_UNIT = f"/etc/systemd/system/{NAME}-logs.service"   # collects the Pi's system logs for „Protokoll speichern“
LOGS_PATH = f"/etc/systemd/system/{NAME}-logs.path"      # watches for the app's request
LOGS_REQUEST = "logs-request.json"       # in DATA_DIR: placed by the app – just "collect"
LOGS_STATUS = "logs-status.json"         # in DATA_DIR: done or failed, for the app
LOGS_FILE = "system-log.txt"             # in DATA_DIR: what was collected, without secrets – the app reads and removes it
LOGS_DAYS = 7                            # the journal of the last week …
LOGS_MAX = 6 << 20                       # … in at most this many bytes
# whose journal goes in, with at most this many of its newest lines
LOGS_UNITS = ((NAME, 3000), (f"{NAME}-update", 600), (f"{NAME}-wifi", 400), (f"{NAME}-remote", 400),
              (f"{NAME}-zigbee", 800), (f"{NAME}-logs", 100), (ZIGBEE_SERVICE, 1500), ("mosquitto", 300),
              ("NetworkManager", 1000), ("wpa_supplicant", 300), ("tailscaled", 800), ("systemd-timesyncd", 100),
              (f"{NAME}-umzug", 200), (f"{OLD_NAME}-update", 400))   # the move from FlowThermPi: where it ran
LOGS_KERNEL = 1500
# An installation from before 0.4.29 (move_old_installation): what moves where, the old services, and the transient
# service that finishes the move once the update – which still runs in the old update service – is over
OLD_MOVES = ((f"/opt/{OLD_NAME}", APP_DIR), (f"/var/lib/{OLD_NAME}", DATA_DIR),
             (f"/opt/{OLD_NAME}-zigbee", ZIGBEE_DIR), (f"/var/lib/{OLD_NAME}-zigbee", ZIGBEE_DATA),
             (f"/etc/{OLD_NAME}", f"/etc/{NAME}"), (f"/etc/default/{OLD_NAME}", f"/etc/default/{NAME}"))
OLD_UNITS = tuple(f"{OLD_NAME}{end}" for end in (
    ".service", "-update.path", "-update.service", "-wifi.path", "-wifi.service", "-remote.path", "-remote.service",
    "-zigbee.path", "-zigbee.service", "-logs.path", "-logs.service", "-zigbee2mqtt.service"))
OLD_UPDATE = f"{OLD_NAME}-update.service"
MOVE_UNIT = f"{NAME}-umzug"
HIDDEN = "•••"
# What a secret looks like – the same list as in heizapp/logs.py (this script cannot import the app), plus the lines of
# NetworkManager, wpa_supplicant and nmcli that hold a WLAN password
SECRET_KEYS = r"token|pop|psk|password|passwort|passwd|secret|auth_?key|auth_?url|login_?url|pin|network_?key"
SECRET_PATTERNS = (
    # Zigbee's network key as a list of numbers – before key=value below, which would hide only its first number
    (re.compile(r"(?i)((?<![A-Za-z0-9])network_?key[\"']?\s*[:=]\s*)\[[^\]\n]{0,400}\]"), rf"\1[{HIDDEN}]"),
    (re.compile(rf"(?i)(?<![A-Za-z0-9])({SECRET_KEYS})=[^\s&\"',;]+"), rf"\1={HIDDEN}"),
    (re.compile(rf"""(?i)(["'])((?:[A-Za-z0-9]+_)*(?:{SECRET_KEYS}))\1(\s*:\s*)(["'])(?:\\.|(?!\4).)*\4"""),
     rf"\1\2\1\3\4{HIDDEN}\4"),
    (re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9+/=._~-]{8,}"), rf"\1 {HIDDEN}"),
    (re.compile(r"(https://[A-Za-z0-9.-]+(?::\d{1,5})?/a/)[A-Za-z0-9]+"), rf"\1{HIDDEN}"),   # Tailscale's login link
    (re.compile(r"\btskey-[A-Za-z0-9_-]+"), f"tskey-{HIDDEN}"),
    (re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]{16,}"), f"data:image/png;base64,{HIDDEN}"),
    (re.compile(r"(?im)\b((?:802-11-wireless-security\.|802-1x\.)?(?:psk|password|wep-key[0-3]|leap-password|"
                r"private-key-password)\s*[:=]\s*)\S.*$"), rf"\1{HIDDEN}"),
)
THROTTLED = {0: "Unterspannung jetzt", 1: "Takt begrenzt jetzt", 2: "gedrosselt jetzt", 3: "Temperaturgrenze jetzt",
             16: "Unterspannung seit dem Start", 17: "Takt begrenzt seit dem Start", 18: "gedrosselt seit dem Start",
             19: "Temperaturgrenze seit dem Start"}   # bits of „vcgencmd get_throttled“
# What „Installieren“ puts on the Pi: fixed for each version of HoLiTherm and checked after every download.
ZIGBEE_PARTS = {
    "node": {"version": "24.22.3",       # Node.js 24 (LTS); Zigbee2MQTT 2.14 asks for ^22.2.0 || ^24
             "sha256": {"arm64": "261af2af250dfd7e9f25cf151d1b2713a0adf8f10a9ad116c4947f68627affe8",
                        "amd64": "061ec3e3062115705b56b51fa9cc330ab50d34b6969387d4e4b1a50c6809acc2"}},
    "pnpm": {"version": "11.26.0",       # the version Zigbee2MQTT names in its package.json (packageManager)
             "sha512": "fc0e2bf890b9f983611f1ab68c0637bce914390653699f83c1a78b005ed25f2c"
                       "81e77920c8fd8eee2ecf0b58b28cdcb00a84d97f69bcf7c56b2f344710238664"},
    "zigbee2mqtt": {"version": "2.14.3", "commit": "3c841fdfd66c92239921ef84fde8f039c64e1662"},
}
NODE_ARCH = {"arm64": "arm64", "amd64": "x64"}   # Debian's name of the system -> the one in Node.js's file names
NODE_URL = "https://nodejs.org/dist/v{version}/node-v{version}-linux-{arch}.tar.gz"
PNPM_URL = "https://registry.npmjs.org/pnpm/-/pnpm-{version}.tgz"
ZIGBEE2MQTT_URL = "https://codeload.github.com/Koenkk/zigbee2mqtt/tar.gz/{commit}"
APT = ("apt-get", "-o", "DPkg::Lock::Timeout=300")   # waits if the system is installing updates right now
RELEASE_INFO = "release.json"            # in each release: version and Git commit, read by the app
COMMIT = re.compile(r"[0-9a-f]{40}")
MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
DEFAULT_PORT = 80
DEFAULT_TZ = "Europe/Berlin"
FACTORY_TZ = {"", "UTC", "Etc/UTC", "Europe/London"}  # Raspberry Pi OS without own setting
SKIP_TOP = {".git", ".claude", "data", "heizungen.conf", RELEASE_INFO}  # never part of the program: secrets, user data
SKIP_ANY = {"__pycache__", ".DS_Store"}
SKIP_PATTERNS = ("*.pdf", "*.log", "*.tmp", "*.pyc")

CONFIG_HEADER = """\
# Heizungen – eine pro Zeile: IP-Adresse und Token.
# Am einfachsten in der App anlegen: Einrichtung › Geräte › „Im Netz suchen“ oder „+ Heizung“.
# Die App schreibt diese Datei dann selbst.
# IP-Adresse        Token
"""

UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Eigene Einstellungen (z. B. HEIZAPP_PASSWORD=…) gehören nach /etc/default/{name}.
[Unit]
Description=HoLiTherm – lokale Heizungssteuerung
Wants=network-online.target
After=network-online.target time-sync.target

[Service]
User={user}
Group={user}
WorkingDirectory={app}/current
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
Environment=HEIZAPP_HOST=0.0.0.0
Environment=HEIZAPP_PORT={port}
Environment=HEIZAPP_CONFIG={data}/heizungen.conf
Environment=HEIZAPP_DATA={data}/data/data.json
Environment=HEIZAPP_UPDATE_DIR={data}
Environment=HEIZAPP_WIFI_DIR={data}
Environment=HEIZAPP_REMOTE_DIR={data}
Environment=HEIZAPP_ZIGBEE_DIR={data}
Environment=HEIZAPP_SYSLOG_DIR={data}
EnvironmentFile=-/etc/default/{name}
ExecStart={python} {app}/current/run.py
Restart=always
RestartSec=5
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={data}

[Install]
WantedBy=multi-user.target
"""

# The app runs without any rights on the program; for an update it only places {data}/update-request.json.
# This unit then runs the installer of the installed version as root. It always fetches the newest
# GitHub main of REPO – the app cannot choose what is installed.
UPDATE_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Holt die neueste Version von GitHub, wenn die App darum bittet (Knopf „Jetzt aktualisieren“).
[Unit]
Description=HoLiTherm – Update von GitHub
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --from-app
TimeoutStartSec=15min
"""

UPDATE_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-update.service, sobald die App eine Update-Anfrage ablegt.
[Unit]
Description=HoLiTherm – wartet auf den Update-Knopf der App

[Path]
PathExists={data}/{request}
Unit={name}-update.service

[Install]
WantedBy=multi-user.target
"""


WIFI_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Richtet das WLAN ein, wenn die App darum bittet (Einrichtung › WLAN).
[Unit]
Description=HoLiTherm – WLAN einrichten
After=NetworkManager.service

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --wifi
TimeoutStartSec=3min
"""

WIFI_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-wifi.service, sobald die App eine WLAN-Anfrage ablegt.
[Unit]
Description=HoLiTherm – wartet auf WLAN-Anfragen der App

[Path]
PathExists={data}/{wifi_request}
Unit={name}-wifi.service

[Install]
WantedBy=multi-user.target
"""

REMOTE_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Fernwartung mit Tailscale, wenn die App darum bittet (Einrichtung › Fernwartung).
[Unit]
Description=HoLiTherm – Fernwartung (Tailscale)
Wants=network-online.target
After=network-online.target
# while a login is open the app asks every few seconds – more often than systemd allows by default
StartLimitIntervalSec=60
StartLimitBurst=30

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --remote
TimeoutStartSec=20min
"""

REMOTE_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-remote.service, sobald die App eine Anfrage zur Fernwartung ablegt.
[Unit]
Description=HoLiTherm – wartet auf Anfragen der App zur Fernwartung

[Path]
PathExists={data}/{remote_request}
Unit={name}-remote.service

[Install]
WantedBy=multi-user.target
"""

TAILSCALE_SOURCE_TEMPLATE = """\
# Tailscale – eingerichtet von HoLiTherm (Einrichtung › Fernwartung)
deb [signed-by={keyring}] {packages}/{distro} {codename} main
"""

ZIGBEE_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Installiert oder entfernt die Zigbee-Programme, wenn die App darum bittet (Einrichtung › Zigbee).
[Unit]
Description=HoLiTherm – Zigbee-Programme
Wants=network-online.target
After=network-online.target
# „Neu prüfen“ several times in a row must not lock the service (and with it the path unit) – systemd's
# default allows 5 starts in 10 s
StartLimitIntervalSec=60
StartLimitBurst=30

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --zigbee
TimeoutStartSec=60min
# the heaters come first: Zigbee2MQTT is built with what the Pi has to spare
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
"""

ZIGBEE_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-zigbee.service, sobald die App eine Anfrage zu den Zigbee-Programmen ablegt.
[Unit]
Description=HoLiTherm – wartet auf Anfragen der App zu Zigbee

[Path]
PathExists={data}/{zigbee_request}
Unit={name}-zigbee.service

[Install]
WantedBy=multi-user.target
"""

LOGS_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Sammelt die Systemprotokolle für „Protokoll speichern“ in der App – ohne Passwörter, Tokens und Anmelde-Links.
[Unit]
Description=HoLiTherm – Systemprotokolle sammeln
# „Protokoll speichern“ several times in a row must not lock the service (and with it the path unit)
StartLimitIntervalSec=60
StartLimitBurst=30

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --logs
TimeoutStartSec=3min
Nice=10
"""

LOGS_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-logs.service, sobald die App um die Systemprotokolle bittet.
[Unit]
Description=HoLiTherm – wartet auf „Protokoll speichern“ in der App

[Path]
PathExists={data}/{logs_request}
Unit={name}-logs.service

[Install]
WantedBy=multi-user.target
"""

MOSQUITTO_TEMPLATE = """\
# Geschrieben von HoLiTherm (Einrichtung › Zigbee) – wird bei jeder Installation neu geschrieben.
# Nur auf diesem Pi erreichbar und nur mit Passwort (eins für die App, eins für Zigbee2MQTT).
listener {port} 127.0.0.1
allow_anonymous false
password_file {passwd}
"""

ZIGBEE_ENV_TEMPLATE = """\
# Geschrieben von HoLiTherm (Einrichtung › Zigbee) – wird bei jeder Installation neu geschrieben.
# Anmeldung von Zigbee2MQTT bei Mosquitto. Nur root kann diese Datei lesen.
ZIGBEE2MQTT_CONFIG_MQTT_SERVER=mqtt://127.0.0.1:{port}
ZIGBEE2MQTT_CONFIG_MQTT_USER={user}
ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD={password}
"""

# Zigbee2MQTT runs as its own user, with nothing but its data folder to write to and the USB stick (group dialout).
ZIGBEE2MQTT_UNIT_TEMPLATE = """\
# Geschrieben von install.py (Einrichtung › Zigbee) – wird bei jedem Update neu geschrieben.
# Zigbee2MQTT: spricht über den USB-Stick mit den Thermostaten und über Mosquitto mit der App.
[Unit]
Description=HoLiTherm – Zigbee2MQTT (Thermostate)
Wants=mosquitto.service
After=mosquitto.service network-online.target
# without the stick it fails at every start: systemd stops trying after a while, the app shows why
StartLimitIntervalSec=10min
StartLimitBurst=10

[Service]
User={user}
Group={user}
SupplementaryGroups=dialout
WorkingDirectory={base}/zigbee2mqtt
Environment=NODE_ENV=production NO_COLOR=1 FORCE_COLOR=0
Environment=ZIGBEE2MQTT_DATA={data}
# no setup pages of its own: HoLiTherm has written its settings
Environment=Z2M_ONBOARD_NO_SERVER=1
Environment=ZIGBEE2MQTT_CONFIG_SERIAL_PORT={port}
EnvironmentFile={env}
ExecStart={base}/node/bin/node index.js
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={data}
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
"""

# Written once, at „Zigbee einrichten“. Zigbee2MQTT rewrites the file at every start (without these comments) and puts
# a random network key and network numbers in place of GENERATE – its own defaults are the same for everybody.
ZIGBEE2MQTT_CONFIG_TEMPLATE = """\
# Geschrieben von HoLiTherm (Einrichtung › Zigbee). Zigbee2MQTT schreibt diese Datei beim Start selbst neu und
# ergänzt dabei einen zufälligen Netzwerkschlüssel – bitte nicht von Hand ändern.
version: 5
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://127.0.0.1:{mqtt_port}
serial:
  port: '{port}'
{stick}advanced:
  channel: {channel}
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
  log_output:
    - console
  log_level: info
  last_seen: ISO_8601
  enable_external_js: false
device_options:
  retain: true
frontend:
  enabled: false
homeassistant:
  enabled: false
"""


class InstallError(Exception):
    pass


class CommandError(InstallError):
    """A command failed. detail is its own message, without the command line (that may hold a password)."""

    def __init__(self, shown: str, detail: str):
        super().__init__(f"„{shown}“ ist fehlgeschlagen: {detail}")
        self.detail = detail


class ProgramMissing(CommandError):
    def __init__(self, program: str):
        super().__init__(program, "Programm nicht gefunden")


class StepFailed(InstallError):
    """A long step failed. details: the end of what it printed (holds no password), for the app."""

    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.details = details


def say(text: str = "") -> None:
    print(text, flush=True)


# ---------------------------------------------------------------------- system access
class System:
    """Everything that reaches beyond our own folders (services, users, owners)."""

    live = True

    def run(self, *cmd: str, check: bool = True, env: dict | None = None, redact: str = "") -> str:
        """Output of cmd. redact: a secret in the arguments that must not appear in an error message."""
        try:
            code, out, err = self.execute(cmd, env)
        except FileNotFoundError:
            if check:
                raise ProgramMissing(cmd[0]) from None
            return ""
        if check and code != 0:
            shown, detail = " ".join(cmd), (err or out).strip()
            if redact:
                shown, detail = shown.replace(redact, "•••"), detail.replace(redact, "•••")
            raise CommandError(shown, detail)
        return out.strip()

    def call(self, *cmd: str, timeout: float | None = None) -> tuple[int, str, str]:
        """Exit code, output and error output of cmd, whatever the code. ProgramMissing if there is no such program."""
        try:
            return self.execute(cmd, None, timeout=timeout)
        except FileNotFoundError:
            raise ProgramMissing(cmd[0]) from None

    def execute(self, cmd: tuple[str, ...], env: dict | None, timeout: float | None = None) -> tuple[int, str, str]:
        try:
            res = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as exc:   # the program was stopped; what it printed until then
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
            return 124, out, f"{cmd[0]}: keine Antwort nach {timeout:g} Sekunden"
        return res.returncode, res.stdout, res.stderr

    def ok(self, *cmd: str) -> bool:
        return subprocess.run(cmd, capture_output=True).returncode == 0

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def user_exists(self, user: str = NAME) -> bool:
        import pwd
        try:
            pwd.getpwnam(user)
            return True
        except KeyError:
            return False

    def fchown(self, fd: int, user: str | None = None) -> None:
        """fd to user and its group – without user to the app's own (app_user)."""
        import pwd
        entry = pwd.getpwnam(user or app_user(self))
        os.fchown(fd, entry.pw_uid, entry.pw_gid)

    def lchown(self, path: Path, user: str) -> None:
        """path itself (a link is not followed) to user and its group."""
        import pwd
        entry = pwd.getpwnam(user)
        os.chown(path, entry.pw_uid, entry.pw_gid, follow_symlinks=False)

    def chown_tree(self, top: Path, user: str) -> None:
        """Hand a folder over that root has just filled and nobody else can reach yet – innermost entries first,
        links themselves, never what they point to."""
        import pwd
        entry = pwd.getpwnam(user)
        for folder, dirs, files in os.walk(top, topdown=False):
            for name in dirs + files:
                os.chown(os.path.join(folder, name), entry.pw_uid, entry.pw_gid, follow_symlinks=False)
        os.chown(top, entry.pw_uid, entry.pw_gid, follow_symlinks=False)

    def run_as(self, user: str, cmd: tuple[str, ...], cwd: Path, env: dict, timeout: float) -> tuple[int, str]:
        """cmd as user – with its own group only, none of root's – and what it printed, errors included."""
        try:
            res = subprocess.run(cmd, cwd=cwd, env=env, user=user, group=user, extra_groups=[], umask=0o022,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
            return 124, f"{out}\n{cmd[0]}: keine Antwort nach {timeout:g} Sekunden"
        except FileNotFoundError:
            raise ProgramMissing(cmd[0]) from None
        return res.returncode, res.stdout


class DryRun(System):
    """For tests (--root): records commands instead of running them."""

    live = False

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, *cmd: str, check: bool = True, env: dict | None = None, redact: str = "") -> str:
        self.commands.append(cmd)
        return ""

    def execute(self, cmd: tuple[str, ...], env: dict | None, timeout: float | None = None) -> tuple[int, str, str]:
        self.commands.append(cmd)
        return 0, "", ""

    def ok(self, *cmd: str) -> bool:
        self.commands.append(cmd)
        return False

    def sleep(self, seconds: float) -> None:
        pass

    def user_exists(self, user: str = NAME) -> bool:
        return False

    def fchown(self, fd: int, user: str | None = None) -> None:
        pass

    def lchown(self, path: Path, user: str) -> None:
        pass

    def chown_tree(self, top: Path, user: str) -> None:
        pass

    def run_as(self, user: str, cmd: tuple[str, ...], cwd: Path, env: dict, timeout: float) -> tuple[int, str]:
        self.commands.append(cmd)
        return 0, ""


# ---------------------------------------------------------------------- program files
def download(ref: str, tmp: Path) -> tuple[Path, str | None]:
    """Program folder and Git commit of ref (GitHub writes the commit into the archive)."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/{ref}"
    say(f"• Lade HoLiTherm ({ref}) von GitHub …")
    error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                archive = tmp / "holitherm.tar.gz"
                archive.write_bytes(res.read())
            return extract(archive, tmp / "src")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise InstallError(f"„{ref}“ gibt es im Repository {REPO} nicht.") from exc
            error = exc
        except (urllib.error.URLError, OSError) as exc:
            error = exc
        time.sleep(5 * attempt)
    raise InstallError(f"Download von GitHub fehlgeschlagen ({error}). Hat der Pi Internet?")


def extract(archive: Path, dest: Path) -> tuple[Path, str | None]:
    """Unpack only plain files and folders, nothing outside dest (no links, no ../)."""
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        comment = str((tar.pax_headers or {}).get("comment", ""))  # git archive: the commit id
        commit = comment if COMMIT.fullmatch(comment) else None
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise InstallError(f"Unerwarteter Pfad im Archiv: {member.name}")
            target = dest.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    tops = list(dest.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise InstallError("Das Archiv von GitHub hat einen unerwarteten Aufbau.")
    return tops[0], commit


def program_version(src: Path) -> str:
    init = src / "heizapp" / "__init__.py"
    if not (src / "run.py").is_file() or not init.is_file():
        raise InstallError(f"In {src} liegt kein HoLiTherm (run.py und heizapp/ fehlen).")
    found = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    return found.group(1) if found else "unbekannt"


def copy_release(src: Path, releases: Path, version: str) -> Path:
    """New folder releases/<running number>-<version>; the number only grows (clock-independent)."""
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / f"{max(map(release_key, releases.iterdir()), default=0) + 1:04d}-{version}"

    def skip(directory: str, names: list[str]) -> set[str]:
        top = Path(directory) == src
        return {x for x in names if x in SKIP_ANY or (top and x in SKIP_TOP)
                or any(fnmatch.fnmatch(x, pattern) for pattern in SKIP_PATTERNS)}

    shutil.copytree(src, release, ignore=skip)
    for path in [release, *release.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return release


def write_release_info(release: Path, version: str, commit: str | None, ref: str | None) -> None:
    """What is installed, for the app („Nach Updates suchen“ compares the commit with GitHub)."""
    info = {"version": version, "commit": commit, "ref": ref, "installed": now_text()}
    path = release / RELEASE_INFO
    path.write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def release_info(folder: Path) -> dict:
    try:
        info = json.loads((folder / RELEASE_INFO).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def install_lock(app: Path):
    """One installer at a time (the update button and the command line could meet)."""
    app.mkdir(parents=True, exist_ok=True)
    fd = os.open(app / "install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("• Warte, bis das laufende Update fertig ist …")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def release_key(path: Path) -> int:
    found = re.match(r"(\d+)-", path.name)
    return int(found.group(1)) if found else 0


def switch_current(app: Path, target: Path) -> Path | None:
    """Point app/current at target in one step; returns the previous target."""
    current = app / "current"
    previous = current.resolve() if current.is_symlink() else None
    link = app / "current.new"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(os.path.relpath(target.resolve(), app.resolve()))
    os.replace(link, current)
    return previous


def prune(app: Path) -> None:
    current = (app / "current").resolve()
    old = sorted((d for d in (app / "releases").iterdir() if d.is_dir() and d.resolve() != current),
                 key=release_key, reverse=True)
    for directory in old[KEEP_RELEASES - 1:]:
        shutil.rmtree(directory)


# ---------------------------------------------------------------------- data folder
# The data folder belongs to the service user, so nothing in it is trusted: root never follows a link
# there (a planted link could otherwise make root change or hand over files elsewhere). Files are opened
# relative to an opened folder with O_NOFOLLOW and replaced by renaming, never written through a path.
def open_folder(name: str | Path, system: System, parent: int | None = None, user: str | None = None,
                mode: int = 0o750) -> int:
    """Folder of the service (created if missing), 0750 and owned by it; returns an open descriptor."""
    try:
        os.mkdir(name, mode, dir_fd=parent)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise InstallError(f"{name} im Datenordner ist kein normaler Ordner (Verweis?) – "
                           "aus Sicherheitsgründen abgebrochen.") from exc
    os.fchmod(fd, mode)
    system.fchown(fd, user)
    return fd


def open_file(folder: int, name: str) -> int | None:
    """An existing plain file in the folder (None if missing) – never through a link."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"{name} im Datenordner ist keine normale Datei (Verweis?) – "
                           "aus Sicherheitsgründen abgebrochen.") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise InstallError(f"{name} im Datenordner ist keine normale Datei – aus Sicherheitsgründen abgebrochen.")
    return fd


def read_private(folder: int, name: str) -> str | None:
    fd = open_file(folder, name)
    if fd is None:
        return None
    with os.fdopen(fd, encoding="utf-8") as fh:
        return fh.read()


def write_private(folder: int, name: str, text: str, system: System) -> None:
    """Replace folder/name: 0600 and owned by the service. Written to a new file that is renamed into
    place, so a planted link is replaced, never followed."""
    write_owned(folder, name, text, system, app_user(system))


def write_owned(folder: int, name: str, text: str, system: System, user: str) -> None:
    """write_private for a file of another user (Zigbee2MQTT's settings in its own folder)."""
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
            system.fchown(fh.fileno(), user)
        os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=folder)
        except FileNotFoundError:
            pass
        raise


def fix_private(folder: int, name: str, system: System) -> bool:
    """An existing file: 0600 and owned by the service. False if it does not exist."""
    fd = open_file(folder, name)
    if fd is None:
        return False
    try:
        os.fchmod(fd, 0o600)
        system.fchown(fd)
    finally:
        os.close(fd)
    return True


def prepare_data(data_dir: Path, system: System, import_dir: str | None) -> None:
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    top = open_folder(data_dir, system)
    try:
        sub = open_folder("data", system, top)
        try:
            if import_dir:
                import_files(Path(import_dir).expanduser(), (top, "heizungen.conf"), (sub, "data.json"), system)
            if not fix_private(top, "heizungen.conf", system):
                write_private(top, "heizungen.conf", CONFIG_HEADER, system)
                say("• Leere Heizungsliste angelegt – Heizungen fügst du in der App unter Einrichtung › Geräte hinzu")
            fix_private(sub, "data.json", system)
        finally:
            os.close(sub)
    finally:
        os.close(top)


def import_files(source: Path, config: tuple[int, str], data: tuple[int, str], system: System) -> None:
    """config/data: (open data folder, file name)."""
    conf_src = source / "heizungen.conf"
    data_src = next((c for c in (source / "data.json", source / "data" / "data.json") if c.is_file()), None)
    if not conf_src.is_file() and data_src is None:
        raise InstallError(f"In {source} liegt weder heizungen.conf noch data.json.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for src, (folder, name) in ((conf_src, config), (data_src, data)):
        if src is None or not src.is_file():
            continue
        old = read_private(folder, name)
        if old is not None:
            write_private(folder, f"{name}.bak-{stamp}", old, system)
        write_private(folder, name, src.read_text(encoding="utf-8"), system)
        say(f"• Übernommen: {src}")
    if conf_src.is_file():
        say(f"  Hinweis: {conf_src} enthält die Tokens – die Kopie dort kannst du jetzt löschen.")


# ---------------------------------------------------------------------- service, checks
def configured_port(unit: Path) -> int | None:
    if unit.exists():
        found = re.search(r"^Environment=HEIZAPP_PORT=(\d+)$", unit.read_text(encoding="utf-8"), re.M)
        if found:
            return int(found.group(1))
    return None


def write_units(root: Path, port: int, user: str = NAME) -> None:
    """The app's service plus the units behind the update button, the WLAN setup, the remote maintenance, the
    Zigbee programs and „Protokoll speichern“. user: the app's user (app_user)."""
    values = {"name": NAME, "user": user, "app": APP_DIR, "data": DATA_DIR, "port": port, "request": UPDATE_REQUEST,
              "wifi_request": WIFI_REQUEST, "remote_request": REMOTE_REQUEST, "zigbee_request": ZIGBEE_REQUEST,
              "logs_request": LOGS_REQUEST, "python": sys.executable or "/usr/bin/python3"}
    for unit, template in ((UNIT, UNIT_TEMPLATE), (UPDATE_UNIT, UPDATE_UNIT_TEMPLATE), (UPDATE_PATH, UPDATE_PATH_TEMPLATE),
                           (WIFI_UNIT, WIFI_UNIT_TEMPLATE), (WIFI_PATH, WIFI_PATH_TEMPLATE),
                           (REMOTE_UNIT, REMOTE_UNIT_TEMPLATE), (REMOTE_PATH, REMOTE_PATH_TEMPLATE),
                           (ZIGBEE_UNIT, ZIGBEE_UNIT_TEMPLATE), (ZIGBEE_PATH, ZIGBEE_PATH_TEMPLATE),
                           (LOGS_UNIT, LOGS_UNIT_TEMPLATE), (LOGS_PATH, LOGS_PATH_TEMPLATE)):
        path = root / unit.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(**values), encoding="utf-8")
        os.chmod(path, 0o644)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def set_timezone(system: System, wanted: str) -> None:
    """Heating plans and heater clocks follow this computer's time zone."""
    zone = system.run("timedatectl", "show", "-p", "Timezone", "--value", check=False)
    if zone in FACTORY_TZ and wanted not in FACTORY_TZ:
        system.run("timedatectl", "set-timezone", wanted)
        say(f"• Zeitzone von {zone or 'unbekannt'} auf {wanted} gestellt (Heizpläne und Heizungsuhren richten sich danach)")
    else:
        say(f"• Zeitzone: {zone}")


def wait_until_up(port: int, seconds: float = 45) -> str | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as res:
                return (json.load(res).get("app") or {}).get("version") or "?"
        except urllib.error.HTTPError as exc:
            if exc.code == 401:  # password set in /etc/default/holitherm – the app answers
                return "?"
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return None


def own_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent; only picks the outgoing interface
            return sock.getsockname()[0]
    except OSError:
        return None


# ---------------------------------------------------------------------- move from FlowThermPi (until 0.4.28)
# Until 0.4.28 everything of the app on the Pi was called „flowthermpi“. The first installation of a newer version moves
# it over: the folders are renamed in place (same files, same owners), the services written anew under the new names,
# Mosquitto gets new logins, the WLAN connections and the Pi's entry in Tailscale their new names, and so do the users.
# Started by the update button, this installer runs inside the old update service, whose program (still 0.4.28) writes
# its result as the app's old user at the very end. So the app's user – and the Pi's own name, which would cut the
# browser off in the middle of the update – follow half a minute later, in a service of their own (finish_move).
def app_user(system: System) -> str:
    """The app's user: NAME – or OLD_NAME as long as a move from FlowThermPi has not renamed it."""
    return OLD_NAME if not system.user_exists(NAME) and system.user_exists(OLD_NAME) else NAME


def unit_running(system: System, unit: str) -> bool:
    """unit is starting, running or stopping. A oneshot service like the update is „activating“ all the while it runs –
    „systemctl is-active“ does not count that."""
    state = system.run("systemctl", "show", "-p", "ActiveState", "--value", unit, check=False)
    return state in ("active", "activating", "deactivating", "reloading")


def old_installation(root: Path) -> bool:
    """Something of FlowThermPi is still there: a folder, a service or Mosquitto's settings."""
    units = root / "etc/systemd/system"
    return (any(os.path.lexists(root / old.lstrip("/")) for old, _ in OLD_MOVES)
            or any(os.path.lexists(units / unit) for unit in OLD_UNITS)
            or os.path.lexists(root / f"etc/mosquitto/conf.d/{OLD_NAME}.conf"))


def move_old_installation(system: System, root: Path) -> dict:
    """An installation of FlowThermPi becomes one of HoLiTherm – without one nothing happens. Returns what install()
    needs to know: the app's port, whether Zigbee2MQTT ran, and later=True when the app's user and the Pi's name have to
    wait for the old update service (schedule_finish)."""
    if not old_installation(root):
        return {}
    double = [f"{old} und {new}" for old, new in OLD_MOVES
              if os.path.lexists(root / old.lstrip("/")) and os.path.lexists(root / new.lstrip("/"))]
    if double:
        raise InstallError(f"Auf diesem Pi gibt es FlowThermPi und HoLiTherm nebeneinander ({', '.join(double)}) – "
                           "bitte zuerst eins davon entfernen.")
    if any(unit_running(system, f"{OLD_NAME}-{task}.service") for task in ("wifi", "remote", "zigbee", "logs")):
        raise InstallError("Die App erledigt gerade noch etwas (WLAN, Fernwartung, Zigbee oder Protokoll) – bitte in "
                           "ein paar Minuten noch einmal aktualisieren.")
    say("• Aus FlowThermPi wird HoLiTherm: Ordner, Dienste, Benutzer und Namen ziehen um")
    units = root / "etc/systemd/system"
    moved = {"port": configured_port(units / f"{OLD_NAME}.service"),
             "zigbee": os.path.lexists(units / f"multi-user.target.wants/{OLD_NAME}-zigbee2mqtt.service")}
    stick = zigbee2mqtt_unit_port(root, f"/etc/systemd/system/{OLD_NAME}-zigbee2mqtt.service")
    for unit in sorted(OLD_UNITS, key=lambda name: not name.endswith(".path")):   # first what starts the others
        if unit != OLD_UPDATE and os.path.lexists(units / unit):   # the old update runs on: this is it
            system.run("systemctl", "disable", "--now", unit, check=False)
    for old, new in OLD_MOVES:
        if os.path.lexists(root / old.lstrip("/")):
            (root / new.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
            os.rename(root / old.lstrip("/"), root / new.lstrip("/"))
            say(f"  {old} → {new}")
    if system.user_exists(f"{OLD_NAME}-zigbee") and not system.user_exists(ZIGBEE_USER):   # Zigbee2MQTT is stopped
        rename_user(system, f"{OLD_NAME}-zigbee", ZIGBEE_USER, ZIGBEE_DATA)
    for link in [units / unit for unit in OLD_UNITS] + [wants / unit for wants in units.glob("*.wants")
                                                         for unit in OLD_UNITS]:   # also links to nowhere
        if os.path.lexists(link):
            link.unlink()
    if stick:
        write_zigbee2mqtt_unit(root, stick)
    system.run("systemctl", "daemon-reload", check=False)
    system.run("systemctl", "reset-failed", *(unit for unit in OLD_UNITS if unit != OLD_UPDATE), check=False)
    for step in (move_mosquitto, rename_wifi_profiles, rename_tailscale_host, rename_app_logs, rename_tailscale_source):
        try:
            step(system, root)
        except (InstallError, OSError) as exc:   # all of this can wait – the app comes first
            say(f"  Hinweis: {exc}")
    moved["later"] = unit_running(system, OLD_UPDATE)
    return moved


def move_mosquitto(system: System, root: Path) -> None:
    """Mosquitto's settings and logins under the new name – the app logs in as „holitherm“ from now on."""
    old = (root / f"etc/mosquitto/conf.d/{OLD_NAME}.conf", root / f"etc/mosquitto/{OLD_NAME}.passwd")
    if not old[0].exists():
        return
    logins = mosquitto_logins(system, root, drop=old)
    folder = os.open(root / DATA_DIR.lstrip("/"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        zigbee_logins(system, root, folder, logins)
    finally:
        os.close(folder)
    restart_zigbee2mqtt(system)   # only if it runs under the new name already (a move that was cut short)
    say("  Mosquitto: neue Anmeldungen für die App und Zigbee2MQTT")


def rename_wifi_profiles(system: System, root: Path) -> None:
    """The WLAN connections FlowThermPi made („FlowThermPi WLAN <Netz>“) are called „HoLiTherm WLAN <Netz>“. Only the
    name changes – a connection in use stays up."""
    count = 0
    for uuid, kind, name in nm_rows(nm(system, "-t", "-f", "UUID,TYPE,NAME", "connection", "show", check=False), 3):
        if uuid and kind in ("802-11-wireless", "wifi") and name.startswith(f"{OLD_WIFI_PROFILE} "):
            nm(system, "connection", "modify", "uuid", uuid, "connection.id",
               WIFI_PROFILE + name[len(OLD_WIFI_PROFILE):])
            count += 1
    if count:
        say(f"  WLAN: {count} Verbindung{'en heißen' if count > 1 else ' heißt'} jetzt „{WIFI_PROFILE} …“")


def rename_tailscale_host(system: System, root: Path) -> None:
    """The Pi's name in Tailscale: flowthermpi-<Gerätenummer> becomes holitherm-<Gerätenummer>. Only the name changes –
    the login, the key and the tailnet's rules stay as they are."""
    number = device_number(root)
    state = tailscale_state(system)
    if not state.get("daemon") or state.get("host_name") != (f"{OLD_NAME}-{number}" if number else OLD_NAME):
        return
    code, out, err = tailscale(system, "set", f"--hostname={remote_name(root)}", timeout=30)
    if code:
        raise InstallError(f"Den Namen in Tailscale konnte ich nicht ändern ({first_line(err or out) or f'Fehlercode {code}'}).")
    say(f"  Tailscale: der Pi heißt dort jetzt „{remote_name(root)}“")


def rename_app_logs(system: System, root: Path) -> None:
    """The app's log files (data/logs/flowthermpi.log, .1 …) under the new name – „Protokoll speichern“ goes on finding
    them. The data folder belongs to the app: no link in it is followed."""
    flags, fds = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, []
    try:
        fds.append(os.open(root / DATA_DIR.lstrip("/"), flags))
        for name in ("data", "logs"):
            fds.append(os.open(name, flags, dir_fd=fds[-1]))
        names = set(os.listdir(fds[-1]))
        for name in sorted(names):
            found = re.fullmatch(rf"{OLD_NAME}\.log(\.\d{{1,2}})?", name)
            if found and f"{NAME}.log{found.group(1) or ''}" not in names:
                os.rename(name, f"{NAME}.log{found.group(1) or ''}", src_dir_fd=fds[-1], dst_dir_fd=fds[-1])
    except FileNotFoundError:
        pass
    finally:
        for fd in fds:
            os.close(fd)


def rename_tailscale_source(system: System, root: Path) -> None:
    """The comment in Tailscale's package source says who set it up."""
    path = root / TAILSCALE_SOURCE.lstrip("/")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    new = re.sub(r"(?im)^(#.*?)\bFlowThermPi\b", r"\1HoLiTherm", text)
    if new != text:
        write_system_file(path, new.encode())


def rename_user(system: System, old: str, new: str, home: str) -> bool:
    """A user and its own group get a new name – their numbers stay, so every file stays theirs. Not while a program of
    the user runs (usermod refuses). False if it did not work: then both keep the old name."""
    try:
        system.run("groupmod", "--new-name", new, old)
        try:
            system.run("usermod", "--login", new, "--home", home, old)
        except CommandError:
            system.run("groupmod", "--new-name", old, new, check=False)   # back: user and group must match
            raise
    except CommandError as exc:
        say(f"  Hinweis: Benutzer „{old}“ ließ sich nicht umbenennen ({first_line(exc.detail)}) – er bleibt vorerst.")
        return False
    say(f"  Benutzer „{old}“ heißt jetzt „{new}“")
    return True


def rename_host(system: System, root: Path) -> bool:
    """The Pi's own name, if it is still FlowThermPi's: http://flowthermpi.local becomes http://holitherm.local. The way
    raspi-config does it – hostnamectl and the line 127.0.1.1 in /etc/hosts. Any other name stays as it is."""
    try:
        current = (root / "etc/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if current.lower() != OLD_NAME:
        return False
    if not system.ok("hostnamectl", "set-hostname", NAME):
        write_system_file(root / "etc/hostname", f"{NAME}\n".encode())
        system.run("hostname", NAME, check=False)
    hosts = root / "etc/hosts"
    try:
        lines = hosts.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        lines = []
    changed = False
    for number, line in enumerate(lines):
        if line.split("#")[0].split()[:1] == ["127.0.1.1"]:
            lines[number] = re.sub(rf"(?i)(?<![\w-]){OLD_NAME}(?![\w-])", NAME, line)
            changed = changed or lines[number] != line
    if changed:
        write_system_file(hosts, "".join(lines).encode())
    system.run("systemctl", "try-restart", "avahi-daemon.service", check=False)   # tells the home network the new name
    say(f"• Der Pi heißt jetzt „{NAME}“ – im Heimnetz: http://{NAME}.local")
    return True


def finish_names(system: System, root: Path) -> bool:
    """The rest of a move from FlowThermPi: the app's user – its service must not run meanwhile – and the Pi's name,
    and Zigbee2MQTT's user if the move could not rename it. True if the app's user got its new name: its services have
    to be written anew."""
    if system.user_exists(f"{OLD_NAME}-zigbee") and not system.user_exists(ZIGBEE_USER):   # not done at the move
        system.run("systemctl", "stop", ZIGBEE_SERVICE, check=False)
        if rename_user(system, f"{OLD_NAME}-zigbee", ZIGBEE_USER, ZIGBEE_DATA):
            system.run("systemctl", "reset-failed", ZIGBEE_SERVICE, check=False)   # it could not start meanwhile
            restart_zigbee2mqtt(system)
    renamed = False
    if system.user_exists(OLD_NAME) and not system.user_exists(NAME):
        system.run("systemctl", "stop", NAME, check=False)   # usermod refuses while a program of the user runs
        renamed = rename_user(system, OLD_NAME, NAME, DATA_DIR)
        if not renamed:
            system.run("systemctl", "start", NAME, check=False)
    rename_host(system, root)
    return renamed


def schedule_finish(system: System) -> None:
    """The app's user and the Pi's name, half a minute after this update: in a service of its own (MOVE_UNIT) that
    first waits until the old update service is over."""
    system.run("systemctl", "reset-failed", f"{MOVE_UNIT}.service", check=False)
    try:
        system.run("systemd-run", f"--unit={MOVE_UNIT}", "--description=HoLiTherm – Umzug abschließen",
                   "--on-active=30", "--timer-property=AccuracySec=1s", "--collect",
                   sys.executable or "/usr/bin/python3", "-I", "-B", "-X", "utf8", f"{APP_DIR}/current/install.py",
                   "--finish-move")
    except CommandError as exc:
        say(f"  Hinweis: Den Rest des Umzugs macht das nächste Update ({first_line(exc.detail)}).")
        return
    say(f"• In einer halben Minute bekommen der Benutzer der App und der Pi ihre neuen Namen ({MOVE_UNIT})")


def finish_move(args: argparse.Namespace, system: System) -> None:
    """Run by MOVE_UNIT half a minute after an update from FlowThermPi: once the old update service is over, the app's
    user gets its new name – its services written anew, the app started again – and the Pi its own."""
    root = Path(args.root)
    for _ in range(450):   # the update is over within seconds – waits at most a quarter of an hour
        if not unit_running(system, OLD_UPDATE):
            break
        system.sleep(2)
    else:
        raise InstallError("Das alte Update läuft noch immer – den Rest des Umzugs macht das nächste Update.")
    with install_lock(root / APP_DIR.lstrip("/")):
        if finish_names(system, root):
            try:
                write_units(root, configured_port(root / UNIT.lstrip("/")) or DEFAULT_PORT, app_user(system))
                system.run("systemctl", "daemon-reload", check=False)
            finally:
                system.run("systemctl", "restart", NAME, check=False)
    say("✓ Umzug von FlowThermPi abgeschlossen.")


# ---------------------------------------------------------------------- commands
def check_host() -> None:
    if sys.platform != "linux":
        raise InstallError("Dieses Skript ist für den Raspberry Pi (Linux) gedacht.")
    if os.geteuid() != 0:
        raise InstallError("Bitte mit sudo starten:  … | sudo python3 -")
    if not os.path.isdir("/run/systemd/system"):
        raise InstallError("systemd fehlt – HoLiTherm braucht Raspberry Pi OS oder ein anderes Linux mit systemd.")


def install(args: argparse.Namespace, system: System) -> None:
    root = Path(args.root)
    app, data_dir, unit = root / APP_DIR.lstrip("/"), root / DATA_DIR.lstrip("/"), root / UNIT.lstrip("/")
    if args.commit and not COMMIT.fullmatch(args.commit):
        raise InstallError("--commit: 40 Zeichen 0-9/a-f erwartet.")
    moved = move_old_installation(system, root)   # before the lock: that lives in the folder that moves
    with install_lock(app):
        update = (app / "current").is_symlink()
        say(f"HoLiTherm – {'Update' if update else 'Einrichtung'}")

        with tempfile.TemporaryDirectory(prefix="holitherm-") as tmp:
            if args.source:
                src, commit = Path(args.source).expanduser().resolve(), args.commit
            else:
                src, commit = download(args.ref, Path(tmp))
            version = program_version(src)
            release = copy_release(src, app / "releases", version)
            write_release_info(release, version, commit, args.ref if commit or not args.source else None)
        say(f"• Programm HoLiTherm {version}{f' ({commit[:7]})' if commit else ''} nach {APP_DIR} kopiert")

        if not moved.get("later"):
            finish_names(system, root)   # the app's user and the Pi's name, if an earlier move left them
        if not system.user_exists(NAME) and not system.user_exists(OLD_NAME):
            system.run("useradd", "--system", "--user-group", "--home-dir", DATA_DIR, "--no-create-home",
                       "--shell", "/usr/sbin/nologin", NAME)
            say(f"• Benutzer „{NAME}“ für den Dienst angelegt")
        prepare_data(data_dir, system, args.import_dir)
        set_timezone(system, args.timezone)

        port = args.port or configured_port(unit) or moved.get("port") or DEFAULT_PORT
        running = system.ok("systemctl", "is-active", "--quiet", NAME)
        if system.live and not running and not port_free(port):
            shutil.rmtree(release)
            raise InstallError(f"Port {port} ist schon belegt (läuft dort ein anderer Webserver?). "
                               "Mit „--port 8080“ einen anderen Port wählen.")
        previous = switch_current(app, release)
        write_units(root, port, app_user(system))
        zigbee_changed = refresh_zigbee2mqtt_unit(root)   # Zigbee2MQTT's service, if it is set up
        system.run("systemctl", "daemon-reload")
        system.run("systemctl", "enable", NAME)
        system.run("systemctl", "enable", "--now", f"{NAME}-update.path")
        system.run("systemctl", "enable", "--now", f"{NAME}-wifi.path")
        system.run("systemctl", "enable", "--now", f"{NAME}-remote.path")
        system.run("systemctl", "enable", "--now", f"{NAME}-zigbee.path")
        system.run("systemctl", "enable", "--now", f"{NAME}-logs.path")
        system.run("systemctl", "restart", NAME)
        if moved.get("zigbee"):      # Zigbee2MQTT ran under its old name: now under the new one
            system.run("systemctl", "enable", "--now", ZIGBEE_SERVICE, check=False)
        elif zigbee_changed:
            system.run("systemctl", "try-restart", ZIGBEE_SERVICE, check=False)

        if system.live:
            say("• Starte HoLiTherm …")
            if wait_until_up(port) is None:
                say(system.run("journalctl", "-u", NAME, "-n", "25", "--no-pager", check=False))
                if previous is not None:
                    switch_current(app, previous)
                    system.run("systemctl", "restart", NAME, check=False)
                    shutil.rmtree(release, ignore_errors=True)
                    raise InstallError("Die neue Version startet nicht – die vorherige läuft wieder (Meldungen oben).")
                raise InstallError("HoLiTherm startet nicht (Meldungen oben).")
        prune(app)
    if moved.get("later"):
        schedule_finish(system)

    suffix = "" if port == 80 else f":{port}"
    address = own_address()
    say("")
    say(f"✓ HoLiTherm {version} läuft.")
    say(f"  Öffnen im Heimnetz:  http://{socket.gethostname().split('.')[0]}.local{suffix}"
        + (f"   oder   http://{address}{suffix}" if address else ""))
    say("  Update später:       in der App unter Einrichtung › Über – oder diesen Befehl noch einmal")
    say(f"  Meldungen ansehen:   journalctl -u {NAME} -f   – oder in der App „Protokoll speichern“")


def update_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by holitherm-update.service when the app has placed its request (button „Jetzt aktualisieren“).

    Fetches GitHub main of REPO and lets the installer of that new version install it – exactly what the
    one-line command does. The request only says „please update“; what gets installed is decided here.
    Progress and result go to update-status.json for the app."""
    root = Path(args.root)
    app, data_dir = root / APP_DIR.lstrip("/"), root / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            os.unlink(UPDATE_REQUEST, dir_fd=folder)  # first: while it exists, the path unit starts us again
        except FileNotFoundError:
            say("Keine Update-Anfrage der App – nichts zu tun.")
            return
        before = release_info(app / "current")
        status = {"state": "running", "started": now_text(), "from": before.get("version"),
                  "from_commit": before.get("commit")}
        write_private(folder, UPDATE_STATUS, json.dumps(status, indent=1) + "\n", system)
        try:
            with tempfile.TemporaryDirectory(prefix="holitherm-") as tmp:
                src, commit = download(args.ref, Path(tmp))
                status.update(to=program_version(src), commit=commit)
                if commit and commit == before.get("commit"):
                    status.update(state="done", message="Das ist schon die neueste Version.")
                    say("✓ Schon die neueste Version – nichts zu tun.")
                else:
                    run_new_installer(src, commit, args)
                    status.update(state="done", message=f"Version {status['to']} ist installiert.")
        except InstallError as exc:
            status.update(state="failed", message=str(exc))
            raise
        finally:
            if status["state"] == "running":
                status.update(state="failed", message=f"Unerwarteter Fehler – Meldungen: journalctl -u {NAME}-update")
            status["finished"] = now_text()
            write_private(folder, UPDATE_STATUS, json.dumps(status, indent=1) + "\n", system)
    finally:
        os.close(folder)


def run_new_installer(src: Path, commit: str | None, args: argparse.Namespace) -> None:
    """The downloaded version installs itself: its installer may know more than this one."""
    if not (src / "install.py").is_file():
        raise InstallError("Die neue Version bringt kein install.py mit.")
    cmd = [sys.executable, "-I", "-B", "-X", "utf8", str(src / "install.py"), "--source", str(src), "--ref", args.ref]
    if commit:
        cmd += ["--commit", commit]
    if args.root != "/":
        cmd += ["--root", args.root]
    try:
        res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Das Update dauert ungewöhnlich lange und wurde abgebrochen.") from exc
    for text in (res.stdout, res.stderr):
        if text.strip():
            say(text.rstrip())
    if res.returncode != 0:
        reason = next((line.lstrip("✗ ").strip() for line in reversed(res.stdout.splitlines()) if line.startswith("✗")), "")
        raise InstallError(reason or f"Das Update ist fehlgeschlagen (Fehlercode {res.returncode}).")


# ---------------------------------------------------------------------- WLAN (Einrichtung › WLAN)
# The Pi is delivered for a network cable; the WLAN is set up in the app. The app may not change the
# network itself: it places {data}/wifi-request.json and holitherm-wifi.path runs „install.py --wifi“
# as root. Only three actions exist (scan, connect, forget); every value is checked here again, and
# NetworkManager is driven with argument lists, never through a shell. The password is handed to nmcli
# once („connection add“, a fraction of a second in its arguments) and never written to the status
# file or the log. A failed connection is removed again and the previous WLAN comes back.
def nm(system: System, *args: str, check: bool = True, redact: str = "") -> str:
    """nmcli with English, machine-readable output and UTF-8 network names."""
    return system.run("nmcli", *args, check=check, env={**os.environ, "LC_ALL": "C.UTF-8"}, redact=redact)


def nm_fields(line: str) -> list[str]:
    """One line of „nmcli -t“: fields split at „:“; a backslash keeps a following „:“ or backslash as it is."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def nm_rows(text: str, count: int) -> list[list[str]]:
    return [(nm_fields(line) + [""] * count)[:count] for line in text.splitlines() if line.strip()]


def check_ssid(value) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise InstallError("Der WLAN-Name ist ungültig (1 bis 32 Zeichen).")
    return value


def check_wifi_password(value) -> str:
    if value in (None, ""):
        return ""   # open network
    if not isinstance(value, str) or not (re.fullmatch(r"[\x20-\x7e]{8,63}", value) or re.fullmatch(r"[0-9a-fA-F]{64}", value)):
        raise InstallError("Das WLAN-Passwort muss 8 bis 63 Zeichen lang sein (ohne Umlaute).")
    return value


def wifi_request(text: str) -> dict:
    """The app's request, checked: nothing from it reaches nmcli unchecked."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise InstallError("Die WLAN-Anfrage der App ist unlesbar.") from exc
    if not isinstance(raw, dict) or raw.get("action") not in WIFI_ACTIONS:
        raise InstallError("Unbekannte WLAN-Anfrage.")
    request = {"action": raw["action"], "country": raw.get("country") or "DE"}
    if request["country"] not in WIFI_COUNTRIES:
        raise InstallError("Unbekanntes Land für das WLAN.")
    if raw["action"] in ("connect", "forget"):
        request["ssid"] = check_ssid(raw.get("ssid"))
    if raw["action"] == "connect":
        request["password"] = check_wifi_password(raw.get("password"))
        request["hidden"] = raw.get("hidden") is True
    return request


def request_id(text: str) -> str | None:
    try:
        rid = json.loads(text).get("id")
    except (ValueError, AttributeError):
        return None
    return rid if isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{8,32}", rid) else None


def ipv4_addresses(system: System) -> dict[str, list[str]]:
    try:
        links = json.loads(system.run("ip", "-j", "-4", "addr", "show", check=False) or "[]")
    except ValueError:
        return {}
    result = {}
    for link in links if isinstance(links, list) else []:
        if isinstance(link, dict) and isinstance(link.get("ifname"), str):
            result[link["ifname"]] = [a["local"] for a in link.get("addr_info") or []
                                      if isinstance(a, dict) and a.get("family") == "inet" and isinstance(a.get("local"), str)]
    return result


def wifi_country(system: System) -> str | None:
    """WLAN country (regulatory domain); without one, Raspberry Pi OS keeps the WLAN switched off."""
    found = re.search(r"^country ([A-Z0-9]{2}):", system.run("iw", "reg", "get", check=False), re.M)
    return found.group(1) if found and found.group(1) not in ("00", "99") else None


def wifi_profiles(system: System) -> list[dict]:
    """Saved WLAN connections: uuid, name, ssid."""
    profiles = []
    for uuid, kind, name in nm_rows(nm(system, "-t", "-f", "UUID,TYPE,NAME", "connection", "show"), 3):
        if kind in ("802-11-wireless", "wifi") and uuid:
            fields = nm_fields(nm(system, "-t", "-f", "802-11-wireless.ssid", "connection", "show", "uuid", uuid,
                                  check=False).strip())
            profiles.append({"uuid": uuid, "name": name, "ssid": ":".join(fields[1:])})
    return profiles


def active_wifi(system: System, device: str) -> str | None:
    """uuid of the connection active on the WLAN adapter."""
    for uuid, kind, dev in nm_rows(nm(system, "-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"), 3):
        if dev == device and kind in ("802-11-wireless", "wifi"):
            return uuid
    return None


def wifi_state(system: System) -> dict:
    """Cable and WLAN as NetworkManager sees them, with IPv4 addresses and the WLAN country."""
    try:
        rows = nm_rows(nm(system, "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"), 4)
    except InstallError as exc:
        raise InstallError("NetworkManager antwortet nicht – das WLAN lässt sich auf diesem Gerät nicht einrichten.") from exc
    addresses = ipv4_addresses(system)
    interfaces = []
    for device, kind, state, connection in rows:
        if kind not in ("ethernet", "wifi"):
            continue
        entry = {"name": device, "type": kind, "state": state, "ipv4": addresses.get(device, [])}
        if kind == "wifi" and state.startswith("connected"):
            for in_use, ssid, signal in nm_rows(nm(system, "-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list",
                                                   "ifname", device, "--rescan", "no", check=False), 3):
                if in_use == "*":
                    entry.update(ssid=ssid, signal=int(signal) if signal.isdigit() else None)
                    break
            else:
                entry["ssid"] = next((p["ssid"] for p in wifi_profiles(system) if p["name"] == connection), connection)
        interfaces.append(entry)
    return {"interfaces": interfaces, "country": wifi_country(system)}


def wifi_power_on(system: System, country: str, state: dict) -> None:
    """Set the country if none is set yet (then Raspberry Pi OS switches the WLAN on) and unblock the radio."""
    if not state["country"]:
        try:   # raspi-config also keeps the country for the next start
            system.run("raspi-config", "nonint", "do_wifi_country", country)
        except InstallError:
            system.run("iw", "reg", "set", country, check=False)
        state["country"] = country
        say(f"• WLAN-Land auf {country} gestellt")
    system.run("rfkill", "unblock", "wifi", check=False)
    nm(system, "radio", "wifi", "on", check=False)


def wifi_scan(system: System, device: str, rescan: bool = True) -> list[dict]:
    """Networks in range, the strongest access point per name; hidden networks (no name) are left out.
    rescan=False: what NetworkManager saw last – quick, but possibly incomplete."""
    text, error = None, None
    for attempt in range(6 if rescan else 1):   # right after switching on, the adapter needs a few seconds
        if attempt:
            system.sleep(2)
        try:
            text = nm(system, "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                      "ifname", device, "--rescan", "yes" if rescan else "no")
        except InstallError as exc:
            error = exc
            continue
        if text or attempt or not rescan:   # an empty first list: the adapter has only just woken up
            break
    if text is None:
        raise InstallError("Das WLAN des Pi ist noch nicht bereit – bitte gleich noch einmal suchen.") from error
    best: dict[str, dict] = {}
    for in_use, ssid, signal, security in nm_rows(text, 4):
        if not ssid:
            continue
        security = "" if security.strip() in ("", "--") else security.strip()
        entry = {"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0, "security": security,
                 "active": in_use == "*", "enterprise": "802.1X" in security}
        old = best.get(ssid)
        if old is None or entry["signal"] > old["signal"]:
            entry["active"] = entry["active"] or bool(old and old["active"])
            best[ssid] = entry
        elif entry["active"]:
            old["active"] = True
    saved = {p["ssid"] for p in wifi_profiles(system)}
    for entry in best.values():
        entry["saved"] = entry["ssid"] in saved
    return sorted(best.values(), key=lambda e: (not e["active"], -e["signal"], e["ssid"].lower()))[:40]


def wifi_reason(detail: str, ssid: str) -> str:
    """nmcli's error in words for the app."""
    text = detail.lower()
    if "secrets were required" in text or "802-11-wireless-security" in text or "psk" in text:
        return "Das Passwort passt nicht. Bitte noch einmal eingeben (auf Groß- und Kleinschreibung achten)."
    if re.search(r"no network with ssid|not (be )?found", text):
        return f"Das WLAN „{ssid}“ ist nicht in Reichweite."
    if "ip configuration" in text or "dhcp" in text:
        return f"Mit „{ssid}“ verbunden, aber keine Adresse vom Router bekommen."
    if "timeout" in text or "timed out" in text:
        return f"„{ssid}“ antwortet nicht – Passwort falsch oder der Pi zu weit weg?"
    first = (detail.strip().splitlines() or [""])[0].removeprefix("Error:").strip()[:160]
    return f"Verbinden hat nicht geklappt ({first or 'unbekannter Fehler'})."


def wifi_connect(system: System, request: dict, device: str) -> None:
    ssid, password = request["ssid"], request["password"]
    try:
        networks = {n["ssid"]: n for n in wifi_scan(system, device, rescan=False)}
    except InstallError:   # the WLAN was only just switched on
        networks = {}
    if ssid not in networks and not request["hidden"]:
        networks = {n["ssid"]: n for n in wifi_scan(system, device)}
    network = networks.get(ssid)
    if network is None and not request["hidden"]:
        raise InstallError(f"Das WLAN „{ssid}“ ist nicht in Reichweite.")
    if network and network["enterprise"]:
        raise InstallError("Firmen-WLAN mit eigener Anmeldung (802.1X) lässt sich hier nicht einrichten.")
    secured = bool(network["security"]) if network else bool(password)
    if secured and not password:
        raise InstallError(f"Für „{ssid}“ fehlt das Passwort.")
    wpa3_only = bool(network) and "WPA3" in network["security"] and not re.search(r"WPA[12]", network["security"])
    profiles, previous = wifi_profiles(system), active_wifi(system, device)
    args = ["connection", "add", "type", "wifi", "ifname", device, "con-name", f"{WIFI_PROFILE} {ssid}",
            "802-11-wireless.ssid", ssid, "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"]
    if request["hidden"]:
        args += ["802-11-wireless.hidden", "yes"]
    if secured:
        args += ["802-11-wireless-security.key-mgmt", "sae" if wpa3_only else "wpa-psk",
                 "802-11-wireless-security.psk", password]
    try:
        added = nm(system, *args, redact=password)
    except CommandError as exc:
        raise InstallError(wifi_reason(exc.detail, ssid)) from None
    found = re.search(r"\(([0-9a-fA-F-]{36})\)", added)
    if not found:
        raise InstallError("NetworkManager hat die neue Verbindung nicht bestätigt.")
    uuid = found.group(1)
    say(f"• Verbinde mit „{ssid}“ …")
    try:
        nm(system, "--wait", "45", "connection", "up", "uuid", uuid)
    except CommandError as exc:
        nm(system, "connection", "delete", "uuid", uuid, check=False)
        if previous:   # back to the WLAN that worked before
            nm(system, "--wait", "30", "connection", "up", "uuid", previous, check=False)
        raise InstallError(wifi_reason(exc.detail, ssid)) from None
    for profile in profiles:   # older entries for this network are outdated now (e.g. old password)
        if profile["ssid"] == ssid and profile["uuid"] != uuid:
            nm(system, "connection", "delete", "uuid", profile["uuid"], check=False)


def wifi_forget(system: System, ssid: str) -> int:
    removed = 0
    for profile in wifi_profiles(system):
        if profile["ssid"] == ssid:
            nm(system, "connection", "delete", "uuid", profile["uuid"], check=False)
            removed += 1
    return removed


def wifi_only_link(state: dict, ssid: str) -> bool:
    """The Pi is in this WLAN and no cable is in: forgetting it would cut the Pi off – also for remote access
    (Tailscale and the like run over that WLAN, too)."""
    up = [i for i in state["interfaces"] if i["state"].startswith("connected")]
    return not any(i["type"] == "ethernet" for i in up) and any(i["type"] == "wifi" and i.get("ssid") == ssid for i in up)


def run_wifi(system: System, request: dict, status: dict) -> None:
    state = wifi_state(system)
    device = next((i["name"] for i in state["interfaces"] if i["type"] == "wifi"), None)
    if device is None:
        raise InstallError("Dieses Gerät hat keinen WLAN-Adapter, den NetworkManager verwaltet.")
    ssid = request.get("ssid")
    if request["action"] == "scan":
        wifi_power_on(system, request["country"], state)
        status["networks"] = wifi_scan(system, device)
    elif request["action"] == "connect":
        wifi_power_on(system, request["country"], state)
        wifi_connect(system, request, device)
        status["message"] = f"Verbunden mit „{ssid}“."
    else:
        if wifi_only_link(state, ssid):
            raise InstallError(WIFI_ONLY_LINK.format(ssid=ssid))
        removed = wifi_forget(system, ssid)
        status["message"] = f"„{ssid}“ ist vergessen." if removed else f"„{ssid}“ war nicht gespeichert."
    say(f"✓ {status.get('message') or 'WLAN-Netze gesucht.'}")


def wifi_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by holitherm-wifi.service when the app has placed a WLAN request. The result goes to
    wifi-status.json, together with the state of cable and WLAN afterwards."""
    data_dir = Path(args.root) / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            text = read_private(folder, WIFI_REQUEST)
        finally:
            try:
                os.unlink(WIFI_REQUEST, dir_fd=folder)   # at once: it may hold a password, and the path unit restarts us
            except FileNotFoundError:
                pass
        if text is None:
            say("Keine WLAN-Anfrage der App – nichts zu tun.")
            return
        status = {"id": request_id(text), "action": None, "state": "running", "at": now_text()}
        try:
            request = wifi_request(text)
            status["action"] = request["action"]
            if request["action"] == "connect":   # takes up to a minute: the app shows „verbinde …“
                status["ssid"] = request["ssid"]
                write_private(folder, WIFI_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)
            run_wifi(system, request, status)
            status["state"] = "done"
        except InstallError as exc:   # reported to the app; not an error of the service itself
            status.update(state="failed", message=str(exc))
            say(f"✗ {exc}")
        try:   # how things are now; for the app's list also the networks NetworkManager sees
            status["net"] = wifi_state(system)
            wifi = next((i for i in status["net"]["interfaces"] if i["type"] == "wifi"), None)
            if "networks" not in status and wifi and status["net"]["country"]:
                status["networks"] = wifi_scan(system, wifi["name"], rescan=False)
        except InstallError:
            pass
        status["at"] = now_text()
        write_private(folder, WIFI_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)
    finally:
        os.close(folder)


# ---------------------------------------------------------------------- Fernwartung (Einrichtung › Fernwartung)
# Remote maintenance with Tailscale: the Pi joins the private network ("tailnet") of whoever signs it in, and
# from there the app can be opened from afar. The app places {data}/remote-request.json with nothing but one of
# REMOTE_ACTIONS, and holitherm-remote.path runs „install.py --remote“ as root. Tailscale is installed only when
# „up“ asks for it, from Tailscale's own signed package archive; it then keeps itself up to date (auto-update).
# The name in the tailnet is decided here (holitherm-<device number>), never by the app. Signing in works with
# a login link that the app shows as a QR code: whoever completes it decides whose tailnet the Pi joins. The Pi
# asks for REMOTE_TAG when it signs in, so nobody has to tag it by hand – and a device tagged at its first login
# keeps its key without expiry. The tailnet must name owners for that tag (README, „Tailscale-Netz einrichten“).
def device_number(root: Path) -> str | None:
    """Eight hex digits that tell this Pi from others: the end of its serial number, part of its name in Tailscale.
    The same as heizapp.remote.device_number (the app shows it, e.g. for a sticker on the Pi)."""
    serial = ""
    try:
        serial = (root / "sys/firmware/devicetree/base/serial-number").read_bytes().decode("ascii", "replace")
    except OSError:
        pass
    if not serial.strip("\x00 \t\n"):
        try:
            found = re.search(r"^Serial\s*:\s*(\S+)\s*$", (root / "proc/cpuinfo").read_text(encoding="ascii", errors="replace"), re.M)
            serial = found.group(1) if found else ""
        except OSError:
            pass
    serial = serial.strip("\x00 \t\n").lower()
    if re.fullmatch(r"[0-9a-f]{8,}", serial) and int(serial, 16):
        return serial[-8:]
    try:   # not a Raspberry Pi: derived from the machine id (which itself should not be shown)
        machine = (root / "etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return hashlib.sha256(f"holitherm:{machine}".encode()).hexdigest()[:8] if re.fullmatch(r"[0-9a-f]{32}", machine) else None


def remote_name(root: Path) -> str:
    number = device_number(root)
    return f"{NAME}-{number}" if number else NAME


def remote_request(text: str) -> str:
    """The app's request, checked: only the action counts, nothing else in it is used."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise InstallError("Die Anfrage der App zur Fernwartung ist unlesbar.") from exc
    if not isinstance(raw, dict) or raw.get("action") not in REMOTE_ACTIONS:
        raise InstallError("Unbekannte Anfrage zur Fernwartung.")
    return raw["action"]


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:200]


def json_objects(text: str) -> list[dict]:
    """The JSON objects in text, one after the other („tailscale up --json“ prints them as they come)."""
    decoder, found, pos = json.JSONDecoder(), [], 0
    while (pos := text.find("{", pos)) >= 0:
        try:
            item, pos = decoder.raw_decode(text, pos)
        except ValueError:
            pos += 1
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def tailscale(system: System, *args: str, timeout: float = 60) -> tuple[int, str, str]:
    return system.call("tailscale", *args, timeout=timeout)


def tailscale_state(system: System) -> dict:
    """Tailscale as „tailscale status --json“ reports it – only what the app shows."""
    try:
        code, out, err = tailscale(system, "status", "--json", timeout=30)
    except ProgramMissing:
        return {"installed": False}
    raw = None
    if code == 0:
        try:
            raw = json.loads(out)
        except ValueError:
            pass
    if not isinstance(raw, dict):   # e.g. „failed to connect to local tailscaled“
        return {"installed": True, "daemon": False, "problem": first_line(err or out) or f"Fehlercode {code}"}

    def obj(value) -> dict:
        return value if isinstance(value, dict) else {}

    def text(value) -> str | None:
        return value if isinstance(value, str) and value else None

    me, tailnet = obj(raw.get("Self")), obj(raw.get("CurrentTailnet"))
    user = obj(obj(raw.get("User")).get(str(me.get("UserID"))))
    ips = me.get("TailscaleIPs") or raw.get("TailscaleIPs")
    return {"installed": True, "daemon": True, "state": text(raw.get("BackendState")),
            "version": (text(raw.get("Version")) or "").split("-")[0] or None,
            "auth_url": text(raw.get("AuthURL")), "host_name": text(me.get("HostName")),
            "dns_name": (text(me.get("DNSName")) or "").rstrip(".") or None,
            "ips": [ip for ip in ips if isinstance(ip, str)] if isinstance(ips, list) else [],
            "key_expiry": text(me.get("KeyExpiry")),
            "tags": [t for t in me.get("Tags") or [] if isinstance(t, str)] if isinstance(me.get("Tags"), list) else [],
            "tailnet": text(tailnet.get("Name")), "magic_dns": tailnet.get("MagicDNSEnabled") is True,
            "user": text(user.get("LoginName"))}


def tailscale_wait(system: System, done, seconds: int) -> dict:
    """Ask Tailscale once a second until done(state) or the time is up."""
    state = tailscale_state(system)
    for _ in range(seconds):
        if done(state):
            break
        system.sleep(1)
        state = tailscale_state(system)
    return state


def os_release(root: Path) -> dict:
    for name in ("etc/os-release", "usr/lib/os-release"):
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        info = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and re.fullmatch(r"[A-Z_]+", key):
                info[key] = value.strip().strip("\"'")
        return info
    return {}


def fetch(url: str, missing: str, limit: int = 1 << 16) -> bytes:
    """A small file over HTTPS (checked against the system's certificates)."""
    error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=30) as res:
                data = res.read(limit + 1)
            if len(data) > limit:
                raise InstallError(f"{url} ist unerwartet groß – abgebrochen.")
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise InstallError(missing) from exc
            error = exc
        except (urllib.error.URLError, OSError) as exc:
            error = exc
        time.sleep(3 * attempt)
    raise InstallError(f"Download von {url} fehlgeschlagen ({error}). Hat der Pi Internet?")


def write_system_file(path: Path, data: bytes) -> None:
    """A file of the system (root's own folder, 0644), replaced in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def apt_reason(detail: str) -> str:
    errors = [line.strip() for line in detail.splitlines() if line.startswith("E:")]
    return (errors[0] if errors else first_line(detail) or "unbekannter Fehler")[:200]


def install_tailscale(system: System, root: Path) -> None:
    """Tailscale from its own package archive – what Tailscale's installer does, without running a script from the web.
    Only the signing key is downloaded; the archive's address is fixed here."""
    info = os_release(root)
    distro, codename = info.get("ID", ""), info.get("VERSION_CODENAME", "")
    if distro not in ("debian", "raspbian") or not re.fullmatch(r"[a-z]{3,20}", codename):
        raise InstallError("Tailscale lässt sich hier nur unter Raspberry Pi OS oder Debian einrichten.")
    say(f"• Lade den Schlüssel von Tailscales Paketarchiv ({distro} {codename}) …")
    key = fetch(f"{TAILSCALE_PACKAGES}/{distro}/{codename}.noarmor.gpg",
                missing=f"Für {distro} „{codename}“ bietet Tailscale (noch) keine Pakete an.")
    if len(key) < 100 or not key[0] & 0x80:   # an OpenPGP key starts with a packet tag – not e.g. a web page
        raise InstallError("Der Schlüssel von Tailscales Paketarchiv sieht nicht richtig aus – abgebrochen.")
    write_system_file(root / TAILSCALE_KEYRING.lstrip("/"), key)
    write_system_file(root / TAILSCALE_SOURCE.lstrip("/"), TAILSCALE_SOURCE_TEMPLATE.format(
        keyring=TAILSCALE_KEYRING, packages=TAILSCALE_PACKAGES, distro=distro, codename=codename).encode())
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"}
    apt = ("apt-get", "-o", "DPkg::Lock::Timeout=300")   # waits if the system is installing updates right now
    say("• Lade die Paketlisten …")
    system.run(*apt, "update", check=False, env=env)     # a broken other archive must not stop us
    say("• Installiere Tailscale …")
    try:
        system.run(*apt, "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
                   "install", "-y", "tailscale", "tailscale-archive-keyring", env=env)
    except CommandError as exc:
        raise InstallError(f"Tailscale ließ sich nicht installieren ({apt_reason(exc.detail)}).") from None
    system.run("systemctl", "enable", "--now", "tailscaled", check=False)


def tailscale_login(system: System, hostname: str) -> dict:
    """„tailscale up“ with this Pi's settings. Returns what it printed – the login link and its QR code when the Pi
    still has to be signed in. It waits only a few seconds: the login goes on in tailscaled, and the app follows it."""
    settings = (f"--hostname={hostname}", "--accept-dns=false", f"--advertise-tags={REMOTE_TAG}")
    code, out, err = tailscale(system, "up", "--json", "--qr", f"--timeout={TAILSCALE_LOGIN_WAIT}s", *settings,
                               timeout=TAILSCALE_LOGIN_WAIT + 60)
    found: dict = {}
    for item in json_objects(out):
        found.update({key: value for key, value in item.items() if isinstance(value, str) and value})
    if code != 0 and not found.get("AuthURL") and "timeout waiting" not in err + out:
        reason = found.get("Error") or first_line(err or out) or f"Fehlercode {code}"
        if "non-default" in reason:   # someone changed Tailscale's settings by hand: „up“ wants them all repeated
            reason = (f"Auf dem Pi wurden Einstellungen von Tailscale von Hand geändert. Bitte dort per SSH anmelden: "
                      f"sudo tailscale up {' '.join(settings)} – und die eigenen Einstellungen dazu")
        raise InstallError(f"Tailscale ließ sich nicht verbinden: {reason}")
    return found


def remote_up(system: System, root: Path, status: dict, report, setup: bool = True) -> None:
    """„up“ (Einrichtung): install Tailscale if needed, switch it on, sign the Pi in if needed. setup=False is „on“,
    the residents' Fernhilfe: it only switches on what is set up – it never installs anything or opens a login."""
    state = tailscale_state(system)
    if not state["installed"]:
        if not setup:
            raise InstallError("Die Fernwartung ist auf diesem Pi noch nicht eingerichtet.")
        status["phase"] = "installing"
        report()
        install_tailscale(system, root)
        state = tailscale_state(system)
        if not state["installed"]:
            raise InstallError("Tailscale ist nach der Installation nicht zu finden.")
    status["phase"] = "connecting"
    report()
    if not state.get("daemon"):
        system.run("systemctl", "enable", "--now", "tailscaled", check=False)
        state = tailscale_wait(system, lambda s: s.get("daemon"), 20)
        if not state.get("daemon"):
            raise InstallError(f"Tailscale startet nicht ({state.get('problem') or 'keine Antwort'}).")
    if state.get("state") == "Stopped":   # signed in, only switched off: on again – every setting stays as it is
        code, _, _ = tailscale(system, "up", timeout=60)
        if code == 0:
            state = tailscale_wait(system, lambda s: s.get("state") in ("Running", "NeedsLogin", "NeedsMachineAuth"), 20)
    if state.get("state") not in ("Running", "Starting", "NeedsMachineAuth"):
        if not setup:
            raise InstallError("Der Pi ist bei Tailscale nicht angemeldet. Neu anmelden lässt er sich in der "
                               "Einrichtung › Fernwartung.")
        hostname = remote_name(root)
        say(f"• Melde den Pi als „{hostname}“ bei Tailscale an …")
        printed = tailscale_login(system, hostname)
        state = tailscale_wait(system, lambda s: s.get("state") in ("Running", "NeedsMachineAuth") or bool(s.get("auth_url")), 30)
        if state.get("state") == "NeedsLogin" and state.get("auth_url"):
            # this run's own login: „tailscale up“ with settings starts tailscaled's login afresh, so a link that was
            # open before (e.g. from „tailscale up“ by hand, with the plain name) is gone. The app shows only this one.
            status["login_url"] = state["auth_url"]
            if state["auth_url"] == printed.get("AuthURL") and printed.get("QR"):
                status["qr"] = printed["QR"]
        if state.get("state") == "NeedsLogin" and not state.get("auth_url"):
            raise InstallError("Tailscale hat keinen Anmelde-Link geschickt. Hat der Pi Internet? Bitte gleich noch einmal versuchen.")
    if setup:
        code, out, err = tailscale(system, "set", "--auto-update=true", timeout=30)   # Tailscale keeps itself up to date
        status["auto_update"] = code == 0
        if code:
            say(f"  Automatische Updates von Tailscale ließen sich nicht einschalten: {first_line(err or out)}")
    status["message"] = {"Running": "Die Fernwartung ist an.",
                         "NeedsLogin": "Bitte den Pi jetzt bei Tailscale anmelden.",
                         "NeedsMachineAuth": "Der Pi wartet auf die Freigabe in der Tailscale-Verwaltung."}.get(
        state.get("state"), "Tailscale verbindet sich noch – bitte gleich noch einmal nachsehen.")


def run_remote(system: System, root: Path, action: str, status: dict, report) -> None:
    if action in ("up", "on"):
        remote_up(system, root, status, report, setup=action == "up")
        say(f"✓ {status['message']}")
        return
    state = tailscale_state(system)
    if action == "status" or not state.get("daemon"):
        return
    if action == "down":
        code, out, err = tailscale(system, "down", timeout=60)
        if code and tailscale_state(system).get("state") in ("Running", "Starting"):
            raise InstallError(f"Ausschalten hat nicht geklappt: {first_line(err or out) or f'Fehlercode {code}'}")
        status["message"] = "Die Fernwartung ist aus."
    else:   # logout – also ends a login that is still open; afterwards nothing comes up again by itself
        code, out, err = tailscale(system, "logout", timeout=60)
        tailscale(system, "down", timeout=60)
        state = tailscale_state(system)
        if state.get("auth_url"):   # a login nobody finished: „logout“ leaves it open (tailscaled has no key yet to log
            system.run("systemctl", "restart", "tailscaled", check=False)   # out) – it lives only in memory
            state = tailscale_wait(system, lambda s: s.get("daemon") and not s.get("auth_url"), 20)
        if state.get("state") in ("Running", "Starting", "Stopped") or state.get("auth_url"):
            why = "die Anmeldung ist noch offen" if state.get("auth_url") else first_line(err or out) or "Tailscale ist noch angemeldet"
            raise InstallError(f"Abmelden hat nicht geklappt: {why}")
        status["message"] = "Der Pi ist bei Tailscale abgemeldet."
    say(f"✓ {status['message']}")


def remote_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by holitherm-remote.service when the app has placed a request. The result goes to remote-status.json,
    always with Tailscale's state afterwards."""
    root = Path(args.root)
    data_dir = root / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            text = read_private(folder, REMOTE_REQUEST)
        finally:
            try:
                os.unlink(REMOTE_REQUEST, dir_fd=folder)   # first: while it exists, the path unit starts us again
            except FileNotFoundError:
                pass
        if text is None:
            say("Keine Anfrage der App zur Fernwartung – nichts zu tun.")
            return
        status = {"id": request_id(text), "action": None, "state": "running", "phase": None, "at": now_text(),
                  "device": device_number(root)}

        def report() -> None:
            status["at"] = now_text()
            write_private(folder, REMOTE_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)

        try:
            status["action"] = remote_request(text)
            run_remote(system, root, status["action"], status, report)
            status["state"] = "done"
        except InstallError as exc:   # reported to the app; not an error of the service itself
            status.update(state="failed", message=str(exc))
            say(f"✗ {exc}")
        status["phase"] = None
        status["tailscale"] = tailscale_state(system)
        report()
    finally:
        os.close(folder)


# ---------------------------------------------------------------------- Zigbee (Einrichtung › Zigbee)
# SONOFF's Zigbee thermostats need three extra programs on the Pi: Mosquitto (passes the messages between Zigbee2MQTT
# and the app; from Debian's own archive), Node.js and Zigbee2MQTT (which talks to the Zigbee USB stick). The app
# places {data}/zigbee-request.json with nothing but one of ZIGBEE_ACTIONS, and holitherm-zigbee.path runs
# „install.py --zigbee“ as root. What gets installed is fixed in ZIGBEE_PARTS for each version of HoLiTherm – the app
# cannot choose – and every download is checked: Node.js and pnpm against their checksums, Zigbee2MQTT against its Git
# commit, its building blocks against the checksums in its own lockfile (pnpm install --frozen-lockfile). Zigbee2MQTT
# is built (and later run) by its own user, never by root. Mosquitto listens only on this Pi and only with a password
# – one for the app, one for Zigbee2MQTT, made anew at every installation and never shown or logged. Zigbee2MQTT itself
# runs only once it is set up („Zigbee einrichten“, with the stick plugged in): the app names the radio channel and the
# stick, nothing else – the settings and the service come from the templates here, the network key from Zigbee2MQTT.
def zigbee_request(text: str) -> dict:
    """The app's request, checked: the action – for „setup“ also the radio channel and the stick –, nothing else."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise InstallError("Die Anfrage der App zu Zigbee ist unlesbar.") from exc
    if not isinstance(raw, dict) or raw.get("action") not in ZIGBEE_ACTIONS:
        raise InstallError("Unbekannte Anfrage zu Zigbee.")
    request = {"action": raw["action"]}
    if raw["action"] == "setup":
        channel, port = raw.get("channel"), raw.get("port")
        if isinstance(channel, bool) or not isinstance(channel, int) or channel not in ZIGBEE_CHANNELS:
            raise InstallError("Bitte einen Funkkanal von 11 bis 26 wählen.")
        if port is not None and not (isinstance(port, str) and re.fullmatch(r"[\w.:+-]{1,200}", port)):
            raise InstallError("Unbekannter Zigbee-Stick.")
        request.update(channel=channel, port=port)
    return request


def apt_env() -> dict:
    return {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"}


def debian_arch(system: System) -> str:
    """The system's own architecture (arm64 for Raspberry Pi OS 64-bit) – not the kernel's: a Pi 4 with the 32-bit
    system runs a 64-bit kernel, and the Node.js for arm64 would not run there."""
    return system.run("dpkg", "--print-architecture", check=False).strip()


def package_version(system: System, package: str) -> str | None:
    """Version of an installed Debian package; None if it is not installed."""
    found = re.fullmatch(r"install ok installed (\S+)",
                         system.run("dpkg-query", "-W", "-f=${Status} ${Version}", package, check=False).strip())
    return found.group(1) if found else None


def serial_ports(root: Path) -> list[dict]:
    """USB devices with a serial port, under their lasting names (/dev/serial/by-id) – the Zigbee stick is one."""
    folder = root / "dev/serial/by-id"
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    ports = []
    for name in names:
        if len(ports) == 8 or not re.fullmatch(r"[\w.:+-]{1,200}", name):
            continue
        try:
            device = os.path.basename(os.readlink(folder / name))
        except OSError:
            device = ""
        ports.append({"id": name, "device": device if re.fullmatch(r"tty\w{1,20}", device) else None})
    return ports


def free_mb(path: Path) -> int | None:
    """Free space where path is (or would be), in MB."""
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free >> 20
    except OSError:
        return None


def zigbee_info(base: Path) -> dict:
    """What earlier installations noted in ZIGBEE_DIR (root's own folder)."""
    try:
        info = json.loads((base / ZIGBEE_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def write_zigbee_info(base: Path, info: dict) -> None:
    write_system_file(base / ZIGBEE_INFO, (json.dumps(info, indent=1) + "\n").encode())


def zigbee_state(system: System, root: Path, folder: int | None = None) -> dict:
    """What the app shows: the programs and their versions, Mosquitto, Zigbee2MQTT's service and network, the USB
    devices. folder: the app's data folder – with it, the service's messages go out without the secrets root knows."""
    base = root / ZIGBEE_DIR.lstrip("/")
    info, arch = zigbee_info(base), debian_arch(system)
    return {"arch": arch or None, "arch_ok": arch in NODE_ARCH, "free_mb": free_mb(base),
            "installed": {part: info.get(part) for part in ZIGBEE_PARTS}, "complete": info.get("complete") is True,
            "wanted": {part: value["version"] for part, value in ZIGBEE_PARTS.items()},
            "mosquitto": {"version": package_version(system, "mosquitto"),
                          "active": system.ok("systemctl", "is-active", "--quiet", "mosquitto")},
            "service": zigbee2mqtt_state(system, root, folder), "network": zigbee_network(root),
            "ports": serial_ports(root)}


def zigbee2mqtt_state(system: System, root: Path, folder: int | None) -> dict:
    """Zigbee2MQTT's service as systemd sees it – and while it does not run although it should, its last messages."""
    present = (root / ZIGBEE_SERVICE_UNIT.lstrip("/")).exists()
    props: dict[str, str] = {}
    if present:
        for line in system.run("systemctl", "show", ZIGBEE_SERVICE, "-p", "ActiveState", "-p", "SubState", "-p",
                               "UnitFileState", "-p", "NRestarts", check=False).splitlines():
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    restarts = props.get("NRestarts", "")
    state = {"present": present, "enabled": props.get("UnitFileState") == "enabled",
             "active": props.get("ActiveState") == "active",
             "failed": props.get("ActiveState") == "failed" or props.get("SubState") == "auto-restart",
             "restarts": int(restarts) if restarts.isdigit() else 0, "log": None}
    if present and (state["failed"] or (state["enabled"] and not state["active"])):
        text = system.run("journalctl", "-u", ZIGBEE_SERVICE, "-n", "40", "--no-pager", "-o", "cat", check=False)
        known = known_secrets(root, folder) if folder is not None else set()
        text = scrub(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text), known).strip()
        state["log"] = text[-4000:] or None
    return state


def zigbee_data(root: Path, system: System | None = None) -> int | None:
    """Zigbee2MQTT's data folder, opened without following a link – it belongs to ZIGBEE_USER, so nothing in it is
    trusted. With system: created if missing (0700, owned by ZIGBEE_USER); without: None if it is missing."""
    data = root / ZIGBEE_DATA.lstrip("/")
    if system is not None:
        data.parent.mkdir(parents=True, exist_ok=True)
        return open_folder(data, system, user=ZIGBEE_USER, mode=0o700)
    try:
        return os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None


def zigbee_config(folder: int) -> dict | None:
    """What HoLiTherm needs from configuration.yaml – radio channel, stick, network key –, read line by line: we and
    Zigbee2MQTT write it in YAML's block style. None if there is none."""
    text = read_private(folder, ZIGBEE_CONFIG)
    if text is None:
        return None
    found: dict = {"channel": None, "port": None, "network_key": None}
    section = key = None
    for line in text.splitlines():
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            section, key = content.split(":", 1)[0], None
        elif indent == 2:
            key, _, value = (part.strip() for part in content.partition(":"))
            if section == "advanced" and key == "channel" and value.isdigit():
                found["channel"] = int(value)
            elif section == "serial" and key == "port":
                found["port"] = value.strip("'\"") or None
            elif section == "advanced" and key == "network_key" and value.startswith("["):
                found["network_key"] = [int(n) for n in re.findall(r"\d+", value)]
        elif section == "advanced" and key == "network_key" and (number := re.fullmatch(r"-\s*(\d{1,3})", content)):
            found["network_key"] = (found["network_key"] or []) + [int(number.group(1))]
    key = found["network_key"]
    if key is not None and (len(key) != 16 or any(n > 255 for n in key)):
        found["network_key"] = None
    return found


def zigbee_devices(folder: int) -> int:
    """How many devices Zigbee2MQTT has paired – database.db holds one JSON object per line; the coordinator (the stick
    itself) and groups do not count."""
    count = 0
    for line in (read_private(folder, "database.db") or "").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("ieeeAddr") and entry.get("type") not in ("Coordinator", "Group"):
            count += 1
    return count


def zigbee_network(root: Path) -> dict:
    """Zigbee2MQTT's network as far as the app shows it: set up, radio channel, stick, number of paired devices."""
    folder, config, devices = zigbee_data(root), None, 0
    if folder is not None:
        try:
            config, devices = zigbee_config(folder), zigbee_devices(folder)
        except InstallError:   # a link or something else planted there: shown as not set up
            config, devices = None, 0
        finally:
            os.close(folder)
    port = zigbee2mqtt_unit_port(root)
    if port is None and config and config["port"]:
        port = config["port"].removeprefix("/dev/serial/by-id/") or None
    return {"configured": config is not None, "channel": config["channel"] if config else None, "port": port,
            "devices": devices}


def fetch_file(url: str, dest: Path, report=None, limit: int = 300 << 20) -> None:
    """A program archive to dest over HTTPS (checked against the system's certificates); report(percent) now and then."""
    error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=60) as res, open(dest, "wb") as out:
                total, done, shown = int(res.headers.get("Content-Length") or 0), 0, -10
                while chunk := res.read(1 << 16):
                    done += len(chunk)
                    if done > limit:
                        raise InstallError(f"{url} ist unerwartet groß – abgebrochen.")
                    out.write(chunk)
                    if report and total and done * 100 // total >= shown + 10:
                        shown = done * 100 // total
                        report(min(shown, 100))
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise InstallError(f"{url} gibt es nicht (mehr).") from exc
            error = exc
        except (urllib.error.URLError, OSError) as exc:
            error = exc
        time.sleep(5 * attempt)
    raise InstallError(f"Download von {url.split('/')[2]} fehlgeschlagen ({error}). Hat der Pi Internet?")


def check_digest(path: Path, algorithm: str, expected: str, url: str) -> None:
    """The file is exactly the one this version of HoLiTherm expects – otherwise it is deleted, nothing is installed."""
    digest = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        path.unlink(missing_ok=True)
        raise InstallError(f"Die Datei von {url} ist nicht die erwartete (Prüfsumme stimmt nicht) – aus "
                           "Sicherheitsgründen abgebrochen.")


def unpack(archive: Path, dest: Path) -> tuple[Path, str | None]:
    """Unpack a program archive (tar.gz) into dest: plain files, folders, and links that stay inside the archive's
    one top folder (it is moved elsewhere later). Nothing is written through a link and nothing lands outside.
    Returns that top folder and the Git commit GitHub writes into its archives (None in other ones)."""
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        comment = str((tar.pax_headers or {}).get("comment", ""))
        commit = comment if COMMIT.fullmatch(comment) else None
        for member in tar:
            name = PurePosixPath(member.name)
            if not name.parts or name.is_absolute() or ".." in name.parts:
                raise InstallError(f"Unerwarteter Pfad im Archiv: {member.name}")
            target, parent = dest.joinpath(*name.parts), dest
            for part in name.parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    raise InstallError(f"Unerwarteter Pfad im Archiv (über einen Verweis): {member.name}")
            if member.isdir():
                if target.is_symlink():
                    raise InstallError(f"Unerwarteter Pfad im Archiv (über einen Verweis): {member.name}")
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not (member.isfile() or member.issym()):
                continue   # hard links, devices and the like: nothing a program needs
            if target.is_symlink() or target.exists():
                raise InstallError(f"Doppelter Eintrag im Archiv: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                if PurePosixPath(member.linkname).is_absolute():
                    raise InstallError(f"Unerwarteter Verweis im Archiv: {member.name}")
                os.symlink(member.linkname, target)
                continue
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with tar.extractfile(member) as src, os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    tops = list(dest.iterdir())
    if len(tops) != 1 or tops[0].is_symlink() or not tops[0].is_dir():
        raise InstallError("Das Archiv hat einen unerwarteten Aufbau.")
    inside = os.path.realpath(tops[0])
    for folder, dirs, files in os.walk(tops[0]):   # every link, followed to its end, must stay inside the top folder
        for entry in dirs + files:
            path = os.path.join(folder, entry)
            if os.path.islink(path) and os.path.commonpath([inside, os.path.realpath(path)]) != inside:
                raise InstallError(f"Unerwarteter Verweis im Archiv: {os.path.relpath(path, dest)}")
    return tops[0], commit


def switch_link(link: Path, target: Path) -> None:
    """Point link (next to target) at target in one step."""
    new = link.with_name(f".{link.name}.new")
    if new.is_symlink() or new.exists():
        new.unlink()
    new.symlink_to(target.name)
    os.replace(new, link)


def write_root_secret(path: Path, text: str) -> None:
    """A file only root reads (0600 in a 0700 folder), replaced in one step."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def zigbee_mosquitto(system: System, root: Path, info: dict) -> dict[str, str]:
    """Mosquitto from Debian's archive – only on this Pi, only with a password. Returns the new logins
    {user: password}; they go to the app and to Zigbee2MQTT, nowhere else."""
    if package_version(system, "mosquitto") is None:
        say("• Lade die Paketlisten …")
        system.run(*APT, "update", check=False, env=apt_env())   # a broken other archive must not stop us
        say("• Installiere Mosquitto …")
        try:
            system.run(*APT, "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
                       "install", "-y", "--no-install-recommends", "mosquitto", env=apt_env())
        except CommandError as exc:
            raise InstallError(f"Mosquitto ließ sich nicht installieren ({apt_reason(exc.detail)}).") from None
        info["mosquitto_ours"] = True   # „Entfernen“ takes it away again
    return mosquitto_logins(system, root)


def mosquitto_logins(system: System, root: Path, drop: tuple[Path, ...] = ()) -> dict[str, str]:
    """Mosquitto's settings and password file anew, with new logins {user: password} – they go to the app and to
    Zigbee2MQTT, nowhere else –, and Mosquitto started again. drop: files of FlowThermPi's, removed just before."""
    logins = {user: secrets.token_urlsafe(24) for user in MQTT_USERS.values()}
    passwd = root / MOSQUITTO_PASSWD.lstrip("/")
    tmp = passwd.with_name(f".{passwd.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{user}:{password}\n" for user, password in logins.items()))
        system.run("mosquitto_passwd", "-U", str(tmp))   # the passwords become hashes, in the file itself
        system.lchown(tmp, "mosquitto")   # Mosquitto reads it again without root's rights (after its log rotation)
        os.replace(tmp, passwd)
    finally:
        tmp.unlink(missing_ok=True)
    write_system_file(root / MOSQUITTO_CONF.lstrip("/"),   # after the password file: if that fails, nothing changed
                      MOSQUITTO_TEMPLATE.format(port=MQTT_PORT, passwd=MOSQUITTO_PASSWD).encode())
    for old in drop:   # its listener would take the same port
        old.unlink(missing_ok=True)
    system.run("systemctl", "enable", "mosquitto", check=False)
    try:
        system.run("systemctl", "restart", "mosquitto")
    except CommandError as exc:
        raise InstallError(f"Mosquitto startet nicht ({first_line(exc.detail) or 'ohne Meldung'}).") from None
    return logins


def zigbee_user(system: System, root: Path, base: Path) -> None:
    """The user that builds and runs Zigbee2MQTT – without login, with access to the USB stick (group dialout) – and
    its folders: ZIGBEE_DATA (0700) and base/work, where pnpm keeps what it has loaded."""
    if not system.user_exists(ZIGBEE_USER):
        system.run("useradd", "--system", "--user-group", "--groups", "dialout", "--home-dir", ZIGBEE_DATA,
                   "--no-create-home", "--shell", "/usr/sbin/nologin", ZIGBEE_USER)
        say(f"• Benutzer „{ZIGBEE_USER}“ für Zigbee2MQTT angelegt")
    data = root / ZIGBEE_DATA.lstrip("/")
    data.parent.mkdir(parents=True, exist_ok=True)
    os.close(open_folder(data, system, user=ZIGBEE_USER, mode=0o700))
    work = base / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    system.lchown(work, ZIGBEE_USER)


def zigbee_logins(system: System, root: Path, folder: int, logins: dict[str, str]) -> None:
    """The app's login into its data folder (0600, its own); Zigbee2MQTT's into a file that only root reads – systemd
    hands it over when it starts Zigbee2MQTT."""
    user = MQTT_USERS["app"]
    write_private(folder, ZIGBEE_LOGIN, json.dumps({"host": "127.0.0.1", "port": MQTT_PORT, "user": user,
                                                    "password": logins[user]}) + "\n", system)
    user = MQTT_USERS["zigbee2mqtt"]
    write_root_secret(root / ZIGBEE_ENV.lstrip("/"),
                      ZIGBEE_ENV_TEMPLATE.format(port=MQTT_PORT, user=user, password=logins[user]))


def restart_zigbee2mqtt(system: System) -> None:
    """Zigbee2MQTT, once it is set up, starts again – with its new password and what was installed."""
    if system.ok("systemctl", "is-enabled", "--quiet", ZIGBEE_SERVICE):
        system.run("systemctl", "restart", ZIGBEE_SERVICE, check=False)


def zigbee_env(base: Path) -> dict:
    """For pnpm and Node.js: our Node.js first, everything pnpm keeps in base/work, no colours, never a question."""
    work = base / "work"
    return {"PATH": f"{base / 'node' / 'bin'}:/usr/bin:/bin", "HOME": str(work), "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_DATA_HOME": str(work / "data"), "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_STATE_HOME": str(work / "state"), "CI": "true", "NO_COLOR": "1", "LANG": "C.UTF-8"}


def program_runs(system: System, base: Path, cmd: tuple[str, ...], expected: str) -> bool:
    """cmd (a --version) runs as ZIGBEE_USER and ends with the expected line."""
    code, out = system.run_as(ZIGBEE_USER, cmd, cwd=base, env=zigbee_env(base), timeout=120)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return code == 0 and bool(lines) and lines[-1] == expected


def zigbee_download(base: Path, url: str, name: str, report, digest: tuple[str, str] | None = None):
    """Download (checked if digest is given) and unpack into a new temporary folder in base. Returns that folder –
    the caller removes it – and (the archive's top folder, its Git commit)."""
    tmp = Path(tempfile.mkdtemp(prefix=".download-", dir=base))
    try:
        archive = tmp / name
        fetch_file(url, archive, report)
        if digest:
            check_digest(archive, *digest, url)
        return tmp, unpack(archive, tmp / "files")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def zigbee_node(system: System, base: Path, arch: str, progress) -> None:
    """Node.js from nodejs.org, checked against its checksum, to base/node-v<version>; base/node points at it."""
    version = ZIGBEE_PARTS["node"]["version"]
    folder = base / f"node-v{version}"
    cmd = (str(folder / "bin" / "node"), "--version")
    if not ((folder / "bin" / "node").is_file() and program_runs(system, base, cmd, f"v{version}")):
        url = NODE_URL.format(version=version, arch=NODE_ARCH[arch])
        tmp, (top, _) = zigbee_download(base, url, "node.tar.gz", progress, ("sha256", ZIGBEE_PARTS["node"]["sha256"][arch]))
        try:
            shutil.rmtree(folder, ignore_errors=True)
            os.rename(top, folder)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not program_runs(system, base, cmd, f"v{version}"):
            raise InstallError(f"Node.js {version} läuft auf diesem Pi nicht.")
    switch_link(base / "node", folder)


def zigbee_pnpm(system: System, base: Path, progress) -> None:
    """pnpm, which loads Zigbee2MQTT's building blocks, from npm's registry (checked) to base/pnpm-<version>."""
    version = ZIGBEE_PARTS["pnpm"]["version"]
    folder = base / f"pnpm-{version}"
    cmd = (str(base / "node" / "bin" / "node"), str(folder / "bin" / "pnpm.mjs"), "--version")
    if not ((folder / "bin" / "pnpm.mjs").is_file() and program_runs(system, base, cmd, version)):
        url = PNPM_URL.format(version=version)
        tmp, (top, _) = zigbee_download(base, url, "pnpm.tgz", progress, ("sha512", ZIGBEE_PARTS["pnpm"]["sha512"]))
        try:
            if not (top / "bin" / "pnpm.mjs").is_file():
                raise InstallError("Das Archiv von pnpm hat einen unerwarteten Aufbau.")
            shutil.rmtree(folder, ignore_errors=True)
            os.rename(top, folder)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not program_runs(system, base, cmd, version):
            raise InstallError(f"pnpm {version} läuft auf diesem Pi nicht.")
    switch_link(base / "pnpm", folder)


def zigbee2mqtt_ready(base: Path, info: dict) -> bool:
    """The wanted Zigbee2MQTT is there and built."""
    project = base / "zigbee2mqtt"
    return (info.get("commit") == ZIGBEE_PARTS["zigbee2mqtt"]["commit"] and project.is_symlink()
            and (project / "dist" / ".hash").is_file() and (project / "node_modules").is_dir())


def zigbee2mqtt_fetch(system: System, base: Path, progress) -> Path:
    """Zigbee2MQTT's source at the fixed commit into a new folder base/zigbee2mqtt-<version>-<n>, handed over to
    ZIGBEE_USER, who builds it there. The folder in use stays as it is until the new one is ready."""
    part = ZIGBEE_PARTS["zigbee2mqtt"]
    url = ZIGBEE2MQTT_URL.format(commit=part["commit"])
    numbers = [int(m.group(1)) for p in base.iterdir() if (m := re.fullmatch(r"zigbee2mqtt-.+-(\d+)", p.name))]
    project = base / f"zigbee2mqtt-{part['version']}-{max(numbers, default=0) + 1}"
    tmp, (top, commit) = zigbee_download(base, url, "zigbee2mqtt.tar.gz", progress)
    try:
        if commit != part["commit"]:
            raise InstallError("Das Archiv von GitHub ist nicht die erwartete Version von Zigbee2MQTT – aus "
                               "Sicherheitsgründen abgebrochen.")
        try:
            version = json.loads((top / "package.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            version = None
        if version != part["version"]:
            raise InstallError("Im Archiv von GitHub steckt eine andere Version von Zigbee2MQTT – abgebrochen.")
        os.rename(top, project)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    system.chown_tree(project, ZIGBEE_USER)
    return project


def pnpm_reason(out: str, code: int) -> str:
    """Why pnpm or the build failed, in words."""
    text = out.lower()
    if "enospc" in text or "no space left" in text:
        return "die Speicherkarte ist voll"
    if "heap out of memory" in text or "enomem" in text or code == 134:
        return "zu wenig Arbeitsspeicher"
    if code == 124:
        return "es hat zu lange gedauert"
    if re.search(r"enotfound|eai_again|econnreset|etimedout|econnrefused|socket hang up", text):
        return "keine Verbindung zum Internet – bitte später noch einmal versuchen"
    return f"Fehlercode {code}"


def run_pnpm(system: System, base: Path, project: Path, *args: str, timeout: float, failed: str) -> None:
    cmd = (str(base / "node" / "bin" / "node"), str(base / "pnpm" / "bin" / "pnpm.mjs"), *args)
    code, out = system.run_as(ZIGBEE_USER, cmd, cwd=project, env=zigbee_env(base), timeout=timeout)
    lines = [line.rstrip() for line in out.splitlines() if line.strip()]
    for line in lines[-20:]:
        say(f"  {line}")
    if code:
        raise StepFailed(f"{failed} ({pnpm_reason(out, code)}).", "\n".join(lines[-40:])[-4000:])


def zigbee2mqtt_build(system: System, base: Path, project: Path, phase) -> None:
    """Its building blocks exactly as its lockfile names them, then the build – both as ZIGBEE_USER. Parts that need
    a compiler and are optional (unix-dgram) are left out by pnpm; the serial port comes ready-built."""
    phase("dependencies")
    run_pnpm(system, base, project, "install", "--frozen-lockfile", "--store-dir", str(base / "work" / "store"),
             timeout=40 * 60, failed="Die Bausteine von Zigbee2MQTT ließen sich nicht laden")
    phase("build")
    run_pnpm(system, base, project, "run", "build", timeout=30 * 60, failed="Zigbee2MQTT ließ sich nicht bauen")
    if not ((project / "dist" / ".hash").is_file() and (project / "dist" / "controller.js").is_file()):
        raise InstallError("Zigbee2MQTT ist nach dem Bauen unvollständig.")


def prune_zigbee(base: Path) -> None:
    """Older versions, and what an interrupted installation left behind."""
    keep = {(base / name).resolve() for name in ("node", "pnpm", "zigbee2mqtt") if (base / name).is_symlink()}
    for path in base.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        if path.name.startswith(".download-") or (re.match(r"(node-v|pnpm-|zigbee2mqtt-)", path.name)
                                                  and path.resolve() not in keep):
            shutil.rmtree(path, ignore_errors=True)


def zigbee_install(system: System, root: Path, folder: int, status: dict, report) -> None:
    """„Installieren“: everything in ZIGBEE_PARTS that is not there yet – also after an update of HoLiTherm that
    brings newer versions. A step that fails leaves what worked before as it was."""
    base = root / ZIGBEE_DIR.lstrip("/")

    def phase(name: str) -> None:
        status.update(phase=name, step=ZIGBEE_STEPS.index(name) + 1, steps=len(ZIGBEE_STEPS), progress=None)
        report()

    def progress(percent: int) -> None:
        status["progress"] = percent
        report()

    phase("check")
    arch = debian_arch(system)
    if arch not in NODE_ARCH:
        raise InstallError("Für Zigbee braucht der Pi ein 64-Bit-System (Raspberry Pi OS 64-bit)"
                           + (f" – hier läuft „{arch}“." if arch else "."))
    free = free_mb(base)
    if free is not None and free < ZIGBEE_FREE_MB:
        raise InstallError(f"Auf der Speicherkarte sind nur noch {free} MB frei – die Zigbee-Programme brauchen etwa "
                           f"{ZIGBEE_FREE_MB} MB.")
    os.chmod(base, 0o755)
    info = zigbee_info(base)

    phase("mosquitto")
    logins = zigbee_mosquitto(system, root, info)
    write_zigbee_info(base, info)
    zigbee_user(system, root, base)
    zigbee_logins(system, root, folder, logins)
    restart_zigbee2mqtt(system)   # it has to log in with its new password
    say("• Mosquitto läuft (nur auf diesem Pi, mit Passwort)")

    phase("node")
    say(f"• Node.js {ZIGBEE_PARTS['node']['version']} …")
    zigbee_node(system, base, arch, progress)
    info["node"] = ZIGBEE_PARTS["node"]["version"]
    write_zigbee_info(base, info)

    phase("pnpm")
    say(f"• pnpm {ZIGBEE_PARTS['pnpm']['version']} …")
    zigbee_pnpm(system, base, progress)
    info["pnpm"] = ZIGBEE_PARTS["pnpm"]["version"]
    write_zigbee_info(base, info)

    part = ZIGBEE_PARTS["zigbee2mqtt"]
    if zigbee2mqtt_ready(base, info):
        say(f"• Zigbee2MQTT {part['version']} ist schon da")
    else:
        phase("zigbee2mqtt")
        say(f"• Zigbee2MQTT {part['version']} …")
        project = zigbee2mqtt_fetch(system, base, progress)
        try:
            zigbee2mqtt_build(system, base, project, phase)
        except BaseException:
            shutil.rmtree(project, ignore_errors=True)
            raise
        switch_link(base / "zigbee2mqtt", project)
        info.update(zigbee2mqtt=part["version"], commit=part["commit"])

    phase("finish")
    info.update(complete=True, arch=arch, installed=now_text())
    write_zigbee_info(base, info)
    prune_zigbee(base)
    restart_zigbee2mqtt(system)
    status["message"] = "Die Zigbee-Programme sind installiert."


def zigbee_remove(system: System, root: Path, folder: int | None, keep_data: bool = True) -> str:
    """„Entfernen“: the programs, the logins and Mosquitto – if it came with them; one that was there before only
    loses our settings. Zigbee2MQTT's data (network key, paired thermostats) and its user stay unless keep_data is
    False: after a new installation the thermostats are still paired."""
    base = root / ZIGBEE_DIR.lstrip("/")
    info = zigbee_info(base)
    system.run("systemctl", "disable", "--now", ZIGBEE_SERVICE, check=False)
    unit = root / ZIGBEE_SERVICE_UNIT.lstrip("/")
    if unit.exists():
        unit.unlink()
        system.run("systemctl", "daemon-reload", check=False)
    shutil.rmtree(base, ignore_errors=True)
    for name in (ZIGBEE_ENV, MOSQUITTO_CONF, MOSQUITTO_PASSWD):
        (root / name.lstrip("/")).unlink(missing_ok=True)
    if folder is not None:
        try:
            os.unlink(ZIGBEE_LOGIN, dir_fd=folder)
        except FileNotFoundError:
            pass
    if package_version(system, "mosquitto"):
        if info.get("mosquitto_ours"):
            say("• Entferne Mosquitto …")
            system.run(*APT, "purge", "-y", "mosquitto", check=False, env=apt_env())
        else:
            system.run("systemctl", "restart", "mosquitto", check=False)
    if not keep_data:
        shutil.rmtree(root / ZIGBEE_DATA.lstrip("/"), ignore_errors=True)
        system.run("userdel", ZIGBEE_USER, check=False)
    return "Die Zigbee-Programme sind entfernt."


def zigbee_stick(port: str) -> str:
    """The stick's lines in configuration.yaml: SONOFF's Dongle Plus MG24 (chip EFR32MG24, firmware EmberZNet) speaks
    „ember“ at 115200 baud, without hardware flow control. Other sticks are left to Zigbee2MQTT's own detection."""
    if re.search(r"mg24|efr32", port, re.I):
        return "  adapter: ember\n  rtscts: false\n  baudrate: 115200\n"
    return ""


def zigbee_port(root: Path, wanted: str | None) -> str:
    """The stick to use (its name under /dev/serial/by-id): the one the app named – it must be plugged in – or the only
    USB device with a serial port."""
    ports = [port["id"] for port in serial_ports(root)]
    if wanted is not None:
        if wanted not in ports:
            raise InstallError("Der gewählte Zigbee-Stick steckt nicht (mehr) am Pi.")
        return wanted
    if len(ports) == 1:
        return ports[0]
    raise InstallError("Bitte den Zigbee-Stick am Pi einstecken." if not ports else
                       "Am Pi stecken mehrere USB-Geräte – bitte den Zigbee-Stick wählen.")


def zigbee2mqtt_unit_port(root: Path, unit: str = ZIGBEE_SERVICE_UNIT) -> str | None:
    """The stick Zigbee2MQTT's service uses (its name under /dev/serial/by-id); None if it is not set up."""
    try:
        text = (root / unit.lstrip("/")).read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"^Environment=ZIGBEE2MQTT_CONFIG_SERIAL_PORT=/dev/serial/by-id/([\w.:+-]{1,200})$", text, re.M)
    return found.group(1) if found else None


def write_zigbee2mqtt_unit(root: Path, port: str) -> bool:
    """Zigbee2MQTT's service, with this stick (the setting from the service wins over the one in configuration.yaml).
    True if the unit changed."""
    path = root / ZIGBEE_SERVICE_UNIT.lstrip("/")
    text = ZIGBEE2MQTT_UNIT_TEMPLATE.format(user=ZIGBEE_USER, base=ZIGBEE_DIR, data=ZIGBEE_DATA, env=ZIGBEE_ENV,
                                            port=f"/dev/serial/by-id/{port}")
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    write_system_file(path, text.encode())
    return True


def refresh_zigbee2mqtt_unit(root: Path) -> bool:
    """At an update of HoLiTherm: Zigbee2MQTT's service – if it is set up – from this version's template."""
    port = zigbee2mqtt_unit_port(root)
    return port is not None and write_zigbee2mqtt_unit(root, port)


def zigbee_setup(system: System, root: Path, request: dict) -> str:
    """„Zigbee einrichten“: Zigbee2MQTT's settings – radio channel, stick, a network of its own with a random key – and
    its service, started now and at every start of the Pi. Again with the same channel, the network and the paired
    devices stay (another stick, a broken service). Another channel means a new network: only while no device is
    paired, as they would all have to be paired again."""
    base = root / ZIGBEE_DIR.lstrip("/")
    info = zigbee_info(base)
    if not (info.get("complete") is True and zigbee2mqtt_ready(base, info)):
        raise InstallError("Bitte zuerst die Zigbee-Programme installieren.")
    if not (root / ZIGBEE_ENV.lstrip("/")).is_file():
        raise InstallError("Die Anmeldung von Zigbee2MQTT bei Mosquitto fehlt – bitte die Zigbee-Programme noch einmal "
                           "installieren.")
    channel, port = request["channel"], zigbee_port(root, request.get("port"))
    folder = zigbee_data(root, system)
    try:
        config = zigbee_config(folder)
        if config is not None and config["channel"] == channel:
            say(f"• Funkkanal {channel} bleibt – das Zigbee-Netz und die angelernten Geräte auch")
        else:
            devices = zigbee_devices(folder) if config is not None else 0
            if devices:
                raise InstallError(f"Den Funkkanal kann man nur wechseln, solange kein Gerät angelernt ist – sonst "
                                   f"müsste man alle neu anlernen. Angelernt: {devices}. Bitte zuerst alle entfernen.")
            system.run("systemctl", "stop", ZIGBEE_SERVICE, check=False)   # before its files go
            for name in (*ZIGBEE_NETWORK, ZIGBEE_CONFIG):
                try:
                    os.unlink(name, dir_fd=folder)
                except FileNotFoundError:
                    pass
            write_owned(folder, ZIGBEE_CONFIG, ZIGBEE2MQTT_CONFIG_TEMPLATE.format(
                mqtt_port=MQTT_PORT, port=f"/dev/serial/by-id/{port}", stick=zigbee_stick(port), channel=channel),
                system, ZIGBEE_USER)
            say(f"• Neues Zigbee-Netz auf Funkkanal {channel} – den Schlüssel würfelt Zigbee2MQTT beim ersten Start")
    finally:
        os.close(folder)
    write_zigbee2mqtt_unit(root, port)
    say(f"• Dienst {ZIGBEE_SERVICE} mit dem Stick {port}")
    system.run("systemctl", "daemon-reload")
    system.run("systemctl", "reset-failed", ZIGBEE_SERVICE, check=False)
    system.run("systemctl", "enable", ZIGBEE_SERVICE)
    try:
        system.run("systemctl", "restart", ZIGBEE_SERVICE)
    except CommandError as exc:
        raise InstallError(f"Zigbee2MQTT startet nicht ({first_line(exc.detail) or 'ohne Meldung'}).") from None
    return f"Zigbee2MQTT ist eingerichtet (Funkkanal {channel}) und startet."


def zigbee_switch(system: System, root: Path, on: bool) -> str:
    """„Starten“ and „Anhalten“: Zigbee2MQTT's service – also for the next start of the Pi."""
    if not (root / ZIGBEE_SERVICE_UNIT.lstrip("/")).exists():
        raise InstallError("Zigbee2MQTT ist noch nicht eingerichtet.")
    if on:
        system.run("systemctl", "reset-failed", ZIGBEE_SERVICE, check=False)   # after too many failed starts
        system.run("systemctl", "enable", "--now", ZIGBEE_SERVICE)
        return "Zigbee2MQTT startet."
    system.run("systemctl", "disable", "--now", ZIGBEE_SERVICE)
    return "Zigbee2MQTT ist angehalten."


def zigbee_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by holitherm-zigbee.service when the app has placed a request. Progress and result go to
    zigbee-status.json – at the end always with the state of the programs."""
    root = Path(args.root)
    data_dir = root / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            text = read_private(folder, ZIGBEE_REQUEST)
        finally:
            try:
                os.unlink(ZIGBEE_REQUEST, dir_fd=folder)   # first: while it exists, the path unit starts us again
            except FileNotFoundError:
                pass
        if text is None:
            say("Keine Anfrage der App zu Zigbee – nichts zu tun.")
            return
        status = {"id": request_id(text), "action": None, "state": "running", "phase": None, "step": None,
                  "steps": None, "progress": None, "started": now_text(), "at": now_text()}

        def report() -> None:
            status["at"] = now_text()
            write_private(folder, ZIGBEE_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)

        try:
            request = zigbee_request(text)
            status["action"] = action = request["action"]
            if action != "status":
                report()   # the app shows at once that it has begun
                with install_lock(root / ZIGBEE_DIR.lstrip("/")):
                    if action == "install":
                        zigbee_install(system, root, folder, status, report)
                    elif action == "remove":
                        status["phase"] = "remove"
                        report()
                        status["message"] = zigbee_remove(system, root, folder)
                    elif action == "setup":
                        status["message"] = zigbee_setup(system, root, request)
                    else:
                        status["message"] = zigbee_switch(system, root, action == "start")
                say(f"✓ {status['message']}")
            status["state"] = "done"
        except InstallError as exc:   # reported to the app; not an error of the service itself
            status.update(state="failed", message=str(exc), details=getattr(exc, "details", "") or None)
            say(f"✗ {exc}")
        finally:
            if status["state"] == "running":   # an error in this script: the app must not wait for ever
                status.update(state="failed", message=f"Unerwarteter Fehler – Meldungen: journalctl -u {NAME}-zigbee")
            status.update(phase=None, step=None, progress=None, finished=now_text())
            try:
                status["zigbee"] = zigbee_state(system, root, folder)
            finally:
                report()
    finally:
        os.close(folder)


# ---------------------------------------------------------------------- „Protokoll speichern“
# The app may not read the system's journal: it places {data}/logs-request.json and holitherm-logs.path runs
# „install.py --logs“ as root. That collects the journal of the last week (HoLiTherm's services, Zigbee2MQTT,
# Mosquitto, the network, Tailscale, the kernel) and the Pi's state – voltage, temperature, memory, SD card,
# restarts – and writes it to {data}/system-log.txt, readable only by the app. Every secret root can find (the
# heaters' tokens, WLAN passwords, the MQTT logins, the app's password) and everything that looks like one is
# replaced by ••• before the file is written.
def logs_request(text: str) -> None:
    """The app's request, checked: it can only ask for the one thing."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise InstallError("Die Anfrage der App nach den Systemprotokollen ist unlesbar.") from exc
    if not isinstance(raw, dict) or raw.get("action") != "collect":
        raise InstallError("Unbekannte Anfrage nach den Systemprotokollen.")


def scrub(text: str, known: set[str]) -> str:
    """text without the known secrets and without anything that looks like one."""
    if known:
        text = re.compile("|".join(map(re.escape, sorted(known, key=len, reverse=True)))).sub(HIDDEN, text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def known_secrets(root: Path, folder: int) -> set[str]:
    """Values that must not leave the Pi: the heaters' tokens, the WLAN passwords, the MQTT logins, the app's password,
    Zigbee's network key. Files in the data folders are read without following a link (read_private)."""
    found: set[str] = set()

    def take(value) -> None:
        value = str(value or "").strip().strip("\"'")
        if len(value) >= 6:   # shorter ones would blank out ordinary words
            found.add(value)

    for line in (read_private(folder, "heizungen.conf") or "").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) == 2:
            take(parts[1])
    try:
        login = json.loads(read_private(folder, ZIGBEE_LOGIN) or "{}")
        take(login.get("password") if isinstance(login, dict) else None)
    except ValueError:
        pass
    zigbee = zigbee_data(root)   # the network key, in the forms it could show up in
    if zigbee is not None:
        try:
            key = (zigbee_config(zigbee) or {}).get("network_key")
        except InstallError:
            key = None
        finally:
            os.close(zigbee)
        if key:
            for form in (",".join(map(str, key)), ", ".join(map(str, key)), bytes(key).hex(), bytes(key).hex().upper()):
                take(form)
    files = [(root / ZIGBEE_ENV.lstrip("/"), r"^ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD=(.+)$"),
             (root / "etc/default" / NAME, r"^\s*(?:export\s+)?HEIZAPP_PASSWORD=(.+)$")]
    for folder_name in ("etc/NetworkManager/system-connections", "etc/wpa_supplicant"):
        try:
            names = sorted(os.listdir(root / folder_name))
        except OSError:
            continue
        files += [(root / folder_name / name, r"^\s*(?:psk|password|wep-key[0-3]|leap-password|private-key-password|"
                   r"pin)\s*=\s*(.+)$") for name in names]
    for path, pattern in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(pattern, text, re.M | re.I):
            take(match.group(1))
    return found


def throttled(text: str) -> str:
    """„throttled=0x50005“ from vcgencmd in words – under-voltage is the Pi's most common trouble."""
    found = re.search(r"throttled=(0x[0-9a-fA-F]+)", text)
    if not found:
        return first_line(text) or "?"
    value = int(found.group(1), 16)
    flags = [word for bit, word in THROTTLED.items() if value >> bit & 1]
    return f"{found.group(1)} – " + (", ".join(flags) if flags else "keine Unterspannung, nicht gedrosselt")


def system_overview(system: System, root: Path) -> list[str]:
    """The Pi in a few lines: model, system, running time, memory, SD card, temperature, voltage, versions."""
    def read(name: str) -> str:
        try:
            return (root / name).read_bytes()[:4096].decode("utf-8", "replace").strip("\x00 \n")
        except OSError:
            return ""

    def call(*cmd: str) -> str:
        try:
            code, out, err = system.call(*cmd, timeout=10)
        except ProgramMissing:
            return ""
        return out if code == 0 else ""

    lines = [f"Gerät:            {read('proc/device-tree/model') or '?'} · {os_release(root).get('PRETTY_NAME') or '?'} · "
             f"Kernel {platform.release()} · {debian_arch(system) or platform.machine()}"]
    uptime = read("proc/uptime").split()
    if uptime:
        minutes = int(float(uptime[0]) // 60)
        lines.append(f"Läuft seit:       {minutes // 1440} Tg. {minutes % 1440 // 60} Std. {minutes % 60} Min. "
                     f"(Last {' '.join(read('proc/loadavg').split()[:3]) or '?'})")
    memory = {key: int(value.split()[0]) >> 10 for key, value in
              (line.split(":", 1) for line in read("proc/meminfo").splitlines() if ":" in line)
              if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree") and value.split()[0].isdigit()}
    if memory:
        lines.append(f"Speicher:         {memory.get('MemAvailable', '?')} von {memory.get('MemTotal', '?')} MB frei, "
                     f"Auslagerung {memory.get('SwapFree', '?')} von {memory.get('SwapTotal', '?')} MB frei")
    try:
        disk = shutil.disk_usage(root)
        lines.append(f"SD-Karte:         {disk.free >> 20} von {disk.total >> 20} MB frei")
    except OSError:
        pass
    temp = read("sys/class/thermal/thermal_zone0/temp")
    if temp.isdigit():
        lines.append(f"CPU-Temperatur:   {int(temp) / 1000:.1f} °C".replace(".", ","))
    volts = call("vcgencmd", "get_throttled")
    lines.append(f"Spannung:         {throttled(volts) if volts else 'vcgencmd nicht verfügbar'}")
    lines.append(f"Journal:          {'dauerhaft gespeichert' if (root / 'var/log/journal').is_dir() else 'nur seit dem letzten Start (geht beim Ausschalten verloren)'}")
    app = release_info(root / APP_DIR.lstrip("/") / "current")
    lines.append(f"HoLiTherm:        {app.get('version') or '?'} ({str(app.get('commit') or '?')[:7]}), installiert {app.get('installed') or '?'}")
    zigbee = zigbee_info(root / ZIGBEE_DIR.lstrip("/"))
    lines.append("Zigbee-Programme: " + (", ".join(f"{part} {zigbee[part]}" for part in ZIGBEE_PARTS if zigbee.get(part))
                                         or "nicht installiert"))
    lines.append(f"Mosquitto:        {package_version(system, 'mosquitto') or 'nicht installiert'}")
    lines.append(f"Tailscale:        {first_line(call('tailscale', 'version')) or 'nicht installiert'}")
    lines.append(f"Python:           {platform.python_version()}")
    return lines


def folder_listing(folder: int) -> str:
    """Names, sizes and times in the data folder (and data/, data/logs/) – never a file's content, never a link
    followed."""
    rows: list[str] = []

    def walk(fd: int, prefix: str, depth: int) -> None:
        with os.scandir(fd) as entries:
            items = sorted(entries, key=lambda entry: entry.name)
        for entry in items[:100]:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = "/" if stat.S_ISDIR(info.st_mode) else " → Verweis" if stat.S_ISLNK(info.st_mode) else ""
            rows.append(f"{prefix}{entry.name}{kind}  {info.st_size} B  {stat.filemode(info.st_mode)}  "
                        f"{datetime.fromtimestamp(info.st_mtime):%Y-%m-%d %H:%M:%S}")
            if kind == "/" and depth < 2 and entry.name in ("data", "logs"):
                try:
                    sub = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError:
                    continue
                try:
                    walk(sub, prefix + "  ", depth + 1)
                finally:
                    os.close(sub)

    walk(folder, "", 0)
    return "\n".join(rows)


def usb_devices(root: Path) -> str:
    """USB devices as the kernel lists them (maker and product, no serial numbers) and the serial ports."""
    base, rows = root / "sys/bus/usb/devices", []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        names = []

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()[:80]
        except OSError:
            return ""

    for name in names[:60]:
        vendor = read(base / name / "idVendor")
        if vendor:
            rows.append(f"{name}: {vendor}:{read(base / name / 'idProduct')} {read(base / name / 'manufacturer')} "
                        f"{read(base / name / 'product')}".rstrip())
    rows += [f"Serieller Anschluss: {port['id']} → {port['device'] or '?'}" for port in serial_ports(root)]
    return "\n".join(rows)


def system_report(system: System, root: Path, folder: int) -> str:
    """The Pi's system logs and state for „Protokoll speichern“ – without any secret (scrub)."""
    now = datetime.now()
    since = (now - timedelta(days=LOGS_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"HoLiTherm – Systemprotokolle vom {now:%d.%m.%Y %H:%M:%S}, Rechner {socket.gethostname()}, "
             f"Gerätenummer {device_number(root) or '?'}\n"
             f"Journal der letzten {LOGS_DAYS} Tage (seit {since}), von jedem Dienst die neuesten Zeilen.\n"
             "Passwörter, Tokens und Anmelde-Links sind durch ••• ersetzt.\n"]

    def section(title: str, text: str) -> None:
        parts.append(f"\n===== {title} =====\n{text.rstrip() or '(nichts)'}\n")

    def command(title: str, *cmd: str, tail: int = 0, timeout: float = 30) -> None:
        try:
            code, out, err = system.call(*cmd, timeout=timeout)
        except ProgramMissing:
            section(title, f"({cmd[0]} gibt es auf diesem Pi nicht)")
            return
        text = out if out.strip() else err
        if tail:
            lines = text.splitlines()
            text = "\n".join(lines[:1] + (["…"] if len(lines) > tail + 1 else []) + lines[1:][-tail:])
        if code:
            text += f"\n(Fehlercode {code}{': ' + first_line(err) if out.strip() and err.strip() else ''})"
        section(title, text)

    section("Überblick", "\n".join(system_overview(system, root)))
    command("Uhrzeit (timedatectl)", "timedatectl", "show")
    command("Dienste", "systemctl", "show", "--no-pager", "-p",
            "Id,LoadState,ActiveState,SubState,Result,NRestarts,ActiveEnterTimestamp,ExecMainStatus",
            NAME, *(f"{NAME}-{kind}.path" for kind in ("update", "wifi", "remote", "zigbee", "logs")), ZIGBEE_SERVICE,
            "mosquitto", "tailscaled", "NetworkManager", "systemd-timesyncd")
    command("Fehlgeschlagene Dienste", "systemctl", "--failed", "--no-pager", "--no-legend", "--plain")
    command("Netz: Adressen", "ip", "-brief", "address")
    command("Netz: Wege", "ip", "route")
    command("Netz: Geräte", "nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
    command("WLAN in Reichweite", "nmcli", "-f", "IN-USE,SSID,CHAN,SIGNAL,SECURITY", "device", "wifi", "list",
            "--rescan", "no")
    section("USB-Geräte", usb_devices(root))
    section(f"Datenordner {DATA_DIR}", folder_listing(folder))
    command("Systemstarts", "journalctl", "--list-boots", "--no-pager", tail=20)
    journal = ("-o", "short-iso", "--no-pager", "--no-hostname", f"--since={since}")
    for unit, lines in LOGS_UNITS:
        command(f"Journal: {unit}", "journalctl", "-u", unit, "-n", str(lines), *journal)
    command("Journal: Kernel", "journalctl", "-k", "-n", str(LOGS_KERNEL), *journal)
    text = scrub("".join(parts), known_secrets(root, folder))
    data = text.encode("utf-8")
    if len(data) > LOGS_MAX:   # the newest at the end matter most: keep the start (overview) and cut the middle
        head, rest = data[:LOGS_MAX // 4], data[-(LOGS_MAX - LOGS_MAX // 4):]
        text = (head.decode("utf-8", "ignore") + "\n\n… (zu lang – hier gekürzt) …\n\n" + rest.decode("utf-8", "ignore"))
    return text


def logs_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by holitherm-logs.service when the app asks for the system logs („Protokoll speichern“). Writes them,
    without any secret, to system-log.txt in the data folder (only the app reads it) and the result to
    logs-status.json."""
    root = Path(args.root)
    data_dir = root / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            text = read_private(folder, LOGS_REQUEST)
        finally:
            try:
                os.unlink(LOGS_REQUEST, dir_fd=folder)   # first: while it exists, the path unit starts us again
            except FileNotFoundError:
                pass
        if text is None:
            say("Keine Anfrage der App nach den Systemprotokollen – nichts zu tun.")
            return
        status = {"id": request_id(text), "state": "running", "started": now_text()}
        try:
            logs_request(text)
            write_private(folder, LOGS_STATUS, json.dumps(status) + "\n", system)
            report = system_report(system, root, folder)
            write_private(folder, LOGS_FILE, report, system)
            status.update(state="done", size=len(report.encode("utf-8")))
            say(f"✓ Systemprotokolle gesammelt ({len(report.encode('utf-8')) >> 10} KB)")
        except InstallError as exc:   # reported to the app
            status.update(state="failed", message=str(exc))
            say(f"✗ {exc}")
        finally:
            if status["state"] == "running":   # an error in this script: the app must not wait for ever
                status.update(state="failed", message=f"Unerwarteter Fehler – Meldungen: journalctl -u {NAME}-logs")
            status["finished"] = now_text()
            write_private(folder, LOGS_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)
    finally:
        os.close(folder)


def reset_pin(args: argparse.Namespace, system: System) -> None:
    system.run("systemctl", "stop", NAME, check=False)
    try:
        system.run("runuser", "-u", app_user(system), "--", sys.executable, f"{APP_DIR}/current/run.py",
                   "--reset-pin", "--data", f"{DATA_DIR}/data/data.json")
    finally:
        system.run("systemctl", "start", NAME, check=False)
    say("✓ PIN gelöscht – die Einrichtung ist wieder ohne PIN zugänglich.")


def uninstall(args: argparse.Namespace, system: System) -> None:
    root = Path(args.root)
    move_old_installation(system, root)   # FlowThermPi goes the same way
    with install_lock(root / APP_DIR.lstrip("/")):
        system.run("systemctl", "disable", "--now", f"{NAME}-update.path", check=False)
        system.run("systemctl", "disable", "--now", f"{NAME}-wifi.path", check=False)
        system.run("systemctl", "disable", "--now", f"{NAME}-remote.path", check=False)
        system.run("systemctl", "disable", "--now", f"{NAME}-zigbee.path", check=False)
        system.run("systemctl", "disable", "--now", f"{NAME}-logs.path", check=False)
        system.run("systemctl", "disable", "--now", NAME, check=False)
        for unit in (UNIT, UPDATE_UNIT, UPDATE_PATH, WIFI_UNIT, WIFI_PATH, REMOTE_UNIT, REMOTE_PATH, ZIGBEE_UNIT, ZIGBEE_PATH,
                     LOGS_UNIT, LOGS_PATH):
            path = root / unit.lstrip("/")
            if path.exists():
                path.unlink()
        system.run("systemctl", "daemon-reload", check=False)
        left = [root / name.lstrip("/") for name in (ZIGBEE_DIR, MOSQUITTO_CONF, ZIGBEE_ENV, ZIGBEE_SERVICE_UNIT)]
        if args.purge:   # also what „Entfernen“ in the app kept: the paired thermostats and their user
            left.append(root / ZIGBEE_DATA.lstrip("/"))
        if any(path.exists() for path in left) or (args.purge and system.user_exists(ZIGBEE_USER)):
            zigbee_remove(system, root, None, keep_data=not args.purge)
            say(f"✓ Zigbee-Programme entfernt ({ZIGBEE_DIR})." if args.purge else
                f"✓ Zigbee-Programme entfernt ({ZIGBEE_DIR}) – die angelernten Thermostate bleiben in {ZIGBEE_DATA}.")
        shutil.rmtree(root / APP_DIR.lstrip("/"), ignore_errors=True)
    say(f"✓ HoLiTherm entfernt ({APP_DIR}).")
    if system.live and shutil.which("tailscale"):
        say("  Tailscale (Fernwartung) bleibt installiert. Abmelden: sudo tailscale logout – "
            "entfernen: sudo apt remove tailscale")
    if args.purge:
        shutil.rmtree(root / DATA_DIR.lstrip("/"), ignore_errors=True)
        system.run("userdel", app_user(system), check=False)
        say(f"✓ Daten gelöscht ({DATA_DIR}, auch die Tokens).")
    else:
        say(f"  Heizungen, Tokens und Einstellungen bleiben in {DATA_DIR} (alles löschen: --uninstall --purge).")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="install.py",
                                     description="HoLiTherm auf einem Raspberry Pi einrichten oder aktualisieren.")
    parser.add_argument("--port", type=int, help="Port der App (Standard 80, bei Updates der bisherige)")
    parser.add_argument("--ref", default="main", help="Git-Tag oder Branch (Standard: main)")
    parser.add_argument("--import", dest="import_dir", metavar="ORDNER",
                        help="heizungen.conf und data.json aus diesem Ordner übernehmen")
    parser.add_argument("--source", metavar="ORDNER", help="aus einem lokalen Ordner statt von GitHub installieren")
    parser.add_argument("--timezone", default=DEFAULT_TZ,
                        help=f"Zeitzone, falls noch die Werkseinstellung gilt (Standard {DEFAULT_TZ})")
    parser.add_argument("--reset-pin", action="store_true", help="PIN der Einrichtung löschen")
    parser.add_argument("--uninstall", action="store_true", help="Dienst und Programm entfernen")
    parser.add_argument("--purge", action="store_true", help="mit --uninstall: auch alle Daten löschen")
    parser.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)  # holitherm-update.service
    parser.add_argument("--wifi", action="store_true", help=argparse.SUPPRESS)  # holitherm-wifi.service
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)  # holitherm-remote.service
    parser.add_argument("--zigbee", action="store_true", help=argparse.SUPPRESS)  # holitherm-zigbee.service
    parser.add_argument("--logs", action="store_true", help=argparse.SUPPRESS)  # holitherm-logs.service
    parser.add_argument("--finish-move", action="store_true", help=argparse.SUPPRESS)  # holitherm-umzug.service
    parser.add_argument("--commit", help=argparse.SUPPRESS)  # with --source: Git commit of that folder
    parser.add_argument("--root", default="/", help=argparse.SUPPRESS)  # tests: work inside this folder only
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, system: System | None = None) -> int:
    args = parse_args(argv)
    if system is None:
        system = DryRun() if args.root != "/" else System()
    try:
        if sys.version_info < MIN_PYTHON:
            raise InstallError(f"Python {'.'.join(map(str, MIN_PYTHON))} oder neuer nötig, gefunden "
                               f"{platform.python_version()} – bitte ein aktuelles Raspberry Pi OS verwenden.")
        if system.live:
            check_host()
        if args.from_app:
            update_from_app(args, system)
        elif args.wifi:
            wifi_from_app(args, system)
        elif args.remote:
            remote_from_app(args, system)
        elif args.zigbee:
            zigbee_from_app(args, system)
        elif args.logs:
            logs_from_app(args, system)
        elif args.finish_move:
            finish_move(args, system)
        elif args.uninstall:
            uninstall(args, system)
        elif args.reset_pin:
            reset_pin(args, system)
        else:
            install(args, system)
        return 0
    except InstallError as exc:
        say(f"\n✗ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
