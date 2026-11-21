# FlowThermPi – lokale Steuerung für Thermotec AeroFlow (FlexiSmart Pro)

Kleine Web-App, die Thermotec-Elektroheizungen mit **FlexiSmart-Pro-Regler** direkt im
Heimnetz steuert – ohne Cloud, ohne Konto, ohne Zusatzpakete (nur Python-Standardbibliothek). Nur die
freiwillige [Fernwartung](#fernwartung-tailscale) nutzt Tailscale.

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
- **Fernhilfe erlauben** (ganz unten, wenn die [Fernwartung](#fernwartung-tailscale) eingerichtet ist): Dann lässt
  sich die App zwei Stunden lang aus der Ferne öffnen, oben steht „Fernhilfe ist an – bis 16:30 Uhr“ mit **Beenden**.

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
- **WLAN** (nur auf dem Raspberry Pi): Netze in der Nähe suchen, mit Passwort verbinden, gespeicherte
  WLANs vergessen – siehe [WLAN einrichten](#wlan-einrichten).
- **Fernwartung** (nur auf dem Raspberry Pi): den Pi mit Tailscale aus der Ferne erreichbar machen, um einem
  Haushalt zu helfen, ohne hinzufahren – von sich aus nur, solange die Bewohner es erlauben. Siehe
  [Fernwartung](#fernwartung-tailscale).
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
  `--reset-pin` (PIN vergessen), `--uninstall` (Daten bleiben), `--uninstall --purge` (alles weg).
- **Meldungen:** `journalctl -u flowthermpi -f`. Passwort fürs Heimnetz: `HEIZAPP_PASSWORD=…` in
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


## Raumfühler suchen

In der Einrichtung unter „Geräte“ → Untertab „Fühler“ → **Fühler suchen** startet einen Suchmodus
für Shelly-Fühler (z. B. **Shelly H&T Gen3**, ab Shelly-Generation 2). Der Fühler muss schon im WLAN
sein (Einrichtung über seinen Hotspot `ShellyHTG3-…` und `http://192.168.33.1`). Er schläft fast immer
und antwortet nur, solange er wach ist: Während der Suche den Knopf hinter der Rückwand **kurz**
drücken (5 s = Hotspot, 10 s = Werkseinstellungen). Die App fragt dafür alle 2 Sekunden jede Adresse
des Heimnetzes nach `GET /shelly` (rund 3 s für ein /24-Netz) und liest gefundene Fühler einmal mit
`Shelly.GetStatus` aus: Temperatur, Luftfeuchte, Batterie oder USB. Es wird nur gelesen; nach
3 Minuten endet der Suchmodus von selbst.

**Speichern und Raum:** Bei einem gefundenen Fühler **Übernehmen** drücken, optional einen Namen
eintragen, einen Raum wählen, speichern. Das klappt auch noch, wenn der Fühler schon wieder schläft:
Die App nimmt die Daten aus der Suche (bis 30 Minuten danach). Gespeicherte Fühler stehen unter
„Raumfühler“ (Raum direkt umstellbar, „Bearbeiten“ für Name und Entfernen) und auf der Raumkachel
mit ihrem letzten Messwert. Neue Messwerte und eine neue IP übernimmt die App bei jeder Suche, in
der der Fühler wach ist; laufend empfängt sie noch keine Werte.

**Aktualisieren** (bei jedem gespeicherten Fühler) liest ihn sofort aus – unter seiner letzten Adresse,
sonst sucht die App ihn im Heimnetz. Schläft er, wartet die Seite bis zu einer Minute und bittet darum,
den Knopf hinter der Rückwand kurz zu drücken; sobald er aufwacht, stehen die neuen Werte da.

**Kalibrierung – Vergleich:** Ist der Fühler einem Raum zugeordnet, zeigt er unter „Kalibrierung“ seine
Isttemperatur neben der Isttemperatur jeder Heizung des Raums und die Abweichung (Heizung − Fühler).
Beide Werte stammen vom selben Zeitpunkt: Bei jedem Auslesen des Fühlers (Suche, „Aktualisieren“)
merkt sich die App die Raumtemperatur der Heizungen dieses Moments. Bis ±0,5 °C gilt als „passt“,
sonst „zeigt zu viel“ oder „zeigt zu wenig“. Die Heizung meldet ihren Wert schon mit ihrer Korrektur.

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
Fernwartung gegen ein nachgebautes Tailscale (`FakeTS`, ebenda) – ohne Download, ohne Tailscale-Konto.

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
