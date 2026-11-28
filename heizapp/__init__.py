"""FlowThermPi – lokale Steuerung für Thermotec AeroFlow-Heizungen mit FlexiSmart-Pro-Regler.

Copyright (C) 2026 antratec. Freie Software unter der GNU General Public License,
Version 3 (siehe LICENSE).
"""
import logging

APP_NAME = "FlowThermPi"
__version__ = "0.4.24"
LICENSE = "GPL-3.0"
COPYRIGHT = "© 2026 antratec"
REPO_URL = "https://github.com/antratec-git-1/FlowThermPi"

# Where the entries go is decided by whoever starts the app (run.py: logs.setup). Without that – in the tests – they
# are dropped instead of landing on the console.
logging.getLogger("heizapp").addHandler(logging.NullHandler())
