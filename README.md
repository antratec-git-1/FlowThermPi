# FlowThermPi – lokale Steuerung für Thermotec AeroFlow (FlexiSmart Pro)

Kleine Web-App, die Thermotec-Elektroheizungen mit **FlexiSmart-Pro-Regler** direkt im
Heimnetz steuert – ohne Cloud, ohne Konto, ohne Zusatzpakete (nur Python-Standardbibliothek). Nur die
freiwillige [Fernwartung](#fernwartung-tailscale) nutzt Tailscale, und die
[Zigbee-Thermostate](#zigbee-thermostate-in-arbeit) (in Arbeit) brauchen Zusatzprogramme, die die App auf Knopfdruck
installiert.

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
- **Aussehen:** Jede Kachel färbt sich nach der Raumtemperatur, von kühlem Blaugrau (12 °C) bis Aprikose (25 °C);
  ausgeschaltete Räume sind grau. Oben rechts steht ein Schild: „heizt“, „volle Kraft“, „Fenster offen“, „aus“ oder
  „Urlaub“. Solange die Heizung heizt, läuft unten auf der Kachel eine sanfte Wärmewelle (sie steht still, wenn das
  Gerät „Bewegung reduzieren“ eingestellt hat). Nach **wärmer / kälter** steht unter der Zahl „wird gesendet …“, dann
  kurz „✓ gesendet“. Steht das Gerät auf dunkel, wird auch die Seite dunkel, mit warm leuchtenden Zahlen. Die Schrift
  ist [Atkinson Hyperlegible Next](https://github.com/googlefonts/atkinson-hyperlegible-next), gemacht für schwache
  Augen; sie liegt in der App, nichts wird aus dem Internet geladen.
- **Raumfühler:** Hat der Raum einen [Fühler](#raumfühler-shelly-pill), steht unter der großen Zahl zusätzlich
  „Fühler: 21,3 °C“ (beim DHT22 mit Luftfeuchte); die große Zahl heißt dann „an der Heizung“. Ein Messwert, der älter
  als 15 Minuten ist, erscheint dort nicht.
- **Fernhilfe erlauben** (ganz unten, wenn die [Fernwartung](#fernwartung-tailscale) eingerichtet ist): Dann lässt
  sich die App zwei Stunden lang aus der Ferne öffnen, oben steht „Fernhilfe ist an – bis 16:30 Uhr“ mit **Beenden**.
- **Protokoll speichern** (ganz unten, klein, ohne PIN): Klappt etwas nicht, speichern die Bewohner damit eine Datei
  und schicken sie dem Helfer – auf Wunsch mit vorbereiteter E-Mail. Siehe [Protokoll](#protokoll-wenn-etwas-schiefgeht).

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
- **Raumfühler** (Tab „Geräte“ → „Fühler“): Shelly Pills im Heimnetz suchen, ihre Fühler einem Raum zuweisen und
  einstellen, was in der Pill steckt (DS18B20 oder DHT22). Der Fühler zeigt seine Temperatur neben der der Heizungen
  seines Raums – so sieht man, ob eine Heizung kalibriert werden sollte. Ein **Kalibrierlauf** (zum Beispiel einen Tag
  lang) schlägt dann je Heizung die passende Korrektur vor. Siehe [Raumfühler: Shelly Pill](#raumfühler-shelly-pill).
- **Geräte-Liste filtern:** Im Tab „Geräte“ nach Raum filtern (Alle / je Raum / Nicht zugeordnet,
  jeweils mit Anzahl); die Auswahl merkt sich die App.
- **Heizplan aus Heizung auslesen** (Tab „Geräte“): liest alle drei Planplätze einer Heizung
  und übernimmt jeden belegten, noch unbekannten Plan als neuen Heizplan in die App (z. B. Pläne
  aus SmartHeat4U oder vom Display). Minutengenaue Zeiten bleiben erhalten; Duplikate werden
  erkannt. Funktioniert auch ohne Token.
- **Uhren:** werden automatisch gestellt (beim Start, bei > 1 min Abweichung, täglich 3 Uhr –
  deckt Sommer-/Winterzeit ab). Ohne Internet stellt der Regler sie nicht selbst um.
- **WLAN** (nur auf dem Raspberry Pi): Netze in der Nähe suchen, mit Passwort verbinden, gespeicherte
  WLANs vergessen – siehe [WLAN einrichten](#wlan-einrichten).
- **Fernwartung** (nur auf dem Raspberry Pi): den Pi mit Tailscale aus der Ferne erreichbar machen, um einem
  Haushalt zu helfen, ohne hinzufahren – von sich aus nur, solange die Bewohner es erlauben. Siehe
  [Fernwartung](#fernwartung-tailscale).
- **Zigbee** (nur auf dem Raspberry Pi, in Arbeit): die Programme für SONOFF-Zigbee-Thermostate installieren,
  aktualisieren und entfernen, Zigbee mit einem Funkkanal einrichten, Thermostate anlernen und wieder entfernen.
  Angelernte Thermostate sind Heizungen der App wie die AeroFlow: Raum, Temperatur, Ein/Aus, Boost, Heizplan,
  Kalibrierung, dazu „Sanft regeln“ und neue Firmware per Knopf – siehe
  [Zigbee-Thermostate](#zigbee-thermostate-in-arbeit).
- **Protokoll** (Tab „Über“): die E-Mail-Adresse des Helfers eintragen, das ausführliche Protokoll für 24 Stunden
  einschalten, selbst ein Protokoll speichern – siehe [Protokoll](#protokoll-wenn-etwas-schiefgeht).
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

- **Am Mac mit Chrome oder Firefox nicht erreichbar** (Chrome: `ERR_ADDRESS_UNREACHABLE`): Ab macOS 15
  braucht jeder Browser außer Safari die Freigabe *Lokales Netzwerk*. Der Mac fragt beim ersten Aufruf
  danach – *Erlauben* wählen und die Seite neu laden. Wurde die Frage abgelehnt: *Systemeinstellungen ›
  Datenschutz & Sicherheit › Lokales Netzwerk*, dort den Browser einschalten und die Seite neu laden; hilft
  das nicht, den Browser ganz beenden (⌘Q) und neu öffnen. Safari braucht die Freigabe nicht, Browser auf
  iPhone und iPad auch nicht
  ([Apple TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)).
- **Update per Knopf:** in der App *Einrichtung › Über › Nach Updates suchen*, dann *Jetzt
  aktualisieren*. Der Pi holt den neuesten Stand von `main` und startet neu (etwa eine Minute, die
  Heizungen heizen weiter); die Seite lädt danach von selbst neu. Startet die neue Version nicht,
  läuft die vorherige weiter. Heizungen, Tokens, Räume, Pläne und PIN bleiben erhalten.
  Den Knopf gibt es ab Version 0.2.0 – ein älterer Pi bekommt ihn, wenn der Befehl oben einmal
  ausgeführt wird. Der Befehl funktioniert auch weiterhin als Update.
- **Wie der Knopf abgesichert ist:** Die App hat keine Rechte am Programm. Sie legt nur eine Anfrage
  in ihren Datenordner. Daraufhin startet der Dienst `flowthermpi-update` (als root) das installierte
  `install.py`; das lädt immer `main` von diesem Repository und installiert es mit dem Installer der
  neuen Version – genau wie der Befehl oben. Welcher Code kommt, kann die App nicht bestimmen. Im
  Datenordner folgt root keinen Verweisen (Symlinks). Meldungen: `journalctl -u flowthermpi-update`.
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
  `--reset-pin` (PIN vergessen), `--uninstall` (Daten bleiben; Zigbee-Programme kommen mit weg),
  `--uninstall --purge` (alles weg).
- **Meldungen:** `journalctl -u flowthermpi -f` – oder in der App [Protokoll speichern](#protokoll-wenn-etwas-schiefgeht).
  Passwort fürs Heimnetz: `HEIZAPP_PASSWORD=…` in
  `/etc/default/flowthermpi` eintragen, dann `sudo systemctl restart flowthermpi`.
- **Uhrzeit:** Ein Raspberry Pi hat keine Batterie-Uhr. Die App stellt die Heizungsuhren erst, wenn
  der Pi seine Uhrzeit aus dem Internet hat – nach einem Stromausfall also nie auf eine falsche Zeit.

## WLAN einrichten

Der Pi wird mit Netzwerkkabel in Betrieb genommen; ins WLAN bringt man ihn danach in der App, unter
*Einrichtung › WLAN*:

1. Netzwerkkabel am Pi einstecken, die App öffnen (z. B. `http://flowthermpi.local`), *Einrichtung › WLAN*.
   Die App sucht die WLANs in der Nähe.
2. Beim eigenen WLAN auf **Verbinden** tippen und das WLAN-Passwort eingeben (bei der FRITZ!Box der
   „WLAN-Netzwerkschlüssel“ auf der Unterseite). Nach bis zu einer Minute meldet die App
   „Der Pi ist jetzt im WLAN …“ und zeigt die neue Adresse.
3. Kabel abziehen. Die App ist weiter unter `http://flowthermpi.local` erreichbar oder unter der angezeigten
   Adresse.

- Klappt das Verbinden nicht (Passwort falsch, WLAN außer Reichweite), entfernt der Pi den neuen Eintrag
  wieder und geht zurück ins vorherige WLAN, falls es eins gab. Die App zeigt den Grund.
- Frisches Raspberry Pi OS hat das WLAN aus, bis ein WLAN-Land gesetzt ist. Das erledigt die App bei der
  ersten Suche (Deutschland).
- **Vergessen** entfernt ein gespeichertes WLAN. Das WLAN, über das der Pi gerade im Netz hängt, lässt sich nur
  vergessen, wenn das Netzwerkkabel steckt – sonst wäre der Pi nicht mehr erreichbar, auch nicht aus der Ferne
  (etwa über Tailscale). Der Pi prüft das vor dem Löschen selbst noch einmal.
- Versteckte WLANs: „Namen selbst eingeben“. Firmen-WLANs mit eigener Anmeldung (802.1X) gehen nicht.
- **Wie es abgesichert ist:** wie beim Update-Knopf. Die App legt eine Anfrage in ihren Datenordner (nur für
  sie lesbar). Der Dienst `flowthermpi-wifi` (root) prüft sie noch einmal, löscht sie sofort und steuert
  NetworkManager (`nmcli`) – ohne Shell, mit genau drei Aktionen: suchen, verbinden, vergessen. Das
  WLAN-Passwort steht nie im Status, im Log oder in Fehlermeldungen. Beim Anlegen der Verbindung steht es
  einen Sekundenbruchteil in den Argumenten von `nmcli`, wie bei `nmcli device wifi connect … password …`.
  Meldungen: `journalctl -u flowthermpi-wifi`.
- Voraussetzung: Raspberry Pi OS ab Bookworm (NetworkManager). Den Reiter gibt es ab Version 0.3.0; ein Pi
  mit 0.2.0 bekommt ihn über den Update-Knopf.

## Fernwartung (Tailscale)

Damit man einem Haushalt helfen kann, ohne hinzufahren: Unter *Einrichtung › Fernwartung* kommt der Pi in das
eigene private Netz bei [Tailscale](https://tailscale.com). Darüber öffnet man die App von überall, ohne Freigaben
im Router. Die Fernwartung ist freiwillig; ohne sie bleibt alles im Heimnetz. Und auch mit ihr ist der Pi von sich
aus nur erreichbar, solange die Bewohner es erlauben – ähnlich wie bei einer Fernwartungs-Software für den PC.

**Vorher, einmal:** ein eigenes Tailscale-Konto anlegen (z. B. mit dem GitHub-Login), Tailscale auf dem eigenen
Mac oder Handy installieren und das [Tailscale-Netz einrichten](#tailscale-netz-einrichten-einmal-vor-dem-ersten-pi).
Der Tarif *Personal* ist kostenlos und für private Nutzung gedacht (Stand November 2026: bis 6 Nutzer, bis 50
getaggte Geräte; aktuelle Bedingungen auf [tailscale.com/pricing](https://tailscale.com/pricing)).

1. Im Heimnetz die App öffnen, *Einrichtung › Fernwartung › Fernwartung einrichten*. Der Pi lädt Tailscale aus
   dessen offiziellem, signiertem Paketarchiv (`pkgs.tailscale.com`, etwa 30 MB). Das dauert ein paar Minuten; die
   Heizungen heizen dabei weiter.
2. Die App zeigt einen QR-Code und einen Anmelde-Link. Mit dem Handy scannen und mit dem eigenen Tailscale-Konto
   anmelden. **Link und Code nicht weitergeben:** Wer sich damit anmeldet, holt den Pi in sein eigenes Netz.
3. Danach zeigt die App „An – Fernhilfe bis …“ und die Adressen für unterwegs, z. B. `http://flowthermpi-a1b2c3d4`
   (mit MagicDNS) oder `http://100.x.y.z`. Zwei Stunden lang lässt sich das ausprobieren, dann schaltet sich die
   Fernwartung von selbst aus – bis die Bewohner die nächste Fernhilfe erlauben.

### Fernhilfe: nur, wenn die Bewohner es erlauben

- Ruft ein Haushalt an, tippen die Bewohner in der Heizungsansicht ganz unten auf **Fernhilfe erlauben** und
  bestätigen. Zwei Stunden lang ist der Pi dann aus der Ferne erreichbar; oben steht „Fernhilfe ist an – bis 16:30
  Uhr“ mit **Beenden**. **Fernhilfe verlängern** lässt die zwei Stunden neu beginnen.
- Auf dem eigenen Mac oder Handy Tailscale einschalten und die App unter der Adresse des Pis öffnen. Fertig?
  **Beenden** – das geht auch aus der Ferne, in der Heizungsansicht oder unter *Einrichtung › Fernwartung*.
- Nach zwei Stunden schaltet die App Tailscale von selbst aus (`tailscale down`), auch nach einem Neustart der App
  oder des Pis – und ebenso, wenn jemand es auf dem Pi von Hand eingeschaltet hat.
- Erlauben und verlängern geht nur im Heimnetz, nicht aus der Ferne (über Tailscale). So kann sich niemand selbst
  mehr Zeit geben, als die Bewohner erlaubt haben.
- Die Fernhilfe schaltet nur ein, was eingerichtet ist: Sie installiert nichts und startet keine neue Anmeldung.
- *Einrichtung › Fernwartung › Wann ist der Pi erreichbar?:* **Nur nach Freigabe** (Vorgabe) oder **Immer** – dann
  bleibt die Fernwartung dauerhaft an, und in der Heizungsansicht steht „Fernwartung ist eingeschaltet“. Ändern lässt
  sich das nur im Heimnetz.
- Ein Pi, der schon vor Version 0.4.1 eingerichtet wurde, ist danach ebenfalls auf „Nur nach Freigabe“ und schaltet
  die Fernwartung gleich nach dem Update aus. Wer sie dort dauerhaft möchte, wählt im Heimnetz „Immer“.

### Was sonst noch gilt

- **Name:** Im Tailscale-Netz heißt der Pi `flowthermpi-` + **Gerätenummer** (die letzten 8 Zeichen seiner
  Seriennummer), damit sich mehrere Pis auseinanderhalten lassen. Die Gerätenummer steht unter *Einrichtung › Über*
  und *Fernwartung*, am besten auch als Aufkleber auf dem Pi. Im Heimnetz bleibt er `flowthermpi.local`.
- **Die Bewohner sehen es:** Während einer Fernhilfe steht in der Heizungsansicht, bis wann sie läuft; bei „Immer“
  steht dort „Fernwartung ist eingeschaltet“.
- **Ausschalten** (bei „Nur nach Freigabe“: **Fernhilfe beenden**) trennt den Pi vom Tailscale-Netz
  (`tailscale down`); angemeldet bleibt er. **Einschalten** geht ohne neue Anmeldung, aber nur im Heimnetz – bei „Nur
  nach Freigabe“ als Fernhilfe für zwei Stunden. **Abmelden** (`tailscale logout`) nimmt ihn ganz heraus; danach
  lässt er sich in der Tailscale-Verwaltung löschen.
- **Updates:** Tailscale hält sich selbst aktuell (`tailscale set --auto-update`).
- Der Pi übernimmt keine DNS-Einstellungen aus dem Tailscale-Netz (`--accept-dns=false`), damit Heizungen mit
  FRITZ!Box-Namen erreichbar bleiben. Er gibt nur sich selbst frei, nicht das Heimnetz (keine Subnet-Routen).
- `--uninstall` lässt Tailscale installiert. Abmelden: `sudo tailscale logout`, entfernen: `sudo apt remove tailscale`.
- **Wie es abgesichert ist:** wie beim Update-Knopf. Die App legt nur eine Anfrage mit der gewünschten Aktion in
  ihren Datenordner. Der Dienst `flowthermpi-remote` (root) kennt genau fünf: nachsehen, einschalten (beim ersten Mal
  mit Installieren und Anmelden), nur einschalten (Fernhilfe), ausschalten, abmelden. Namen, Tag und Einstellungen
  bestimmt er selbst, ohne Shell. Den Anmelde-Link zeigt nur die Einrichtung, nie die Heizungsansicht. Die
  Einstellung und das Ende der Fernhilfe stehen in `/var/lib/flowthermpi/remote-settings.json`. Meldungen:
  `journalctl -u flowthermpi-remote`.
- Voraussetzung: Raspberry Pi OS (Debian) mit Internet. Den Reiter gibt es ab Version 0.4.0, die Fernhilfe ab
  0.4.1; ein älterer Pi bekommt beides über den Update-Knopf.

### Tailscale-Netz einrichten (einmal, vor dem ersten Pi)

Von sich aus darf in einem Tailscale-Netz jedes Gerät jedes andere erreichen, also auch der Pi im einen Haushalt den
Pi im anderen und den eigenen Mac. Deshalb in der Verwaltung ([login.tailscale.com](https://login.tailscale.com/admin)):

**Access controls:** die Regel, nach der alle alles dürfen (`"src": ["*"]`, `"dst": ["*"]` bzw. `"*:*"`), löschen
und stattdessen diese beiden Abschnitte eintragen (die übrigen, etwa `ssh`, bleiben):
```jsonc
{
  "tagOwners": { "tag:flowthermpi": ["autogroup:admin"] },
  "grants": [
    // die eigenen Geräte untereinander (Mac, Handy …)
    { "src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["*"] },
    // die eigenen Geräte zu den Pis: nur die App (Port 80) und SSH
    { "src": ["autogroup:member"], "dst": ["tag:flowthermpi"], "ip": ["tcp:80", "tcp:22"] }
  ]
}
```

Für die Pis selbst gibt es keine Regel: Sie erreichen weder einander noch den Mac. Die Verbindung geht also nur in
eine Richtung – von den eigenen Geräten zum Pi.

Jeder Pi meldet sich selbst mit dem Tag `tag:flowthermpi` an (`--advertise-tags`). `tagOwners` erlaubt den Tag den
Admins des Netzes – meldet ein Admin (etwa der Inhaber) den Pi mit dem QR-Code an, bekommt er den Tag gleich dabei.
Von Hand taggen ist dann nicht nötig, und weil der Pi schon bei der ersten Anmeldung getaggt ist, läuft seine
Anmeldung nicht ab. Ohne den Abschnitt `tagOwners` lässt Tailscale den Tag nicht zu, und die Anmeldung scheitert.

Ein Pi, der vor dieser Version angemeldet wurde, hat den Tag nicht von selbst: einmal *Machines* › beim Pi „…“ ›
*Edit ACL tags* › `tag:flowthermpi` und *Disable key expiry*. Ein nachträglich vergebener Tag allein beendet den
Ablauf nicht; die App zeigt das Ablaufdatum, solange es eins gibt.

## Heizungen im Netz suchen

In der Einrichtung unter „Geräte“ → Untertab „Heizung“ → **Im Netz suchen**: Die App fragt jede Adresse des Heimnetzes
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


## Raumfühler: Shelly Pill

Als Raumfühler dient die **Shelly Pill** („The Pill by Shelly“, Modell S3SN-0U53X) am USB-C-Netzteil. In ihre
3,5-mm-Buchse kommt ein Temperaturfühler **DS18B20** (bis zu fünf über den 1→5-Verteiler) oder ein **DHT22** für
Temperatur und Luftfeuchte (zum Beispiel über das 5-Terminal-Add-on). Die Pill schläft nie: Die App liest gespeicherte
Fühler jede Minute und hat ihre Werte immer parat. Die Pill gibt es in der App ab Version 0.4.2. Den Shelly H&T
unterstützt sie nicht mehr; einen gespeicherten H&T entfernt sie beim Start (mit einem Eintrag im Log).

**Vorher, einmal:** die Pill mit der Shelly-App ins WLAN bringen (oder über ihren Hotspot `ShellyPill-…` und
`http://192.168.33.1`), den Fühler einstecken und die Pill ans Netzteil hängen.

**Fühler suchen:** In der Einrichtung unter „Geräte“ → Untertab „Fühler“ → **Fühler suchen**. Die App fragt jede
Adresse des Heimnetzes einmal nach `GET /shelly` (rund 3 s für ein /24-Netz) und liest jede gefundene Pill mit
`Shelly.GetStatus`, `Pill.GetConfig` und `Shelly.GetComponents` aus: was eingestellt ist (DS18B20 oder DHT22) und was
jeder eingesteckte Fühler misst. Die Pill meldet jeden Fühler als eigene Komponente (der erste DS18B20 heißt
`temperature:200`), aber nur in `Shelly.GetComponents`. Dabei wird nur gelesen.

**Übernehmen und Raum:** Bei einem gefundenen Fühler **Übernehmen** drücken, optional einen Namen eintragen, einen Raum
wählen, speichern. Jeder Fühler einer Pill wird ein eigener Fühler in der App, mit eigenem Raum. Gespeicherte Fühler
stehen unter „Raumfühler“ (Raum direkt umstellbar, „Bearbeiten“ für Name und Entfernen), auf der Raumkachel der
Einrichtung mit ihrem letzten Messwert und auf der Startseite in der Kachel ihres Raums. **Aktualisieren** liest sofort. Antwortet eine Pill nicht mehr unter ihrer Adresse, sucht die
App sie im Heimnetz (höchstens alle 15 Minuten) und übernimmt die neue Adresse.

**Einrichten** (bei jeder gefundenen Pill und bei jedem gespeicherten Fühler): Hier stellt man ein, was in der Pill
steckt – DS18B20 oder DHT22 (`Pill.SetConfig`, Betriebsart `onewire` bzw. `dht22`). Braucht die Pill dafür einen
Neustart, startet die App sie neu. **Pill neu starten** (`Shelly.Reboot`) hilft, wenn ein eingesteckter Fühler nicht
erkannt wird; nach etwa 15 Sekunden sieht die App wieder nach. Nur hier schickt die App etwas an die Pill, und vorher
prüft sie, dass unter der Adresse noch dieselbe Pill antwortet. Eine Pill mit Passwort kann die App weder lesen noch
einrichten.

**Kalibrierung – Vergleich:** Ist der Fühler einem Raum zugeordnet, zeigt er unter „Kalibrierung“ seine
Isttemperatur neben der Isttemperatur jeder Heizung des Raums und die Abweichung (Heizung − Fühler).
Beide Werte stammen vom selben Zeitpunkt: Bei jedem Auslesen des Fühlers (jede Minute, Suche, „Aktualisieren“)
merkt sich die App die Raumtemperatur der Heizungen dieses Moments. Bis ±0,5 °C gilt als „passt“,
sonst „zeigt zu viel“ oder „zeigt zu wenig“. Die Heizung meldet ihren Wert schon mit ihrer Korrektur.

**Kalibrierlauf:** Genauer als ein einzelner Vergleich. Den Fühler in die Raummitte auf 1–1,5 m Höhe legen, nicht in
die Sonne und nicht neben Fenster oder Heizung. Dann unter „Kalibrierung“ die Dauer wählen (6 Stunden bis 3 Tage,
empfohlen 1 Tag) und **Kalibrierlauf starten**.
- Jede Minute paart die App den Messwert des Fühlers mit jeder Heizung des Raums. Es zählen nur ruhige Minuten: Die
  Heizung antwortet, sie meldet kein offenes Fenster, kein Boost, sie ist nicht aus (Standby, Urlaub), und Betriebsart,
  Solltemperatur und Fenster sind seit mindestens einer Stunde unverändert. Was nicht zählt, zeigt die App mit Grund an.
- Für jede ruhige Minute rechnet sie aus, welche Korrektur die Heizung genau auf den Wert des Fühlers gebracht hätte
  (Fühler − Heizung + ihre Korrektur in diesem Moment, auf 0,1 °C). Der Vorschlag ist der Median aller Minuten.
  Ausreißer wie Sonne auf dem Fühler zählen so kaum, und eine zwischendurch geänderte Korrektur stört nicht. Daneben
  steht, wie weit die mittlere Hälfte der Minuten auseinanderliegt: „gleichmäßig“, „schwanken etwas“ oder „schwanken
  stark“. Über 1 °C ist der Vorschlag unsicher.
- Nach der gewählten Dauer endet der Lauf von selbst. **Jetzt auswerten** geht früher, sobald eine Heizung 2 Stunden
  ruhig gemessen ist. **Abbrechen** verwirft einen laufenden Lauf, **Schließen** ein Ergebnis.
- In die Heizung kommt ein Vorschlag erst mit **Übernehmen** (mit Rückfrage), wie beim Kalibrieren von Hand. Eine nur
  lesbare Heizung (ohne Token) und eine Korrektur außerhalb von ±9,9 °C übernimmt die App nicht.
- Der Lauf übersteht einen Neustart der App; sie sichert ihn mit den Messwerten in `data.json`. Solange es ihn gibt,
  bleibt der Fühler in seinem Raum. Wird der Raum gelöscht, endet der Lauf. Für einen ganzen Tag gehört die App auf
  den Pi – ein Rechner, der schläft, misst nicht.

**Gut zu wissen:**
- In `data.json` landet ein Messwert höchstens alle 15 Minuten (und bei Suche, Übernehmen und Aktualisieren). Das
  schont die Speicherkarte des Pi; die Anzeige ist trotzdem jede Minute aktuell.
- Firmware-Updates der Pill spielt man in der Shelly-App ein. Die App zeigt nur an, dass eines bereitsteht.
- DS18B20 und DHT22 messen laut Datenblatt auf ±0,5 °C genau.

## Zigbee-Thermostate (in Arbeit)

SONOFF-Heizkörperthermostate (**TRV Gen2**, Modell TRV-ZBT), die über einen Zigbee-USB-Stick am Pi (**SONOFF
Dongle Plus MG24**) gesteuert werden – in der App genauso wie die AeroFlow-Heizungen. Ab Version 0.4.4 installiert
*Einrichtung › Zigbee* die Zusatzprogramme dafür auf Knopfdruck; ab 0.4.6 richtet die App Zigbee ein und lernt
Thermostate an; ab 0.4.8 sind angelernte Thermostate Heizungen der App (*Thermostate steuern*, unten); ab 0.4.9
regeln sie auf Wunsch sanft und bekommen neue Firmware per Knopf. Noch nicht dabei: Heizpläne aus einem Thermostat
auslesen und die Shelly Pill als Fühler für ein Thermostat.

- **Was der Pi lädt:**
  - **Mosquitto** aus dem Paketarchiv von Raspberry Pi OS. Es reicht die Nachrichten zwischen Zigbee2MQTT und der App
    weiter – nur auf dem Pi selbst (`127.0.0.1:1883`) und nur mit Passwort.
  - **Node.js 24** (offizielles Archiv von nodejs.org) und **pnpm** (von npm): darauf läuft Zigbee2MQTT bzw. damit
    wird es gebaut.
  - **Zigbee2MQTT** (Quellcode von GitHub, Bausteine von npm): spricht über den Stick mit den Thermostaten.
- **Feste Versionen, jede Datei geprüft:** Welche Versionen kommen, steht in `install.py` (`ZIGBEE_PARTS`): für
  Node.js und pnpm mit Prüfsumme, für Zigbee2MQTT mit der Git-Commit-Nummer. Die Bausteine prüft pnpm gegen die
  Prüfsummen im Lockfile von Zigbee2MQTT (`--frozen-lockfile`). Passt etwas nicht, wird nichts installiert. Neue
  Versionen kommen mit einem Update von FlowThermPi; dann steht im Reiter **Neue Versionen installieren**.
- **Ablauf:** *Einrichtung › Zigbee › Zigbee-Programme installieren*. Die Seite zeigt Schritt und Fortschritt; die
  Heizungen heizen weiter, und die Seite darf zu (der Pi macht allein weiter, auch wenn die App neu startet).
  Geschätzt 10 bis 20 Minuten und etwa 200 MB Download – auf einem echten Pi noch nicht gemessen. Klappt etwas nicht,
  nennt die Seite den Grund (etwa „Speicherkarte voll“, „keine Verbindung zum Internet“) und zeigt die letzten
  Meldungen des Pi; eine schon installierte Version bleibt dann in Betrieb.
- **Voraussetzungen:** Raspberry Pi OS **64-bit** (auf einem 32-Bit-System lehnt die App ab, auch auf einem Pi 4),
  mindestens 1,5 GB frei, Internet.
- **Wo es liegt:** Programme in `/opt/flowthermpi-zigbee`, die Daten von Zigbee2MQTT (Einstellungen,
  Netzwerkschlüssel, angelernte Thermostate) in `/var/lib/flowthermpi-zigbee` (nur für ihn lesbar). Zigbee2MQTT wird
  vom eigenen Benutzer `flowthermpi-zigbee` gebaut und betrieben – ohne Anmeldung, mit Zugriff auf den USB-Stick, nie
  als root.
- **Passwörter für Mosquitto** erzeugt der Pi bei jeder Installation neu. Mosquitto kennt nur ihre Hashes; im Klartext
  stehen sie nur bei ihren Nutzern: für die App in ihrem Datenordner (`zigbee-mqtt.json`, 0600), für Zigbee2MQTT in
  `/etc/flowthermpi/zigbee2mqtt.env` (nur root; systemd reicht sie beim Start weiter). Die Seite zeigt sie nie.
- **Entfernen …** (bei den Programmen) hält Zigbee2MQTT an und löscht die Programme und Mosquitto wieder. Ein
  Mosquitto, das schon vorher auf dem Pi war, bleibt und
  verliert nur die Einstellungen von FlowThermPi. Die Daten von Zigbee2MQTT bleiben, damit angelernte Thermostate nach
  einer neuen Installation noch verbunden sind. `--uninstall` nimmt die Programme mit; `--uninstall --purge` auch die
  Daten und den Benutzer.
- **Wie es abgesichert ist:** wie beim Update-Knopf. Die App legt nur eine Anfrage in ihren Datenordner; der Dienst
  `flowthermpi-zigbee` (root) kennt genau sechs Aktionen: nachsehen, installieren, entfernen, einrichten, starten,
  anhalten. Was er lädt, kann die App nicht bestimmen; beim Einrichten nimmt er nur einen Funkkanal von 11 bis 26
  und einen Stick, der wirklich am Pi steckt. Archive entpackt er selbst, ohne Pfade außerhalb des Programmordners.
  Meldungen: `journalctl -u flowthermpi-zigbee`.

**Zigbee einrichten** (nach der Installation, mit eingestecktem Stick):

- **Funkkanal:** Zigbee funkt wie das WLAN auf 2,4 GHz und bekommt deshalb einen eigenen Kanal. Die App schlägt
  **Kanal 25** vor – er passt, wenn das WLAN auf Kanal 1 bis 10 funkt (das ist meistens so). Kanal 15 passt zu
  WLAN-Kanal 7 bis 13, Kanal 20 zu 1 bis 5 und 12 bis 13. Welchen Kanal das WLAN nutzt, zeigt die FRITZ!Box unter
  *WLAN › Funkkanal*. Gibt es im Haus eine Philips-Hue-Bridge, einen anderen Kanal als sie nehmen (steht in der
  Hue-App). Den Kanal kann man nur ändern, solange kein Gerät angelernt ist – sonst müsste man alle neu anlernen.
- **Was dabei passiert:** Der Pi schreibt die Einstellungen von Zigbee2MQTT (Stick, Funkkanal, Verbindung zu
  Mosquitto) – ohne eigene Weboberfläche, ohne Home Assistant. Den Netzwerkschlüssel würfelt Zigbee2MQTT beim ersten
  Start selbst aus; die Seite zeigt ihn nie, und im [Protokoll](#protokoll-wenn-etwas-schiefgeht) ist er versteckt.
  Dann startet der Dienst `flowthermpi-zigbee2mqtt`: mit dem Pi, nach einem Absturz von selbst neu, abgeschottet
  (schreiben darf er nur in seinen Datenordner).
- **Anhalten / Starten:** Angehalten bleibt Zigbee2MQTT auch nach einem Neustart des Pi aus, bis man „Starten“
  drückt. Die Thermostate behalten so lange ihre letzte Einstellung.
- **Startet nicht:** Die Seite sagt es und zeigt die letzten Meldungen von Zigbee2MQTT (Passwörter und Schlüssel
  darin versteckt), daneben „Neu starten“. Meldungen: `journalctl -u flowthermpi-zigbee2mqtt`.

**Thermostate anlernen:**

- **Thermostate suchen** öffnet das Zigbee-Netz für 3 Minuten. Die Seite zählt herunter und sagt, was am Thermostat
  zu tun ist: Batterien einlegen, das Drehrad unter 5 °C drehen, bis es aus ist, dann die mittlere Taste 2 Sekunden
  drücken, bis das Verbindungszeichen blinkt. Nach etwa einer Minute steht es in der Liste – mit Modell, Batterie,
  Funkqualität und gemessener Temperatur. Mehrere Thermostate gehen nacheinander in derselben Suche; klappt es nicht,
  das Thermostat zum Anlernen näher an den Pi bringen.
- **Entfernen …** (bei einem Thermostat) meldet es im Zigbee-Netz ab. Antwortet es nicht (es schläft oder ist zu weit
  weg), bietet die Seite **Trotzdem entfernen** an: Dann vergisst der Pi es, ohne auf seine Antwort zu warten.
- **Wie die App mit Zigbee2MQTT spricht:** über Mosquitto (MQTT), nur auf dem Pi selbst und mit Passwort – mit einem
  kleinen eigenen MQTT-Baustein (`heizapp/mqtt.py`, nur Python-Standardbibliothek).
- Den Reiter gibt es nur auf dem Pi (mit `install.py` eingerichtet); ein älterer Pi bekommt ihn über den Update-Knopf.

**Thermostate steuern** (ab 0.4.8):

- Jedes angelernte Thermostat steht unter *Einrichtung › Geräte* bei den Heizungen – mit Batterie, Funkqualität,
  Störungen und wann es sich zuletzt gemeldet hat. Dort bekommt es einen Namen und einen Raum. In `heizungen.conf`
  steht es nie; Name und Raum merkt sich die App in `data.json`, auch wenn es kurz aus dem Zigbee-Netz verschwindet.
  „+ Heizung“ fragt, ob eine AeroFlow (WLAN) oder ein Thermostat (Zigbee) dazukommt.
- Auf der Startseite und unter *Räume* geht alles wie bei der AeroFlow: kälter/wärmer, Ausschalten/Einschalten,
  Wieder nach Plan, volle Kraft (Boost, 30 Minuten), Heizplan senden. Ein Raum darf AeroFlow-Heizungen und
  Thermostate mischen.
- Ein Befehl braucht ein paar Sekunden, weil das Thermostat nur alle paar Sekunden beim Stick nachfragt. Er gilt erst
  als angekommen, wenn Zigbee2MQTT den neuen Wert vom Thermostat meldet; sonst sagt die App, dass es nicht
  geantwortet hat.
- **Heizpläne:** Die App schreibt den Plan des Raums in Plan 1 des Thermostats. Zwischen den Zeitfenstern hält das
  Thermostat seinen Frostschutz – wie die AeroFlow. Es kann höchstens 12 Wechsel am Tag; passt ein Plan nicht, sagt
  die App das vorher und sendet nichts.
- **Kalibrieren** geht von Hand, in Schritten von 0,2 °C bis ±10 °C. Den Kalibrierlauf mit der Shelly Pill gibt es
  nur für AeroFlow-Heizungen: Das Thermostat meldet seine Temperatur nur, wenn sie sich um 0,5 °C ändert.
- **Batterie:** Bei 20 % oder weniger – oder wenn das Thermostat selbst warnt – steht auf der Startseite „Batterie im
  Thermostat bald wechseln“.
- **Erreichbar** ist ein Thermostat, solange es sich in den letzten 2 Stunden gemeldet hat. Nach 30 Minuten Stille
  fragt die App einmal nach seiner Temperatur. Schweigt es länger, hilft meist ein Druck auf die mittlere Taste.
- Die Uhr der Thermostate stellt Zigbee2MQTT.
- **Sanft regeln** (ab 0.4.9, *Einrichtung › Geräte*, Zeile „Regelung“): Ab Werk öffnet das Thermostat das Ventil ganz,
  bis die Temperatur erreicht ist, und schließt es dann ganz. „Sanft regeln“ schaltet seine eigene „smarte
  Regelung“ ein: Es öffnet das Ventil nur so weit wie nötig – wie ein normaler Thermostatkopf. „Wie ab Werk“ schaltet
  sie wieder aus. Hat das Thermostat noch nicht gesagt, wie es regelt, fragt die App es einmal danach.
- **Firmware einspielen** (ab 0.4.9, Zeile „Firmware“): Liegt bei Zigbee2MQTT eine neue Firmware bereit, spielt der
  Knopf sie über Funk ein. Das dauert 10 bis 100 Minuten; das Thermostat heizt dabei weiter und startet am Ende kurz
  neu. Die Seite zeigt, wie weit es ist. Es geht immer nur ein Thermostat zur Zeit und nur mit mindestens 70 %
  Batterie, wie Zigbee2MQTT es rät.

## Protokoll: wenn etwas schiefgeht

Ab Version 0.4.5 schreibt die App mit, was passiert: wann eine Heizung oder ein Fühler nicht antwortet und wann wieder, was in der App
eingestellt wurde (Temperatur, Modus, Heizplan, Uhren, Räume, PIN), was sich an einer Heizung selbst geändert hat (am
Display oder durch den Plan), wie Update, WLAN, Fernwartung und Zigbee laufen, jeden Start – und ob die App vorher
sauber beendet wurde oder etwa der Strom weg war.

- **Protokoll speichern** – unten in der Heizungsansicht (ohne PIN, damit die Bewohner es können) und in
  *Einrichtung › Über*: Die App stellt eine ZIP-Datei zusammen, der Browser speichert sie (meist im Ordner
  „Downloads“). Ist in der Einrichtung eine E-Mail-Adresse eingetragen, bietet die Seite danach an, eine E-Mail an sie
  vorzubereiten – mit Betreff und kurzem Text. Die Datei hängen die Bewohner selbst an; das kann ein Browser nicht.
- **In der Datei:** `LIESMICH.txt`; `uebersicht.txt` (Version, Heizungen mit Zustand, Räume, Pläne, Fühler, WLAN,
  Fernwartung, Zigbee, die letzten Warnungen); `app.log` (das Protokoll der App, ältester Eintrag zuerst);
  `system.txt` (nur auf dem Pi: das Journal seiner Dienste aus den letzten 7 Tagen, Kernel, Neustarts, Netz,
  USB-Geräte, Speicherkarte, Temperatur und Unterspannung); `daten.json` (Räume, Pläne, Namen – ohne PIN).
- **Nicht in der Datei:** Tokens der Heizungen, Passwörter (WLAN, App, Mosquitto) und der Anmelde-Link von Tailscale.
  Sie werden schon beim Schreiben jeder Zeile durch `•••` ersetzt und beim Zusammenstellen noch einmal. **Wohl aber**
  Namen von Räumen und Geräten, Adressen im Heimnetz und WLAN-Namen – darauf weist der Dialog hin.
- **Ausführliches Protokoll** (*Einrichtung › Über*), für Fehler, die nur ab und zu auftreten: schreibt 24 Stunden
  lang zusätzlich jede Abfrage der Heizungen und Fühler und jeden Aufruf der Seite mit und schaltet sich dann von
  selbst ab, auch wenn die App zwischendurch neu startet.
- **Wo es liegt:** `logs/flowthermpi.log` neben `data.json` (auf dem Pi in `/var/lib/flowthermpi/data/logs/`). Ist die
  Datei 2 MB groß, beginnt eine neue; die zehn neuesten bleiben, zusammen höchstens etwa 20 MB. Anderer Ordner:
  `--log-dir` bzw. `HEIZAPP_LOG_DIR`. Jede Zeile beginnt mit Datum, Uhrzeit (mit Millisekunden und Zeitzone), Stufe und
  dem Teil der App. Auf der Konsole bzw. im Journal (`journalctl -u flowthermpi`) stehen dieselben Meldungen in kurz,
  ohne die ausführlichen.
- **Die Systemprotokolle des Pi** darf die App nicht selbst lesen. Wie beim Update-Knopf legt sie nur eine Anfrage in
  ihren Datenordner; der Dienst `flowthermpi-logs` (root) sammelt dann genau das oben Genannte. Er ersetzt die
  Geheimnisse, die er auf dem Pi findet (Tokens aus `heizungen.conf`, WLAN-Passwörter von NetworkManager, die
  Passwörter für Mosquitto, `HEIZAPP_PASSWORD`), und alles, was wie eines aussieht. Das Ergebnis kann nur die App
  lesen; sie löscht es, sobald es in der Datei ist. Vom Datenordner nennt er nur Namen und Größen. Ob das Journal
  Neustarts übersteht, steht in `system.txt` („dauerhaft gespeichert“ oder „nur seit dem letzten Start“). Einen
  älteren Pi bringt der Update-Knopf auf diesen Stand; auf dem Mac oder in Docker fehlt dieser Teil, dann steht in
  `system.txt`, warum. Meldungen: `journalctl -u flowthermpi-logs`.

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
beschriebene Verhalten nachbildet – die echten Heizungen werden dabei nicht angesprochen. Die
WLAN-Einrichtung läuft gegen einen nachgebauten NetworkManager (`FakeNM` in `tests/test_install.py`), die
Fernwartung gegen ein nachgebautes Tailscale (`FakeTS`, ebenda) – ohne Download, ohne Tailscale-Konto. Die
Raumfühler und der Kalibrierlauf laufen gegen nachgebaute Shelly Pills (`tests/fake_shelly.py`). Die Zigbee-Programme
werden gegen einen nachgebauten Pi installiert (`FakePi` in `tests/test_zigbee.py`: apt, Mosquitto, Node.js, pnpm,
systemd) – ohne Download; die „Downloads“ sind kleine Archive, die der Test selbst baut, mit echten Prüfsummen. Für das
Anlernen und Steuern gibt es ein nachgebautes Mosquitto und Zigbee2MQTT mit Thermostaten (`tests/fake_mqtt.py`) – ohne
Stick und ohne echtes Thermostat; es prüft und beantwortet Befehle so, wie es die Gerätedefinition des TRV-ZBT in
Zigbee2MQTT vorgibt, und spielt ein Firmware-Update so, wie die Anleitung von Zigbee2MQTT es beschreibt. Wie sich das
echte Thermostat verhält, prüfen die Tests nicht. Das Einsammeln der
Systemprotokolle läuft gegen einen nachgebauten Pi (`FakePi` in `tests/test_logs.py`), dessen Ausgaben erfundene
Passwörter und Tokens enthalten – geprüft wird, dass keines davon in der Datei landet.

## Ideen für später

- Paket für das NAS (Docker); feste Versionen (Git-Tags) für den Update-Knopf; automatische Updates.
- Licht und Sensoren über die Philips-Hue-Bridge; Regelung über einen Raumfühler.
- Verlauf und Kosten (Heizdauer × Nennleistung × Strompreis).
- Heizungen zusätzlich in Apple Home einbinden.

## Lizenz

Copyright © 2026 antratec

FlowThermPi ist freie Software unter der **GNU General Public License, Version 3**
(siehe [LICENSE](LICENSE)): Sie darf genutzt, verändert und weitergegeben werden – wer eine
veränderte Version weitergibt, muss deren Quellcode ebenfalls unter dieser Lizenz offenlegen.
FlowThermPi wird ohne jede Gewährleistung bereitgestellt.

Die Schrift Atkinson Hyperlegible Next (Braille Institute; lateinischer Teil, von Google Fonts) in `web/fonts/` steht
unter der SIL Open Font License 1.1 (siehe [web/fonts/OFL.txt](web/fonts/OFL.txt)).
