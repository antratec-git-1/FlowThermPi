"""PIN for the setup area ("Einrichtung").

The simple view for residents needs no login: reading the state, warmer/colder, on/off and
"back to the plan" always work. Everything that changes the setup (heaters, rooms, plans,
calibration, clocks, the PIN itself) needs an unlocked session as soon as a PIN is set.

The PIN is stored as a salted PBKDF2 hash in data.json (key "settings"). Sessions live only in
memory, so a restart of the app locks the setup again. After FREE_ATTEMPTS wrong PINs the app
pauses further attempts, doubling the pause with every further wrong PIN.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Callable

from .service import ActionError
from .store import DataStore

COOKIE = "heizung_einrichtung"
PIN_PATTERN = re.compile(r"\d{4,8}")
ITERATIONS = 200_000
SESSION_TTL = 30 * 60      # seconds without a setup action until the PIN is asked again
FREE_ATTEMPTS = 5          # wrong PINs before the pause starts
FIRST_PAUSE = 60           # seconds; doubles with every further wrong PIN
MAX_PAUSE = 15 * 60


def _hash(pin: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations).hex()


class SetupAccess:
    def __init__(self, store: DataStore, clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, float] = {}   # token -> expiry (clock time)
        self._failures = 0
        self._paused_until = 0.0

    # ------------------------------------------------------------ PIN
    def _record(self) -> dict | None:
        return (self.store.data.get("settings") or {}).get("pin")

    @property
    def pin_set(self) -> bool:
        return bool(self._record())

    def set_pin(self, pin, token: str | None) -> str:
        """Set or change the PIN. Other devices have to enter the new PIN; the caller stays
        unlocked. Returns the caller's session token."""
        pin = str(pin or "").strip()
        if not PIN_PATTERN.fullmatch(pin):
            raise ActionError("Die PIN muss aus 4 bis 8 Ziffern bestehen.")
        salt = secrets.token_bytes(16)
        record = {"salt": salt.hex(), "iterations": ITERATIONS, "hash": _hash(pin, salt, ITERATIONS)}
        with self.store.edit() as d:
            d.setdefault("settings", {})["pin"] = record
        with self._lock:
            keep = token if self._valid(token) else None
            self._sessions.clear()
            return self._new_session(keep)

    def remove_pin(self) -> None:
        with self.store.edit() as d:
            (d.get("settings") or {}).pop("pin", None)
        with self._lock:
            self._sessions.clear()

    # ------------------------------------------------------------ sessions
    def unlock(self, pin) -> str:
        """Check the PIN; returns a new session token. Wrong PIN -> 403, too many -> 429."""
        record = self._record()
        if not record:
            return self._new_session(None)
        with self._lock:
            wait = self._paused_until - self.clock()
            if wait > 0:
                raise ActionError(f"Zu viele falsche Versuche – bitte in {int(wait + 0.999)} Sekunden "
                                  "noch einmal versuchen.", 429)
        pin = str(pin or "").strip()
        ok = bool(PIN_PATTERN.fullmatch(pin)) and hmac.compare_digest(
            _hash(pin, bytes.fromhex(record["salt"]), int(record["iterations"])), record["hash"])
        with self._lock:
            if not ok:
                self._failures += 1
                if self._failures >= FREE_ATTEMPTS:
                    pause = min(MAX_PAUSE, FIRST_PAUSE * 2 ** (self._failures - FREE_ATTEMPTS))
                    self._paused_until = self.clock() + pause
                    raise ActionError(f"Falsche PIN. Zu viele Versuche – bitte {pause} Sekunden warten.", 403)
                raise ActionError("Falsche PIN.", 403)
            self._failures, self._paused_until = 0, 0.0
            return self._new_session(None)

    def lock(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def allowed(self, token: str | None, touch: bool = True) -> bool:
        """May this request change the setup? Always, as long as no PIN is set."""
        if not self.pin_set:
            return True
        with self._lock:
            if not self._valid(token):
                return False
            if touch:
                self._sessions[token] = self.clock() + SESSION_TTL
            return True

    def _valid(self, token: str | None) -> bool:
        expiry = self._sessions.get(token or "")
        if expiry is None:
            return False
        if expiry < self.clock():
            self._sessions.pop(token, None)
            return False
        return True

    def _new_session(self, token: str | None) -> str:
        with self._lock:
            now = self.clock()
            for old in [t for t, exp in self._sessions.items() if exp < now]:
                del self._sessions[old]
            token = token or secrets.token_urlsafe(32)
            self._sessions[token] = now + SESSION_TTL
            return token
