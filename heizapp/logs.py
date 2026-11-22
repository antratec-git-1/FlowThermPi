"""Protokoll der App: eine Datei im Datenordner, die Bewohner dem Helfer schicken können, wenn etwas schiefgeht.

FlowThermPi schreibt alles Wichtige nach <Ordner von data.json>/logs/flowthermpi.log (auf dem Pi
/var/lib/flowthermpi/data/logs) und wie bisher auf die Konsole, auf dem Pi also ins Journal. Ist die Datei 2 MB groß,
wird sie zu flowthermpi.log.1; neun ältere bleiben, zusammen höchstens etwa 20 MB. Jede Zeile beginnt mit Datum,
Uhrzeit und dem Teil der App, aus dem sie kommt.

Geheimnisse kommen nie hinein: Tokens der Heizungen, Passwörter und Anmelde-Links werden beim Schreiben durch „•••“
ersetzt – die Werte, die die App kennt (keep_secret), und alles, was wie eines aussieht („token=…“, "password": "…",
der Anmelde-Link von Tailscale).

Ausführliches Protokoll (Einrichtung › Über): für 24 Stunden auch jede Abfrage der Heizungen und Fühler und jeden
Aufruf der Seite (Stufe DEBUG) – nur in die Datei, nicht ins Journal. Es endet von selbst (support.py).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

FILE = "flowthermpi.log"
MAX_BYTES = 2 << 20     # one file; a full one becomes flowthermpi.log.1, .1 becomes .2 …
BACKUPS = 9             # … and the oldest of ten goes: at most about 20 MB on the SD card
MARKER = ".running"     # exists while the app runs: left over at the next start, the app did not end cleanly
HIDDEN = "•••"
MIN_SECRET = 6          # shorter values are never replaced – they could be ordinary words or numbers
RECENT = 60             # warnings and errors kept in memory for the overview of „Protokoll speichern“

# What a secret looks like, also where the app does not know its value. install.py has the same list for the system
# logs it collects (it cannot import the app).
_KEYS = r"token|pop|psk|password|passwort|passwd|secret|auth_?key|auth_?url|login_?url|pin"
PATTERNS = (
    # token=… in an address, MQTT_PASSWORD=… in the environment
    (re.compile(rf"(?i)(?<![A-Za-z0-9])({_KEYS})=[^\s&\"',;]+"), rf"\1={HIDDEN}"),
    # "password": "…", "mqtt_password": "…" in JSON
    (re.compile(rf"""(?i)(["'])((?:[A-Za-z0-9]+_)*(?:{_KEYS}))\1(\s*:\s*)(["'])(?:\\.|(?!\4).)*\4"""),
     rf"\1\2\1\3\4{HIDDEN}\4"),
    (re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9+/=._~-]{8,}"), rf"\1 {HIDDEN}"),       # Authorization header
    (re.compile(r"(https://[A-Za-z0-9.-]+(?::\d{1,5})?/a/)[A-Za-z0-9]+"), rf"\1{HIDDEN}"),  # Tailscale's login link
    (re.compile(r"\btskey-[A-Za-z0-9_-]+"), f"tskey-{HIDDEN}"),                            # Tailscale auth key
    (re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]{16,}"), f"data:image/png;base64,{HIDDEN}"),  # login QR code
)

_lock = threading.Lock()
_secrets: set[str] = set()
_known: re.Pattern | None = None          # every known secret, longest first
_recent: deque[str] = deque(maxlen=RECENT)
_folder: Path | None = None               # where the log files are (None: only the console)
_file: logging.handlers.RotatingFileHandler | None = None
log = logging.getLogger(__name__)


def keep_secret(value) -> None:
    """Never write this value into the log (a heater's token, a password)."""
    global _known
    if not isinstance(value, str) or len(value.strip()) < MIN_SECRET:
        return
    value = value.strip()
    with _lock:
        if value in _secrets:
            return
        _secrets.add(value)
        _known = re.compile("|".join(map(re.escape, sorted(_secrets, key=len, reverse=True))))


def redact(text: str) -> str:
    """text without the secrets: known values and what looks like one become •••."""
    known = _known
    if known is not None:
        text = known.sub(HIDDEN, text)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def area(name: str) -> str:
    """The part of the app a logger belongs to: heizapp.service -> service; the app itself (run.py) -> app."""
    return "app" if name in ("heizapp", "root", "__main__") else name.removeprefix("heizapp.")


class Formatter(logging.Formatter):
    """One line per entry: date and time with milliseconds and UTC offset, level, part of the app, message – without
    secrets. short: only the time (the console; on the Pi the journal adds the date itself)."""

    def __init__(self, short: bool = False):
        super().__init__("%(asctime)s %(levelname)s %(area)s: %(message)s" if short
                         else "%(asctime)s %(levelname)-7s %(area)-8s %(message)s")
        self.short = short

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging's name)
        moment = time.localtime(record.created)
        if self.short:
            return time.strftime("%H:%M:%S", moment)
        return f"{time.strftime('%Y-%m-%d %H:%M:%S', moment)}.{int(record.msecs):03d} {time.strftime('%z', moment)}"

    def format(self, record):
        record.area = area(record.name)
        return redact(super().format(record))


class _Recent(logging.Handler):
    """Keeps the latest warnings and errors in memory – the overview shows them first."""

    def emit(self, record):
        try:
            _recent.append(self.format(record))
        except Exception:  # noqa: BLE001 – logging must never fail the app
            self.handleError(record)


def setup(folder: str | os.PathLike | None, console: bool = True) -> Path | None:
    """Logging of the app: to the console (on the Pi: the journal) and into folder/flowthermpi.log. Returns the log
    file – None without folder or if it cannot be written (the app runs anyway)."""
    global _folder, _file
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):   # earlier calls (tests) and basicConfig
        root.removeHandler(handler)
        if getattr(handler, "_flowthermpi", False):
            handler.close()
    handlers: list[logging.Handler] = []
    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.INFO)   # the detailed entries go only into the file
        stream.setFormatter(Formatter(short=True))
        handlers.append(stream)
    recent = _Recent(logging.WARNING)
    recent.setFormatter(Formatter())
    handlers.append(recent)
    _folder, _file, path, problem = None, None, None, None
    if folder is not None:
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            path = Path(folder) / FILE
            _file = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                         encoding="utf-8")
            _file.setFormatter(Formatter())
            handlers.append(_file)
            _folder = Path(folder)
        except OSError as exc:
            path, problem = None, exc
    for handler in handlers:
        handler._flowthermpi = True
        root.addHandler(handler)
    if problem is not None:
        log.warning("Die Protokolldatei in %s lässt sich nicht schreiben (%s) – es gibt nur die Meldungen auf der "
                    "Konsole.", folder, problem.strerror or problem)
    threading.excepthook = _thread_error
    sys.excepthook = _error
    return path


def _thread_error(args) -> None:
    if args.exc_type is SystemExit:
        return
    logging.getLogger("heizapp").error("Unerwarteter Fehler im Hintergrund (%s)",
                                       args.thread.name if args.thread else "?",
                                       exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _error(kind, value, tb) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, tb)
        return
    logging.getLogger("heizapp").critical("Die App ist mit einem Fehler beendet worden", exc_info=(kind, value, tb))


def set_detail(on: bool) -> None:
    """Ausführliches Protokoll: also the DEBUG entries (into the file only)."""
    logging.getLogger("heizapp").setLevel(logging.DEBUG if on else logging.NOTSET)


def detail() -> bool:
    return logging.getLogger("heizapp").level == logging.DEBUG


def files() -> list[Path]:
    """The log files, oldest first."""
    if _folder is None:
        return []
    found = [_folder / f"{FILE}.{n}" for n in range(BACKUPS, 0, -1)] + [_folder / FILE]
    return [path for path in found if path.is_file()]


def read_all(limit: int = 32 << 20) -> str:
    """Everything in the log files, oldest entry first (at most limit bytes: the newest)."""
    handler = _file
    if handler is None:
        return ""
    handler.acquire()   # no change of file while they are read
    try:
        handler.flush()
        chunks, total = [], 0
        for path in reversed(files()):   # newest first, until the limit is reached
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if total + len(data) > limit:
                data = data[len(data) - (limit - total):]
                data = data[data.find(b"\n") + 1:]   # from the start of a line
            chunks.append(data)
            total += len(data)
            if total >= limit:
                break
    finally:
        handler.release()
    return b"".join(reversed(chunks)).decode("utf-8", "replace")


def size() -> int:
    """Bytes in all log files."""
    total = 0
    for path in files():
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def oldest() -> str | None:
    """Date and time of the first entry that is still kept, e.g. "2026-11-01T08:00:01" (the server's local time)."""
    for path in files():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
        except OSError:
            continue
        found = re.match(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", first)
        if found:
            return f"{found.group(1)}T{found.group(2)}"
    return None


def recent() -> list[str]:
    """The latest warnings and errors since the app started, oldest first."""
    return list(_recent)


def mark_running() -> str | None:
    """Notes that the app runs. If the last run did not end cleanly (power cut, crash, killed), returns the time of
    its last entry ("" if unknown) – else None."""
    if _folder is None:
        return None
    marker = _folder / MARKER
    unclean = marker.exists()
    last = ""
    if unclean:
        last = _last_time()
    try:
        marker.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass
    return last if unclean else None


def mark_stopped() -> None:
    if _folder is not None:
        try:
            (_folder / MARKER).unlink()
        except OSError:
            pass


def _last_time() -> str:
    """Time of the newest entry in the log files before this start."""
    for path in reversed(files()):
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - 65536))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            found = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
            if found:
                return found.group(1)
    return ""
