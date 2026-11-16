# FlowThermPi – lokale Steuerung für Thermotec AeroFlow (FlexiSmart Pro)

Kleine Web-App, die Thermotec-Elektroheizungen mit **FlexiSmart-Pro-Regler** direkt im
Heimnetz steuert – ohne Cloud, ohne Konto, ohne Zusatzpakete (nur Python-Standardbibliothek).

Inoffizielles Projekt – kein Produkt von Thermotec und nicht von Thermotec unterstützt.

## Zwei Ansichten: einfach für Bewohner, Einrichtung mit PIN

**Einfache Ansicht** (Startseite) – gedacht für Menschen, die einfach nur heizen möchten:
eine große Kachel pro Raum mit der Raumtemperatur, großen Knöpfen **kälter / wärmer** und dem
Zustand in Worten, z. B. „Nach Plan: 21 °C bis 22 Uhr, danach 17 °C“.

- **wärmer / kälter** im Plan-Betrieb gilt wie an der Heizung selbst nur **bis zum nächsten
  Planwechsel** („Bis 22 Uhr auf 22 °C gestellt, danach wieder nach Plan.“), sonst dauerhaft.
- **Ausschalten** (mit Rückfrage) = nur noch Frostschutz. **Einschalten** stellt wieder her, was
  vorher lief: Heizplan bzw. die vorherige Temperatur; ohne beides 20 °C bzw. der Plan des Raums.
- **Wieder nach Plan** erscheint, wenn ein Raum fest eingestellt ist, aber einen Heizplan hat.
- Hinweise wie „Fenster offen – die Heizung macht eine Pause“ oder „Keine Verbindung zur Heizung“.

**Einrichtung** (Knopf oben rechts): Heizungen, Räume, Heizpläne, Kalibrierung, Uhren – alles
Folgende. Mit **PIN festlegen** (4–8 Ziffern) wird sie geschützt; die einfache Ansicht bleibt
ohne PIN bedienbar. **Fertig** sperrt die Einrichtung wieder; nach 10 Minuten ohne Bedienung
sperrt sie sich selbst (nicht, solange ein Formular offen ist). Nach 5 falschen PINs wartet die
App 1 Minute, danach jeweils doppelt so lange. Die PIN liegt nur als gesalzener Hash in
`data/data.json`. **PIN vergessen?** App stoppen, `python3 run.py --reset-pin`, App starten.

## Funktionen der Einrichtung

- **Räume:** Heizungen zu Räumen zusammenfassen und gemeinsam steuern
  (Solltemperatur, Manuell / Automatik / Boost / Aus). Im Tab „Räume“ oben **+ Raum**, auf jeder
  Raum-Kachel **Bearbeiten**: Name und die Heizungen des Raums ankreuzen (eine Heizung, die schon in
  einem anderen Raum ist, wechselt dabei den Raum); dort auch „Raum löschen“. Kommt eine Heizung
  neu in einen Raum, zeigt die Kachel an, dass der Heizplan noch gesendet werden muss.
- **Heizpläne:** in der App verwalten (bis zu 10 Zeitfenster pro Tag, Viertelstunden-Raster)
  und an die Heizungen eines Raums **senden**. Die Heizung führt den Plan im Modus
  „Automatik“ selbst aus – auch wenn die App aus ist.
- **Kalibrieren** (Tab „Geräte“, je Heizung): gemessene Raumtemperatur eintragen – die App setzt die
  Temperatur-Korrektur der Heizung (±9,9 °C) so, dass sie danach diesen Wert anzeigt und danach
  regelt. Verglichen wird mit dem Mittel der Heizung über die letzten 30 Minuten (sobald die App
  mindestens 5 Minuten läuft), sonst mit dem aktuellen Wert. „Korrektur auf 0 setzen“ nimmt sie zurück.
  Richtig messen: Thermometer in der Raummitte auf 1–1,3 m Höhe, Raum seit 1–2 Stunden auf
  Solltemperatur, über 30–60 Minuten mehrmals ablesen und mitteln.
- **Geräte-Liste filtern:** Im Tab „Geräte“ nach Raum filtern (Alle / je Raum / Nicht zugeordnet,
  jeweils mit Anzahl); die Auswahl merkt sich die App.
- **Heizplan aus Heizung auslesen** (Tab „Geräte“): liest alle drei Planplätze einer Heizung
  und übernimmt jeden belegten, noch unbekannten Plan als neuen Heizplan in die App (z. B. Pläne
  aus SmartHeat4U oder vom Display). Minutengenaue Zeiten bleiben erhalten; Duplikate werden
  erkannt. Funktioniert auch ohne Token.
- **Uhren:** werden automatisch gestellt (beim Start, bei > 1 min Abweichung, täglich 3 Uhr –
  deckt Sommer-/Winterzeit ab). Ohne Internet stellt der Regler sie nicht selbst um.
- Ausgelegt für bis zu 10 Heizungen (mehr gehen auch).

## Start

```bash
git clone https://github.com/antratec-git-1/FlowThermPi.git
cd FlowThermPi
python3 run.py                  # http://127.0.0.1:8080 – nur auf diesem Rechner
```

Im ganzen Heimnetz (z. B. vom iPhone) erreichbar machen – dann bitte mit Passwort
(Benutzername im Browser: `heizung`):

```bash
HEIZAPP_PASSWORD='geheim' python3 run.py --host 0.0.0.0
```

Beim ersten Start fragt macOS, ob das Terminal auf Geräte im lokalen Netzwerk zugreifen
darf – erlauben, sonst erscheinen die Heizungen als „offline“ („No route to host“).

## Installation auf dem Raspberry Pi

Voraussetzung: Raspberry Pi mit aktuellem **Raspberry Pi OS** (Lite reicht), im selben Netz wie die
Heizungen und per SSH erreichbar. Dann **ein Befehl** per SSH auf dem Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/antratec-git-1/FlowThermPi/main/install.py | sudo python3 -
```

Das Skript lädt FlowThermPi von GitHub nach `/opt/flowthermpi`, legt den Dienst `flowthermpi` an
(startet beim Einschalten und nach Absturz oder Stromausfall von selbst), legt die Daten in
`/var/lib/flowthermpi` an (Heizungsliste mit Tokens nur für den Dienst lesbar), stellt die Zeitzone
auf Europe/Berlin, falls noch die Werkseinstellung gilt, prüft, ob die App antwortet, und zeigt die
Adresse an – z. B. `http://flowthermpi.local`.

- **Update:** denselben Befehl noch einmal ausführen. Startet die neue Version nicht, läuft die
  vorherige weiter. Heizungen, Tokens, Räume, Pläne und PIN bleiben erhalten.
- **Daten vom Mac übernehmen** (Heizungen mit Tokens, Räume, Pläne, PIN):
  ```bash
  # auf dem Mac, im App-Ordner:
  scp heizungen.conf data/data.json BENUTZER@flowthermpi.local:
  # auf dem Pi:
  curl -fsSL https://raw.githubusercontent.com/antratec-git-1/FlowThermPi/main/install.py | sudo python3 - --import ~
  rm ~/heizungen.conf ~/data.json
  ```
  Danach die App auf dem Mac beenden – sonst steuern zwei Apps dieselben Heizungen.
- **Weitere Optionen** hinter `python3 -`: `--port 8080`, `--ref v0.2.0` (bestimmte Version),
  `--reset-pin` (PIN vergessen), `--uninstall` (Daten bleiben), `--uninstall --purge` (alles weg).
- **Meldungen:** `journalctl -u flowthermpi -f`. Passwort fürs Heimnetz: `HEIZAPP_PASSWORD=…` in
  `/etc/default/flowthermpi` eintragen, dann `sudo systemctl restart flowthermpi`.
- **Uhrzeit:** Ein Raspberry Pi hat keine Batterie-Uhr. Die App stellt die Heizungsuhren erst, wenn
  der Pi seine Uhrzeit aus dem Internet hat – nach einem Stromausfall also nie auf eine falsche Zeit.

## Heizungen im Netz suchen

In der Einrichtung unter „Geräte“ → **Im Netz suchen**: Die App fragt jede Adresse des Heimnetzes
einmal nach ihrer Seriennummer – nur lesend, ohne Token, ein paar Sekunden für ein /24-Netz – und
listet alle Heizungen auf (Name `Heat4U-…`, IP, Raumtemperatur). Bekannte sind als „in der App“
markiert, neue haben **Hinzufügen**: Das Formular hat die IP schon eingetragen und erklärt, wie man
an den Token kommt. Läuft die App in Docker oder in einem anderen Netz, lässt sich das Heimnetz
angeben (z. B. `192.168.178`; nur private Netze, höchstens /22).

**Neue IP-Adresse:** Ist eine bekannte Heizung länger als 3 Minuten nicht erreichbar, sucht die
App sie (höchstens alle 15 Minuten) im Netz und übernimmt ihre neue Adresse anhand der
Seriennummer selbst – ebenso bei jeder Suche von Hand. Antwortet unter einer bekannten Adresse
plötzlich eine andere Heizung, ordnet die App Namen und Raum über die Seriennummer neu zu.

**Den Token zeigt die Suche nicht** – dafür gibt es keinen Befehl, und jeder Befehl an die Heizung
braucht selbst schon den Token (so beweist er, dass man vor der Heizung stand). Er steht im QR-Code:
an der Heizung *Einstellungen › Verbindungen › Verbinden*. Ist die Heizung schon mit SmartHeat4U
verbunden, steht dort nur *Trennen*: erst Trennen, dann Verbinden, und die Heizung danach in
SmartHeat4U mit demselben QR-Code neu koppeln.

## Konfiguration: `heizungen.conf`

Heizungen legst du am einfachsten in der App an: Tab „Geräte“ → **+ Heizung**, bzw. **Bearbeiten**
bei einer Heizung (Name, IP-Adresse, Token; Token entfernen; Heizung entfernen). Beim Speichern
prüft die App Adresse und Token direkt an der Heizung und schreibt `heizungen.conf` selbst –
ohne Neustart. Ins Token-Feld kann auch der komplette QR-Inhalt
(`{"ver":"v1","name":"…","pop":"…",…}`) eingefügt werden; die App nimmt dann `pop` und prüft,
ob die Seriennummer zur IP-Adresse passt.

Die Datei kann weiterhin auch von Hand bearbeitet werden (danach App neu starten).
Eine Heizung pro Zeile – IP-Adresse (oder FRITZ!Box-Name) und Token:

```
192.168.5.81     <24 Zeichen Token>
192.168.5.152                        # ohne Token: nur Anzeige
```

**Token besorgen:** An der Heizung *Einstellungen > Verbindungen > Verbinden* – das Display
zeigt einen QR-Code. Darin steht JSON, der Token ist das Feld `pop` (24 Zeichen). Ein erneutes
Einrichten erzeugt einen neuen Token. Danach in SmartHeat4U koppeln, damit die App des
Herstellers weiter funktioniert.

Tipp: In der FRITZ!Box jeder Heizung eine feste IP-Adresse geben. Die Heizungen heißen dort
`Heat4U-` + erste und letzte drei Ziffern der Seriennummer.

`heizungen.conf` enthält die Tokens → nur für den eigenen Benutzer lesbar (`chmod 600`),
steht in `.gitignore`. Räume, Namen und Heizpläne speichert die App in `data/data.json`.

## Wichtig zu wissen

- **Zwischen den Zeitfenstern** hält die Heizung ihre Frostschutztemperatur (meist 5 °C).
  Eine Absenkung (z. B. nachts 17 °C) braucht ein eigenes Zeitfenster – im Planeditor hilft
  „Lücken füllen mit … °C“.
- Die App schreibt ihre Pläne immer in **Plan 1** der Heizung.
- **Aus** = manueller Modus auf Frostschutztemperatur.
- **Boost** heizt 30 Minuten mit voller Leistung; erneutes Tippen beendet ihn und kehrt zum
  vorherigen Modus zurück.
- Die Heizung speichert keinen Verlauf.

## Schnittstelle – was am Gerät gemessen wurde (Firmware 00.01.08)

Grundlage ist Thermotecs „Lokale Datenschnittstelle“ (EU Data Act, Ausgabe 04.09.2026).
Einiges verhält sich anders als dort beschrieben:

| Thema | Doku | tatsächlich |
|---|---|---|
| Ressource | `/json` | `/Json` (`/json` → 4.04) |
| Token | „Query-Parameter“ | `?token=<pop>`; `pop=`, falscher Token → 4.01 |
| Lesen | Token nötig | geht **ohne** Token |
| Schreiben | JSON-RPC 2.0 | ohne `"jsonrpc"`-Feld senden, sonst still ignoriert |
| Modus wechseln | `auto_mode_en` usw. | `operating_mode` schreiben (0 Manuell, 1 Plan, 2 Boost); die `…_en`-Felder sind nur Freigaben |
| Zeitfenster | 6 pro Tag | 10 pro Tag, `"HH:MM,HH:MM,<0,1 °C>"`, leer = `"00:00,00:00,0"`, Ende max. 23:59 |
| Überlappende Fenster | – | werden ohne Fehlermeldung verworfen (App prüft vorher und liest zurück) |
| `cmd clear_plan` | löscht Plan | Fehler -31001 → App löscht Fenster einzeln |
| Gerätesuche | Multicast `WHEREIS` | keine Antwort; die App fragt stattdessen jede Adresse einzeln (Lesen geht ohne Token) |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Die Tests laufen gegen eine simulierte Heizung (`tests/fake_heater.py`), die das oben
beschriebene Verhalten nachbildet – die echten Heizungen werden dabei nicht angesprochen.

## Ideen für später

- Paket für das NAS (Docker); automatische Updates und Update-Knopf in der Einrichtung.
- Licht und Sensoren über die Philips-Hue-Bridge; Regelung über einen Raumfühler.
- Verlauf und Kosten (Heizdauer × Nennleistung × Strompreis).
- Heizungen zusätzlich in Apple Home einbinden.

## Lizenz

Copyright © 2026 antratec

FlowThermPi ist freie Software unter der **GNU General Public License, Version 3**
(siehe [LICENSE](LICENSE)): Sie darf genutzt, verändert und weitergegeben werden – wer eine
veränderte Version weitergibt, muss deren Quellcode ebenfalls unter dieser Lizenz offenlegen.
FlowThermPi wird ohne jede Gewährleistung bereitgestellt.
