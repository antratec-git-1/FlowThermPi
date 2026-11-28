# HoLiTherm – Licht und Heizung steuern, Strom im Blick

<img src="web/logo.jpg" width="300" alt="HoLiTherm-Logo: Licht, Heizung, Power">

Kleine Web-App, die Licht und Heizung direkt im Heimnetz steuert und zeigt, wie viel Strom die Geräte verbrauchen:
Thermotec-Elektroheizungen mit **FlexiSmart-Pro-Regler**, Heizkörper-Thermostate, Raumfühler, Lampen, Schalter,
Bewegungsmelder und Steckdosen mit Strommessung – ohne Cloud, ohne Konto, ohne Zusatzpakete (nur
Python-Standardbibliothek). Nur die
freiwillige [Fernwartung](#fernwartung-tailscale) nutzt Tailscale, und die
[Zigbee-Thermostate](#zigbee-thermostate-in-arbeit) (in Arbeit) brauchen Zusatzprogramme, die die App auf Knopfdruck
installiert.

Inoffizielles Projekt – kein Produkt von Thermotec, SONOFF, Shelly, IKEA, Philips Hue, Raspberry Pi oder anderen
Herstellern, deren Geräte es nutzt, und von keinem dieser Hersteller unterstützt. Die genannten Marken gehören ihren
Inhabern.

Bis Version 0.4.25 hieß das Projekt FlowThermPi. Ordner, Dienste und Netznamen auf dem Pi (`flowthermpi`) und
die Adresse auf GitHub heißen noch so, damit Updates auf vorhandenen Geräten weiter funktionieren.

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
- **Raumtemperatur und Geräte** (ab 0.4.11): Die große Zahl („im Raum“) kommt vom [Fühler](#raumfühler-shelly-pill)
  des Raums, wenn er in den letzten 15 Minuten gemessen hat (bei mehreren Fühlern ihr Mittel); sonst ist sie das
  Mittel der erreichbaren Heizungen. Hat der Raum mehr als ein Gerät, steht unten auf der Kachel eine kleine Liste mit
  **Ist** und **Soll** jedes Geräts: Heizungen, Thermostate und Fühler (beim DHT22 mit Luftfeuchte). Ausgeschaltete
  Geräte haben „aus“, Fühler kein Soll. Ein Fühler ohne frischen Messwert fehlt in der Liste. Ab 0.4.12 steht vor
  jedem Gerät ein Symbol (Heizkörper oder Thermometer) und unter jeder Heizung, wie weit sie vom Fühler abweicht, etwa
  „+1,2 °C zum Fühler“ – gelb, wenn es mehr als ±0,5 °C sind.
- **Verlauf** (ab 0.4.13): Unten auf jeder Kachel steht, wie lange der Raum heute geheizt hat, etwa „Heute 2 Std.
  10 Min. geheizt“. Ein Tipp darauf öffnet den **Verlauf** des Tages: die Raumtemperatur als Linie, das Eingestellte
  gestrichelt, darunter warme Balken, wann geheizt wurde, blau „Fenster offen“ und schraffiert, wo es keine Werte
  gibt. Mit ‹ › geht es zu den Tagen davor; ein Tipp auf die Kurve zeigt die Werte dieser fünf Minuten, „Stunde für
  Stunde“ dasselbe als Tabelle. Strom und Kosten („etwa 0,90 €“) stehen nur dabei, wenn sie in der Einrichtung
  eingeschaltet sind – siehe [Verlauf und Kosten](#verlauf-und-kosten).
- **Strom heute** (ab 0.4.19, nur wenn in der Einrichtung eingeschaltet – ab Werk aus): unter den Kacheln eine Zeile
  „Strom heute 4,2 kWh · etwa 1,30 € · gemessen an 3 Steckdosen“ – was alle
  [Steckdosen mit Strommessung](#strom-was-jedes-gerät-braucht) heute gezählt haben.
- **Steckdosen auf den Kacheln** (ab 0.4.20): Unten auf jeder Raum-Kachel stehen ihre Steckdosen mit Strommessung –
  Name, was daran hängt, und „an“, „aus“ oder „keine Verbindung“. Zum Raum gehört eine Steckdose, wenn ihre Heizung im
  Raum hängt oder sie dem Raum zugeordnet ist (*Geräte › Steckdosen*). Ein Raum ohne Heizung, aber mit Steckdose (etwa
  der Keller mit der Waschmaschine) bekommt eine eigene Kachel. Mit dem Schalter „Strom der Steckdosen auch auf der
  Startseite zeigen“ (ab Werk aus) steht darunter „jetzt 240 W · heute 0,8 kWh“, bei Waschmaschine und Trockner
  „läuft“ und „seit 23 Min.“.
- **„Fertig“** (ab 0.4.20): Ist die Waschmaschine oder der Trockner fertig, steht oben „Die Waschmaschine ist fertig –
  seit 10:42.“ mit **OK** (ohne PIN). Siehe [Wäschen und Trockengänge](#wäschen-und-trockengänge).
- **Licht** (ab 0.4.21): Hat ein Raum [Zigbee-Lampen](#licht-zigbee-lampen), steht unten auf seiner Kachel „Licht“
  mit einem großen Knopf **Licht einschalten** bzw. **Licht ausschalten** für alle Lampen des Raums. Ist es an,
  stehen darunter **dunkler** und **heller** mit der Helligkeit, etwa „60 %“ – in Schritten von 10 %, nie unter 10 %:
  ganz aus geht nur mit „Licht ausschalten“. Bei mehreren Lampen steht jede darunter mit „an · 60 %“ oder „aus“ und
  ihrem eigenen Knopf. Ein Raum ohne Heizung, aber mit Lampe (etwa der Flur) bekommt eine eigene Kachel. Antwortet
  eine Lampe nicht, steht dort „… antwortet nicht. Ist der Lichtschalter an der Wand an?“. Alles ohne PIN.
  **Einstellen ›** (ab 0.4.22) öffnet ein Fenster mit jeder Lampe des Raums für sich: an und aus, **Helligkeit**,
  **Lichtfarbe** von Kerzenlicht bis Tageslicht und bei Farblampen **Farbe** – nur, was die Lampe kann. Siehe
  [Einstellen je Lampe](#licht-zigbee-lampen). Ab 0.4.23 schalten auch [Funk-Schalter](#licht-schalter) das Licht
  eines Raums; ist die Batterie eines Schalters fast leer, steht oben „Die Batterie im Schalter „Flur“ ist fast leer
  (12 %) – bitte bald wechseln.“ mit **OK** (ohne PIN). Ab 0.4.24 schaltet auch ein
  [Bewegungsmelder](#licht-bewegungsmelder) das Licht.
- **Kacheln zum Aufklappen** (ab 0.4.24): Ein Tipp auf den Namen eines Raums klappt seine Kachel zu oder auf.
  Zugeklappt zeigt sie den Namen, die Temperatur und eine Zeile mit kleinen Schildern, etwa „Soll 21 °C“, „heizt“,
  „Licht an“, „Bewegung vor 5 Min.“; Warnungen wie „Fenster offen“, „Steckdose aus“ oder „Batterie bald wechseln“
  stehen gelb, „keine Verbindung“ rot. Bei bis zu zwei Kacheln sind sie anfangs offen, bei mehr zu; jedes Handy merkt
  sich, was dort auf- und zugeklappt ist. Ab drei Kacheln steht darüber **Alle aufklappen** bzw. **Alle zuklappen**.
- **Bewegung** (ab 0.4.24): Hat ein Raum einen [Bewegungsmelder](#licht-bewegungsmelder), steht auf seiner Kachel
  „Bewegung gerade“ oder „Bewegung vor 5 Min.“ und beim Licht „An durch Bewegung · aus um 21:45 Uhr“ oder „Geht bei
  Bewegung an“. Ist die Batterie eines Melders fast leer, steht oben „Die Batterie im Bewegungsmelder „Flur“ ist fast
  leer (12 %) – bitte bald wechseln.“ mit **OK**.
- **Steckdose aus** (ab 0.4.14): Hängt eine AeroFlow an einer Steckdose mit Strommessung
  ([Zigbee](#zigbee-steckdosen-mit-strommessung) oder [WLAN](#wlan-steckdosen-mit-strommessung)) und hat jemand die
  Steckdose an ihrer Taste ausgeschaltet, sagt die Kachel „Die Steckdose ist aus – die Heizung hat
  keinen Strom.“ **Steckdose einschalten** schaltet sie wieder ein (ohne PIN). Ausschalten lässt sie sich nur in der
  Einrichtung (ab 0.4.18, *Geräte › Steckdosen* › **Schalten**, zum Testen).
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
  (Solltemperatur, Manuell / Automatik / Boost / Aus). Ab 0.4.25 ist der Tab „Räume“ eine **Liste**: je Raum Name,
  Temperatur und kleine Schilder – was er hat („2 Heizungen“, „Fühler“, „1 Steckdose“, „Licht · 2 Lampen“,
  „1 Schalter“, „Bewegungsmelder“) und was nachzusehen ist („keine Verbindung“ rot, „Batterie bald wechseln“ gelb).
  Ganz unten „Ohne Raum“ mit den Geräten, die noch keinen haben; oben **+ Raum**. Ein Tipp öffnet die
  **Raum-Ansicht** mit allem, was zum Raum gehört:
  - oben „‹ Alle Räume“ (auch die Zurück-Taste des Handys führt zur Liste), der Name mit **Bearbeiten** (Name, Farbe,
    die Heizungen des Raums ankreuzen – eine Heizung aus einem anderen Raum wechselt dabei –, „Raum löschen“) und
    **+ Gerät hinzufügen**: Bei „Wohin?“ ist der Raum schon gewählt, nach „Fertig“ geht es zurück in den Raum;
  - die Abschnitte **Heizen**, **Raumfühler**, **Steckdosen** und **Licht** – jeder mit seiner Anzahl, nur wenn der
    Raum so ein Gerät hat, und zum Zuklappen (das Handy merkt es sich). Darin stehen dieselben Karten wie unter
    *Geräte*;
  - unter Heizen: Solltemperatur, Modus, Heizplan mit **Senden**, die Liste der Geräte mit **Ist** und **Soll**
    (Heizungen und Thermostate mit Hinweisen wie Automatik, Boost, „Fenster offen“ oder „Batterie schwach“ und der
    Abweichung zum Fühler, dann die Fühler mit ihrer letzten Messung) und **Verlauf**. Die große „Raumtemperatur“
    kommt wie auf der Startseite vom Fühler, wenn er frisch gemessen hat. Kommt eine Heizung neu in einen Raum, steht
    da, dass der Heizplan noch gesendet werden muss;
  - unter Licht: Lampen, Schalter, Bewegungsmelder und „Licht bei Bewegung“.

  Wählt man an einem Gerät einen anderen Raum, verschwindet es aus der Ansicht, und eine Meldung sagt, wo es jetzt
  ist. Ein Raum ohne Heizung bekommt keine Heizknöpfe. Der Tab *Geräte* bleibt: alle Geräte nach Art.
- **Farbe je Raum** (ab 0.4.25): unter **Bearbeiten** eine von 24 Farben oder mit **Eigene Farbe …** jede andere. Sie
  steht als Streifen in der Raumliste, als Punkt vor dem Namen in der Raum-Ansicht und als schmaler Streifen links an
  der Kachel des Raums auf der Startseite. Ab Werk hat kein Raum eine Farbe (`data.json`: `rooms.<id>.color`;
  Schnittstelle: `color` bei `POST /api/rooms` und `PATCH /api/rooms/<id>`, `"#rrggbb"` oder `null`).
- **Strom** (Tab, ab 0.4.19): was jedes Gerät an einer Steckdose mit Strommessung braucht und kostet – Tag, Woche
  oder Monat, mit der Woche bzw. dem Monat davor, dazu die Bereitschaft (Standby) jedes Geräts. Siehe
  [Strom: was jedes Gerät braucht](#strom-was-jedes-gerät-braucht).
- **Strom und Kosten** (ab 0.4.13 im Tab „Räume“, ab 0.4.19 im Tab „Strom“, jeweils ganz unten): einen Strompreis für
  den Haushalt (ct/kWh) und die Leistung jeder AeroFlow in Watt eintragen (steht auf dem Typenschild); ein Schalter
  zeigt Strom und Kosten der Heizungen auch auf der Startseite (ab Werk aus), ab 0.4.19 ein zweiter den Strom der
  Steckdosen (ab Werk aus; „Strom heute“, ab 0.4.20 auch Watt und kWh an den Kacheln). Jeder Raum zeigt unter „Heizen“ „Heute … geheizt · etwa … kWh · … €“ und hat den Knopf
  **Verlauf**. Siehe [Verlauf und Kosten](#verlauf-und-kosten). Hängt eine AeroFlow an einer Steckdose mit
  Strommessung ([Zigbee](#zigbee-steckdosen-mit-strommessung) oder [WLAN](#wlan-steckdosen-mit-strommessung), ab
  0.4.14), steht hier, was sie misst, und **Übernehmen** trägt die beim Heizen gemessene Leistung ein. Welche
  Steckdose zu welcher Heizung gehört, wählt man ab 0.4.15 an der Steckdose (Tab „Geräte“ → „Steckdosen“).
- **Heizpläne:** in der App verwalten (bis zu 10 Zeitfenster pro Tag, Viertelstunden-Raster)
  und an die Heizungen eines Raums **senden**. Die Heizung führt den Plan im Modus
  „Automatik“ selbst aus – auch wenn die App aus ist.
- **Kalibrieren** (Tab „Geräte“, je Heizung): gemessene Raumtemperatur eintragen – die App setzt die
  Temperatur-Korrektur der Heizung (±9,9 °C) so, dass sie danach diesen Wert anzeigt und danach
  regelt. Verglichen wird mit dem Mittel der Heizung über die letzten 30 Minuten (sobald die App
  mindestens 5 Minuten läuft), sonst mit dem aktuellen Wert. „Korrektur auf 0 setzen“ nimmt sie zurück.
  Richtig messen: Thermometer in der Raummitte auf 1–1,3 m Höhe, Raum seit 1–2 Stunden auf
  Solltemperatur, über 30–60 Minuten mehrmals ablesen und mitteln.
- **Raumfühler** (Tab „Geräte“ → „Raumfühler“): Shelly Pills im Heimnetz suchen, ihre Fühler einem Raum zuweisen und
  einstellen, was in der Pill steckt (DS18B20 oder DHT22). Der Fühler zeigt seine Temperatur neben der der Heizungen
  seines Raums – so sieht man, ob eine Heizung kalibriert werden sollte. Ein **Kalibrierlauf** (zum Beispiel einen Tag
  lang) schlägt dann je Heizung die passende Korrektur vor. Siehe [Raumfühler: Shelly Pill](#raumfühler-shelly-pill).
- **Steckdosen** (Tab „Geräte“ → „Steckdosen“, ab 0.4.14): alle Steckdosen mit Strommessung mit an/aus, Watt und
  „heute … kWh“; an jeder Steckdose wählt man, was an ihr hängt (**Angeschlossen**, ab 0.4.19), ihre Heizung (ab 0.4.15)
  oder – ohne Heizung – ihren Raum (ab 0.4.17) und kann sie entfernen. Ab 0.4.18 benennt **Ändern …** beim Namen sie um, und **Schalten** schaltet sie zum Testen
  ein und aus. **Viertelstunden ansehen** zeigt ihren Strom je Viertelstunde, auch als Tabelle für Excel
  (ab 0.4.17, siehe [Strom der Steckdosen](#strom-der-steckdosen-in-viertelstunden)). Siehe
  [WLAN-Steckdosen](#wlan-steckdosen-mit-strommessung) und [Zigbee-Steckdosen](#zigbee-steckdosen-mit-strommessung).
- **Gerät hinzufügen** (Tab „Geräte“, ab 0.4.15): ein Knopf **+ Gerät hinzufügen** für Heizung, Thermostat, Raumfühler
  und Steckdose, in vier Schritten: *Was?* · *So geht's* (eine kurze Anleitung; die App sucht dabei selbst – im WLAN,
  über Zigbee oder bei einer Steckdose in beidem) · *Gefunden* (**Übernehmen**; ein Zigbee-Gerät meldet sich von
  selbst) · *Wohin?* (der Raum, bei einer Steckdose ab 0.4.19 erst, was an ihr hängt, dann ihre Heizung oder – ohne
  Heizung – ihr Raum). Eine AeroFlow lässt
  sich weiter von Hand mit
  Adresse und Token eintragen. Jedes Gerät trägt das Schild „WLAN“ oder „Zigbee“; Raum, Heizung, Firmware und
  **Entfernen …** stehen direkt beim Gerät.
- **Geräte-Liste filtern:** Im Tab „Geräte“ nach Raum filtern (Alle / je Raum / Nicht zugeordnet,
  jeweils mit Anzahl); die Auswahl merkt sich die App.
- **Heizplan aus Heizung auslesen** (Tab „Geräte“): liest alle drei Planplätze einer Heizung
  und übernimmt jeden belegten, noch unbekannten Plan als neuen Heizplan in die App (z. B. Pläne
  aus SmartHeat4U oder vom Display). Minutengenaue Zeiten bleiben erhalten; Duplikate werden
  erkannt. Funktioniert auch ohne Token.
- **Uhren:** werden automatisch gestellt (beim Start, bei > 1 min Abweichung, täglich 3 Uhr –
  deckt Sommer-/Winterzeit ab). Ohne Internet stellt der Regler sie nicht selbst um.
- **Verbindungen** (Tab, nur auf dem Raspberry Pi, ab 0.4.15): **Heimnetz**, **Zigbee-Funk** und **Fernwartung** an
  einem Ort – bis 0.4.14 waren das die drei Tabs „WLAN“, „Zigbee“ und „Fernwartung“. Geräte stehen hier keine.
- **WLAN** („Verbindungen“ → „Heimnetz“): Netze in der Nähe suchen, mit Passwort verbinden, gespeicherte
  WLANs vergessen – siehe [WLAN einrichten](#wlan-einrichten).
- **Fernwartung** („Verbindungen“ → „Fernwartung“): den Pi mit Tailscale aus der Ferne erreichbar machen, um einem
  Haushalt zu helfen, ohne hinzufahren – von sich aus nur, solange die Bewohner es erlauben. Siehe
  [Fernwartung](#fernwartung-tailscale).
- **Zigbee** (in Arbeit): unter „Verbindungen“ → „Zigbee-Funk“ die Programme für SONOFF-Zigbee-Thermostate
  installieren, aktualisieren und entfernen und Zigbee mit einem Funkkanal einrichten; Thermostate unter „Geräte“
  anlernen und wieder entfernen. Angelernte Thermostate sind Heizungen der App wie die AeroFlow: Raum, Temperatur,
  Ein/Aus, Boost, Heizplan, Kalibrierung, dazu drei Arten zu regeln (Ab Werk, Sanft, Stufen) und neue Firmware per
  Knopf – siehe [Zigbee-Thermostate](#zigbee-thermostate-in-arbeit).
- **Protokoll** (Tab „Über“): die E-Mail-Adresse des Helfers eintragen, das ausführliche Protokoll für 24 Stunden
  einschalten, selbst ein Protokoll speichern – siehe [Protokoll](#protokoll-wenn-etwas-schiefgeht).
- **IP-Adressen** (ab 0.4.17): Jede angezeigte IP-Adresse ist ein Link und öffnet die Seite des Geräts (Shelly, Pill,
  Router …) in einem neuen Tab. Netze wie „192.168.1.0/24“ sind keine Links.
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

Das Skript lädt HoLiTherm von GitHub nach `/opt/flowthermpi`, legt den Dienst `flowthermpi` an
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
*Einrichtung › Verbindungen › Heimnetz* (bis 0.4.14 *Einrichtung › WLAN*):

1. Netzwerkkabel am Pi einstecken, die App öffnen (z. B. `http://flowthermpi.local`), *Einrichtung › Verbindungen ›
   Heimnetz*.
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
- Voraussetzung: Raspberry Pi OS ab Bookworm (NetworkManager). Die WLAN-Einrichtung gibt es ab Version 0.3.0; ein
  Pi mit 0.2.0 bekommt sie über den Update-Knopf.

## Fernwartung (Tailscale)

Damit man einem Haushalt helfen kann, ohne hinzufahren: Unter *Einrichtung › Verbindungen › Fernwartung* (bis 0.4.14
*Einrichtung › Fernwartung*) kommt der Pi in das eigene private Netz bei [Tailscale](https://tailscale.com). Darüber öffnet man die App von überall, ohne Freigaben
im Router. Die Fernwartung ist freiwillig; ohne sie bleibt alles im Heimnetz. Und auch mit ihr ist der Pi von sich
aus nur erreichbar, solange die Bewohner es erlauben – ähnlich wie bei einer Fernwartungs-Software für den PC.

**Vorher, einmal:** ein eigenes Tailscale-Konto anlegen (z. B. mit dem GitHub-Login), Tailscale auf dem eigenen
Mac oder Handy installieren und das [Tailscale-Netz einrichten](#tailscale-netz-einrichten-einmal-vor-dem-ersten-pi).
Der Tarif *Personal* ist kostenlos und für private Nutzung gedacht (Stand November 2026: bis 6 Nutzer, bis 50
getaggte Geräte; aktuelle Bedingungen auf [tailscale.com/pricing](https://tailscale.com/pricing)).

1. Im Heimnetz die App öffnen, *Einrichtung › Verbindungen › Fernwartung › Fernwartung einrichten*. Der Pi lädt Tailscale aus
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
  **Beenden** – das geht auch aus der Ferne, in der Heizungsansicht oder unter *Verbindungen › Fernwartung*.
- Nach zwei Stunden schaltet die App Tailscale von selbst aus (`tailscale down`), auch nach einem Neustart der App
  oder des Pis – und ebenso, wenn jemand es auf dem Pi von Hand eingeschaltet hat.
- Erlauben und verlängern geht nur im Heimnetz, nicht aus der Ferne (über Tailscale). So kann sich niemand selbst
  mehr Zeit geben, als die Bewohner erlaubt haben.
- Die Fernhilfe schaltet nur ein, was eingerichtet ist: Sie installiert nichts und startet keine neue Anmeldung.
- *Verbindungen › Fernwartung › Wann ist der Pi erreichbar?:* **Nur nach Freigabe** (Vorgabe) oder **Immer** – dann
  bleibt die Fernwartung dauerhaft an, und in der Heizungsansicht steht „Fernwartung ist eingeschaltet“. Ändern lässt
  sich das nur im Heimnetz.
- Ein Pi, der schon vor Version 0.4.1 eingerichtet wurde, ist danach ebenfalls auf „Nur nach Freigabe“ und schaltet
  die Fernwartung gleich nach dem Update aus. Wer sie dort dauerhaft möchte, wählt im Heimnetz „Immer“.

### Was sonst noch gilt

- **Name:** Im Tailscale-Netz heißt der Pi `flowthermpi-` + **Gerätenummer** (die letzten 8 Zeichen seiner
  Seriennummer), damit sich mehrere Pis auseinanderhalten lassen. Die Gerätenummer steht unter *Einrichtung › Über*
  und *Verbindungen › Fernwartung*, am besten auch als Aufkleber auf dem Pi. Im Heimnetz bleibt er `flowthermpi.local`.
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
- Voraussetzung: Raspberry Pi OS (Debian) mit Internet. Die Fernwartung gibt es ab Version 0.4.0, die Fernhilfe ab
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

In der Einrichtung unter „Geräte“ → **+ Gerät hinzufügen** → „Heizung“ (bis 0.4.14 **Im Netz suchen**): Die App fragt
jede Adresse des Heimnetzes einmal nach ihrer Seriennummer – nur lesend, ohne Token, ein paar Sekunden für ein
/24-Netz – und listet alle Heizungen auf (Name `Heat4U-…`, IP, Raumtemperatur). Bekannte sind als „in der App“
markiert, neue haben **Übernehmen**: Das Formular hat die IP schon eingetragen und erklärt, wie man an den Token
kommt; danach fragt die App nach dem Raum. **von Hand eintragen** öffnet dasselbe Formular leer. Läuft die App in
Docker oder in einem anderen Netz, lässt sich das Heimnetz angeben (z. B. `192.168.178`; nur private Netze,
höchstens /22).

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

**Fühler suchen:** In der Einrichtung unter „Geräte“ → **+ Gerät hinzufügen** → „Raumfühler“. Die App fragt jede
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
sonst „zeigt zu viel“ oder „zeigt zu wenig“. Die Heizung meldet ihren Wert schon mit ihrer Korrektur. Ab 0.4.12
steht dieselbe Abweichung auch in den Gerätelisten der Räume (Startseite und *Einrichtung › Räume*), dort nur, solange
der Messwert des Fühlers höchstens 15 Minuten alt ist; mit mehreren Fühlern im Raum ihr Mittel.

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
*Einrichtung › Zigbee* (ab 0.4.15 *Verbindungen › Zigbee-Funk*) die Zusatzprogramme dafür auf Knopfdruck; ab 0.4.6 richtet die App Zigbee ein und lernt
Thermostate an; ab 0.4.8 sind angelernte Thermostate Heizungen der App (*Thermostate steuern*, unten); ab 0.4.9
regeln sie auf Wunsch sanft und bekommen neue Firmware per Knopf; ab 0.4.10 stellt die App danach ihre Einstellungen
wieder ein; ab 0.4.11 gibt es als dritte Art zu regeln die „Stufen“. Noch nicht dabei: Heizpläne aus einem Thermostat auslesen und die Shelly Pill als Fühler für ein
Thermostat.

- **Was der Pi lädt:**
  - **Mosquitto** aus dem Paketarchiv von Raspberry Pi OS. Es reicht die Nachrichten zwischen Zigbee2MQTT und der App
    weiter – nur auf dem Pi selbst (`127.0.0.1:1883`) und nur mit Passwort.
  - **Node.js 24** (offizielles Archiv von nodejs.org) und **pnpm** (von npm): darauf läuft Zigbee2MQTT bzw. damit
    wird es gebaut.
  - **Zigbee2MQTT** (Quellcode von GitHub, Bausteine von npm): spricht über den Stick mit den Thermostaten.
- **Feste Versionen, jede Datei geprüft:** Welche Versionen kommen, steht in `install.py` (`ZIGBEE_PARTS`): für
  Node.js und pnpm mit Prüfsumme, für Zigbee2MQTT mit der Git-Commit-Nummer. Die Bausteine prüft pnpm gegen die
  Prüfsummen im Lockfile von Zigbee2MQTT (`--frozen-lockfile`). Passt etwas nicht, wird nichts installiert. Neue
  Versionen kommen mit einem Update von HoLiTherm; dann steht unter „Zigbee-Funk“ **Neue Versionen installieren**.
- **Ablauf:** *Einrichtung › Verbindungen › Zigbee-Funk › Zigbee-Programme installieren*. Die Seite zeigt Schritt und Fortschritt; die
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
  verliert nur die Einstellungen von HoLiTherm. Die Daten von Zigbee2MQTT bleiben, damit angelernte Thermostate nach
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

**Thermostate und Steckdosen anlernen:**

- **Anlernen:** *Einrichtung › Geräte* › **+ Gerät hinzufügen** › „Thermostat“ oder „Steckdose“ (bis 0.4.14 **Geräte
  suchen** im Tab „Zigbee“, bis 0.4.13 „Thermostate suchen“). Die App öffnet das Zigbee-Netz von selbst für 3 Minuten,
  zählt herunter und sagt, was zu tun ist. Am Thermostat: Batterien einlegen, das Drehrad unter 5 °C drehen, bis es
  aus ist, dann die mittlere Taste 2 Sekunden drücken, bis das Verbindungszeichen blinkt. An einer
  [Steckdose](#zigbee-steckdosen-mit-strommessung): ihre Taste 5 Sekunden halten, bis das blaue Licht langsam blinkt.
  Nach etwa einer Minute steht das Gerät da („✓ angelernt“), und **Weiter** fragt nach Name und Raum (bei einer
  Steckdose: nach ihrer Heizung); ließ es sich nicht anlernen, steht dort **Entfernen …**. Mehrere Geräte gehen nacheinander in
  derselben Suche; klappt es nicht, das Gerät zum Anlernen näher an den Pi bringen. **Abbrechen** oder **Fertig**
  schließt das Zigbee-Netz wieder. Ist Zigbee noch nicht eingerichtet, führt der Ablauf zu „Verbindungen“ ›
  „Zigbee-Funk“.
- **Entfernen …** (unter „Geräte“, bei einem Thermostat oder einer Steckdose) meldet es im Zigbee-Netz ab. Antwortet es nicht (es
  schläft oder ist zu weit weg), bietet die Seite **Trotzdem entfernen** an: Dann vergisst der Pi es, ohne auf die
  Antwort zu warten. Eine entfernte Steckdose gehört danach auch zu keiner Heizung und keinem Raum mehr.
- **Wie die App mit Zigbee2MQTT spricht:** über Mosquitto (MQTT), nur auf dem Pi selbst und mit Passwort – mit einem
  kleinen eigenen MQTT-Baustein (`heizapp/mqtt.py`, nur Python-Standardbibliothek).
- Was weder Thermostat noch Steckdose der App ist (etwa ein Gerät, das sich nicht anlernen ließ), steht unter
  „Geräte“ in **Weitere Zigbee-Geräte**, ebenfalls mit **Entfernen …**.
- „Zigbee-Funk“ gibt es nur auf dem Pi (mit `install.py` eingerichtet); ein älterer Pi bekommt es über den
  Update-Knopf.

**Thermostate steuern** (ab 0.4.8):

- Jedes angelernte Thermostat steht unter *Einrichtung › Geräte* bei den Heizungen – mit Batterie, Funkqualität,
  Störungen und wann es sich zuletzt gemeldet hat. Dort bekommt es einen Namen und einen Raum. In `heizungen.conf`
  steht es nie; Name und Raum merkt sich die App in `data.json`, auch wenn es kurz aus dem Zigbee-Netz verschwindet.
  Neue kommen mit **+ Gerät hinzufügen** dazu (bis 0.4.14 „+ Heizung“).
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
- **Regelung** (*Einrichtung › Geräte*, Zeile „Regelung“ mit den Knöpfen „Ab Werk“, „Sanft“ und „Stufen“). Hat das
  Thermostat noch nicht gesagt, wie es regelt, fragt die App es danach (höchstens alle 30 Minuten).
  - **Ab Werk:** Das Thermostat öffnet das Ventil ganz, bis die Temperatur erreicht ist, und schließt es dann ganz.
  - **Sanft** (ab 0.4.9) schaltet seine eigene „smarte Regelung“ ein: Es öffnet das Ventil nur so weit wie nötig – wie
    ein normaler Thermostatkopf.
  - **Stufen** (ab 0.4.11): Es bleibt beim Wechsel zwischen auf und zu, aber sanfter; die Temperatur hält das
    Thermostat weiter selbst. **Stufe** 1–5 legt fest, wie weit das Ventil beim Heizen höchstens aufgeht (20, 40, 60,
    80 oder 100 %). **In Pausen** ist es ganz zu oder bleibt einen Spalt offen (5, 10 oder 15 %). **Spielraum** sagt,
    wie weit die Temperatur unter das Ziel fallen darf, bevor es wieder aufmacht: 1 °C wie ab Werk oder enger 0,6, 0,4
    oder 0,2 °C. Beim ersten Mal stellt die App Stufe 3 (60 %), Pausen 5 % und Spielraum 0,4 °C ein. Welche Werte am
    besten passen, muss man ausprobieren. Mit „Sanft“ gelten die Stufen nicht; wer danach wieder „Stufen“ drückt,
    bekommt die zuletzt eingestellten Werte. „Ab Werk“ stellt 100 %, zu und 1 °C wieder her.
- **Firmware einspielen** (ab 0.4.9, Zeile „Firmware“): Liegt bei Zigbee2MQTT eine neue Firmware bereit, spielt der
  Knopf sie über Funk ein. Das dauert 10 bis 100 Minuten; das Thermostat heizt dabei weiter und startet am Ende kurz
  neu. Die Seite zeigt, wie weit es ist. Es geht immer nur ein Thermostat zur Zeit und nur mit mindestens 70 %
  Batterie, wie Zigbee2MQTT es rät.
- **Nach dem Update** (ab 0.4.10): Das echte Thermostat hatte nach seinem Update „sanft“ vergessen und stand auf Aus.
  Deshalb merkt sich die App vor dem Einspielen Regelung (ab 0.4.11 auch die Stufen), Aus/Automatik/Ein, Heizplan,
  Korrektur und Frostschutz – auch was während des Updates in der App eingestellt wird – und stellt alles eine Minute
  nach dem Update wieder ein.
  Klappt das nicht, versucht sie es noch zweimal im Abstand von 5 Minuten; dann steht auf der Seite, was zu prüfen
  ist. Das Protokoll sagt, was das Thermostat nach dem Update gemeldet hat und was die App zurückgestellt hat.

## Zigbee-Steckdosen mit Strommessung

Ab 0.4.14. Die AeroFlow meldet keinen Verbrauch. Eine Zigbee-Steckdose mit Strommessung zwischen Wanddose und Heizung
misst, was die Heizung wirklich zieht. Das geht nur bei Heizungen mit Stecker, nicht bei Festanschluss.

- **Welche:** gebaut für die SONOFF S60ZBTPF (16 A, Schuko, etwa 14 €, Stand 25.11.2026). Andere gehen auch, wenn Zigbee2MQTT sie mit
  einem Schalter und einer Leistungsmessung (`power`) kennt; Steckdosenleisten mit mehreren Schaltern nicht.
- **Anlernen:** *Einrichtung › Geräte* › **+ Gerät hinzufügen** › „Steckdose“ (bis 0.4.14 unter *Einrichtung › Zigbee*
  mit **Geräte suchen**), an der Steckdose die Taste 5 Sekunden halten. Sie heißt dann „Steckdose“ mit den letzten
  vier Zeichen ihrer Adresse und steht unter *Geräte › Steckdosen* – mit an/aus, Watt, „heute … kWh“ und ihrer
  Heizung. Eine Steckdose ist keine Heizung; bei den Heizungen steht sie nicht.
- **Zuordnen:** im letzten Schritt „Wohin?“ oder später an der Steckdose unter *Geräte › Steckdosen* die Heizung
  wählen (bis 0.4.14 unter *Räume › Strom und Kosten*; ab 0.4.19 bei „Angeschlossen“ erst „Heizung“). Nur AeroFlow-Heizungen, jede Steckdose höchstens bei einer
  Heizung und jede Heizung an höchstens einer Steckdose. Dabei stellt die App die Steckdose so ein, dass sie nach einem
  Stromausfall wieder angeht – sonst bliebe die Heizung kalt. Ist sie gerade nicht zu erreichen, sagt die App das;
  dann die Heizung später noch einmal wählen. Ab 0.4.17 kann eine Steckdose ohne Heizung auch nur einem Raum gehören
  (etwa mit einem anderen Gerät daran) – siehe [Strom der Steckdosen](#strom-der-steckdosen-in-viertelstunden).
- **Messen:** Solange die Heizung heizt und die Steckdose mehr als 50 W durchlässt, merkt sich die App alle
  10 Sekunden den Wert (höchstens die letzte Stunde Heizen, nur im Speicher – nach einem Neustart misst sie neu).
  Nach einer Minute Heizen steht unter der Heizung „Beim Heizen gemessen: etwa … W“ (der mittlere Wert, einzelne
  Ausreißer zählen nicht; zu sehen unter *Strom › Strom und Kosten*, bis 0.4.18 unter *Räume*). **Übernehmen** trägt ihn als Leistung ein;
  gespeichert ist er erst mit **Speichern**.
- **Ausgeschaltet:** Die Taste der Steckdose schaltet sie aus – dann hat die Heizung keinen Strom. Die Startseite
  sagt das und hat **Steckdose einschalten** (ohne PIN). Ausschalten kann man sie in der App nur unter *Geräte ›
  Steckdosen* › **Schalten** (ab 0.4.18, zum Testen); gehört sie zu einer Heizung, fragt die App vorher nach.
- **Name** (ab 0.4.18): *Geräte › Steckdosen* › Name › **Ändern …**. Der Name einer Zigbee-Steckdose steht nur in der
  App (`data.json`, `plug_names`); ihr Name in Zigbee2MQTT bleibt, wie er ist. Leer gelassen heißt sie wieder
  „Steckdose“ mit den letzten vier Zeichen ihrer Adresse. Wird sie im Zigbee-Netz entfernt, vergisst die App den Namen.
- **Funk:** Eine Steckdose am Strom gibt das Zigbee-Signal weiter; Thermostate weiter weg erreichen den Pi dann
  besser.
- **Bekannt** (Zigbee2MQTT, Stand 25.11.2026): Bei der S60ZBTPF kann der Leistungswert minutenlang hängen bleiben,
  während Strom und Energie weiterlaufen (Issue 33770); dann kommt eine Weile kein neuer Messwert dazu.
- **Nur simuliert geprüft:** Wie oft und wie genau die echte Steckdose meldet, zeigt sich erst am Gerät.
- **Schnittstelle:** `PUT /api/plugs/<Kennung>/heater` (`{"heater": "<Heizung>"}`, `null` für keine; mit PIN, ab
  0.4.15) ordnet eine Steckdose zu; `PUT /api/costs` nimmt dafür weiterhin auch `plugs`
  (`{"<Heizung>": "<IEEE-Adresse>"}`, `""` für keine); `POST /api/heaters/<id>/plug/on` schaltet die Steckdose einer Heizung ein (ohne PIN); in `GET /api/state` stehen
  `plugs` und je Heizung `plug` (mit `id` und `kind`: `zigbee` oder `wifi`). Ab 0.4.18, beide mit PIN und für Zigbee-
  wie WLAN-Steckdosen: `POST /api/plugs/<Kennung>/switch` (`{"on": true}` oder `false`) schaltet,
  `PUT /api/plugs/<Kennung>/name` (`{"name": "…"}`, leer für keinen) benennt um.

## WLAN-Steckdosen mit Strommessung

Ab 0.4.14. Dasselbe wie mit einer [Zigbee-Steckdose](#zigbee-steckdosen-mit-strommessung), nur ohne Zigbee-Stick: eine
Shelly-Steckdose im WLAN. Die App fragt sie direkt im Heimnetz (wie die Pill) – ohne Cloud und ohne Konto.

- **Welche:** gebaut für die **Shelly Plug M Gen3** (S3PL-30110EU, Schuko, 13 A, bis 3.000 W, etwa 10 €, Stand
  26.11.2026); die größte AeroFlow hat 2.450 W. Andere Shelly-Steckdosen ab Generation 2 mit Strommessung gehen auch,
  wenn sie die Leistung der Heizung vertragen (die Plug S Gen3 nur 2.500 W); Steckdosenleisten mit mehreren Schaltern
  nicht. Nicht passend: Tapo, Tuya/Smart Life, Meross und reine Matter-Steckdosen – sie lassen sich nicht einfach ohne
  Cloud lesen.
- **Ins WLAN bringen:** einstecken. Eine neue Steckdose öffnet ein eigenes WLAN, dessen Name mit „Shelly“ beginnt
  (sonst in der ersten Minute die Taste 5 Sekunden halten, bis sie blau blinkt). Mit dem Handy hinein, im Browser
  192.168.33.1 öffnen und das Heim-WLAN eintragen. Kein Passwort für die Steckdose einschalten – dann kann die App sie
  nicht lesen. Danach das Handy zurück ins Heim-WLAN: Solange es im WLAN der Steckdose ist, erreicht es die App nicht
  („Keine Verbindung zur Heizungs-App“). Kommt man zur App zurück oder antwortet sie wieder, sucht sie von selbst noch
  einmal – ebenso bei der Pill.
- **Übernehmen:** *Einrichtung › Geräte* › **+ Gerät hinzufügen** › „Steckdose“ (bis 0.4.14 **Steckdosen suchen** unter
  *Geräte › Steckdosen*). Die App fragt jede Adresse im Heimnetz (ein paar Sekunden); bei der Steckdose
  **Übernehmen**, dann ihre Heizung oder ihren Raum wählen. Ab 0.4.17 heißt sie in der App so wie in der Shelly-App
  (ihr Gerätename); hat sie dort keinen, „WLAN-Steckdose“ mit den letzten vier Zeichen ihrer Kennung. Einen neuen
  Namen aus der Shelly-App übernimmt die App beim nächsten Suchen, sonst nach höchstens 10 Minuten (`/shelly`, das
  auch eine Steckdose mit Passwort beantwortet). Suchen und Übernehmen lesen nur. **Entfernen …** nimmt sie wieder aus der App; die Steckdose selbst bleibt,
  wie sie ist – an oder aus.
- **Name** (ab 0.4.18): *Geräte › Steckdosen* › Name › **Ändern …** schreibt den Namen in die Steckdose
  (`Sys.SetConfig`) – die Shelly-App zeigt ihn dann auch. Leer gelassen löscht ihn dort; dann heißt sie wieder
  „WLAN-Steckdose“ mit den letzten vier Zeichen ihrer Kennung.
- **Zuordnen, Messen, Ausgeschaltet:** wie bei der Zigbee-Steckdose – die Heizung an der Steckdose wählen (*Geräte ›
  Steckdosen*); dabei stellt die App die Steckdose auf „nach Stromausfall an“. „heute … kWh“ rechnet die App aus dem
  Zähler der Steckdose (sein Stand bei der ersten Abfrage des Tages steht in `data.json`).
- **Lesen:** alle 10 Sekunden (`Shelly.GetStatus`); die MAC-Adresse muss passen. Antwortet sie nicht mehr unter ihrer
  Adresse (der Router kann ihr eine neue geben), sucht die App sie höchstens alle 15 Minuten im Heimnetz und merkt sich
  die neue Adresse. Antwortet sie eine Minute lang nicht, steht das bei der Steckdose.
- **Schreiben** nur drei Dinge: `Switch.Set` (**Steckdose einschalten** auf der Startseite; ab 0.4.18 an und aus
  unter *Geräte › Steckdosen* › **Schalten**), `Switch.SetConfig` mit `initial_state: on` (beim Speichern) und ab
  0.4.18 `Sys.SetConfig` mit dem Namen. Vorher prüft die App, dass unter der Adresse wirklich diese Steckdose
  antwortet.
- **Nur simuliert geprüft:** Die Steckdose spielt `tests/fake_shelly.py` so, wie Shellys Dokumentation sie beschreibt.
  Wie die echte Plug M Gen3 antwortet, zeigt sich erst am Gerät. Umbenennen und Ausschalten (ab 0.4.18) sind am
  echten Gerät noch nicht ausprobiert.
- **Schnittstelle:** `POST /api/plugs/discover` sucht, `POST /api/plugs` (`{"plug": "<Kennung>"}`) übernimmt,
  `DELETE /api/plugs/<Kennung>` entfernt, `PUT /api/plugs/<Kennung>/heater` ordnet zu, ab 0.4.18
  `POST /api/plugs/<Kennung>/switch` schaltet und `PUT /api/plugs/<Kennung>/name` benennt um (alle sechs mit PIN);
  die Kennung ist hier `shellyplugmg3-…`.

## Strom der Steckdosen in Viertelstunden

Ab 0.4.17. Für jede Steckdose mit Strommessung schreibt die App mit, wie viel Strom in jeder Viertelstunde durch sie
geflossen ist – ob an ihr eine Heizung hängt oder ein anderes Gerät.

- **Ansehen:** *Einrichtung › Geräte › Steckdosen* › **Viertelstunden ansehen**: der Tag als Balken, ein Balken je
  Viertelstunde, darüber Strom, Kosten (mit eingetragenem Strompreis) und die höchste Leistung des Tages; mit ‹ ›
  andere Tage; darunter „Viertelstunde für Viertelstunde“ als Tabelle. Ein Tipp auf einen Balken zeigt Wh, den Schnitt
  in W und die höchste gemessene Leistung. Gestreift heißt: keine Werte.
- **Als Tabelle für Excel:** „dieser Tag“ oder „dieser Monat“ lädt eine CSV-Datei mit allen Steckdosen herunter
  (Semikolon und Dezimalkomma, so öffnet sie ein deutsches Excel direkt): Tag, Beginn, Ende, Steckdose, Raum, Heizung,
  Strom (Wh), Höchste Leistung (W).
- **Raum ohne Heizung:** An der Steckdose unter „Raum“ (oder im Schritt „Wohin?“) einen Raum wählen; das geht nur,
  solange keine Heizung gewählt ist – mit Heizung ist ihr Raum der der Heizung. Jede Zeile merkt sich Raum und
  Heizung der Viertelstunde; zieht die Steckdose um, bleiben die alten Zeilen beim alten Raum.
- **Wie gemessen:** aus dem Zähler der Steckdose (Shelly `aenergy.total`, Zigbee `energy`), alle 10 Sekunden gelesen.
  Was zwischen zwei Ablesungen floss, verteilt die App gleichmäßig auf die Zeit dazwischen, auch über
  Viertelstunden-Grenzen. Ohne Zähler: Leistung × Zeit, höchstens 20 Minuten ohne Wert. Ein Zähler, der zurückspringt
  (neu gestartet), zählt von null weiter; ein Sprung, den die Steckdose gar nicht ziehen kann (über 4.000 W), zählt als
  Leistung × Zeit (Zigbee2MQTT hat dafür einen bekannten Fehler). Meldet eine Steckdose sich mehr als 6 Stunden nicht,
  bleibt diese Zeit leer – nichts wird geschätzt. Die höchste Leistung ist der höchste gemessene Wert, bei einer ganz
  gemessenen Viertelstunde mindestens ihr Schnitt.
- **Wo:** `energy/JJJJ-MM.csv` neben `data.json` (auf dem Pi in `/var/lib/flowthermpi/data/energy/`), eine Datei je
  Monat, etwa 6 KB am Tag je Steckdose. Geschrieben wird alle 15 Minuten; den letzten Zählerstand merkt sich die App
  in `energy/zaehler.json`, damit nach einem Neustart oder Stromausfall nichts doppelt zählt: Was in der Zeit floss,
  kommt mit dem nächsten Zählerstand dazu. Aufgezeichnet wird erst, wenn die Uhrzeit stimmt.
- **Datenschutz:** wie beim Verlauf – die Werte bleiben auf dem Pi und kommen nicht in „Protokoll speichern“.
- **Nur simuliert geprüft:** mit erfundenen Zählerständen (`tests/test_energy.py`) und in der Demo. Wie die echte
  Steckdose ihren Zähler meldet, zeigt sich erst am Gerät.
- **Schnittstelle** (alle mit PIN): `GET /api/plugs/<Kennung>/energy/JJJJ-MM-TT` (ein Tag in Viertelstunden),
  `GET /api/energy/JJJJ-MM-TT/JJJJ-MM-TT` (die CSV-Datei, alle Steckdosen, höchstens 366 Tage),
  `PUT /api/plugs/<Kennung>/room` (`{"room": "<Raum>"}`, `null` für keinen; nur ohne Heizung).

## Strom: was jedes Gerät braucht

Ab 0.4.19. Der Tab **Strom** in der Einrichtung zeigt, was jedes Gerät an einer Steckdose mit Strommessung braucht und
kostet – aus den [Viertelstunden](#strom-der-steckdosen-in-viertelstunden), die die App ohnehin mitschreibt.

- **Was hängt dran:** An jeder Steckdose (*Geräte › Steckdosen* und im Schritt „Wohin?“) steht **Angeschlossen**:
  Heizung, Waschmaschine, Trockner, Kühlschrank, Gefriergerät, Klimaanlage oder Sonstiges (ab Werk). Hängt eine
  AeroFlow daran, ist es eine Heizung; die AeroFlow wählt man darunter bei „Heizung“. Für ein anderes Gerät dort erst
  „– keine –“ wählen. Gespeichert in `data.json` unter `plug_appliances`; „Sonstiges“ wird nicht gespeichert.
- **Tag · Woche · Monat:** je Gerät kWh und „etwa … €“ (mit eingetragenem Strompreis) und ein Balken, das größte oben;
  darunter „Zusammen“. Mit ‹ › die Zeit davor und danach. Die Woche geht von Montag bis Sonntag. Ein Tipp auf ein
  Gerät öffnet seine Viertelstunden. Gezählt ist nur, was durch die Steckdosen fließt – nicht der ganze Haushalt.
- **Vergleich** (Woche und Monat): mit der Woche bzw. dem Monat davor, „Woche davor 2,4 kWh · 26 % mehr“. Solange die
  Woche oder der Monat noch läuft, nur bis zur gleichen Uhrzeit davor (beim Monat höchstens bis zu dessen Ende). Nur
  wenn die Steckdose beide Male in mindestens 9 von 10 Viertelstunden gemessen hat – eine neue Steckdose oder ein
  Pi, der Tage aus war, gäbe sonst falsche Prozente. „Zusammen“ wird nur verglichen, wenn jedes Gerät verglichen wird.
- **Bereitschaft (Standby):** was ein Gerät auch dann zieht, wenn es nichts tut – der Wert, unter dem es in einem
  Zehntel der Viertelstunden der letzten 7 Tage lag (Durchschnitt der Viertelstunde), erst nach einem Tag Messung.
  Dazu, was das rund um die Uhr im Jahr macht (W × 8,76 kWh, mit Strompreis in €). Nicht bei Heizung, Kühlschrank und
  Gefriergerät – deren niedrigster Wert ist keine Bereitschaft. Die Auflösung ist 0,4 W (0,1 Wh je Viertelstunde).
- **Strom auf der Startseite:** *Strom › Strom und Kosten* › „Strom der Steckdosen auch auf der Startseite zeigen“
  (ab Werk aus; bis 0.4.19 „„Strom heute“ der Steckdosen …“): die Zeile „Strom heute“, die alle Steckdosen zählt, die
  heute gemessen haben, und ab 0.4.20 an jeder Raum-Kachel Watt und kWh ihrer Steckdosen. An und aus stehen immer da.
- **Waschmaschine und Trockner** (ab 0.4.20): ihre Durchgänge – siehe
  [Wäschen und Trockengänge](#wäschen-und-trockengänge).
- **Nur simuliert geprüft:** mit erfundenen Zählerständen (`tests/test_power.py`) und in der Demo mit einer
  erfundenen Waschmaschine und einem Trockner (`uebergabe/demo_server.py … strom`). Am echten Gerät noch nicht.
- **Schnittstelle** (mit PIN): `PUT /api/plugs/<Kennung>/appliance` (`{"appliance": "waschmaschine"}`; `heizung`,
  `trockner`, `kuehlschrank`, `gefriergeraet`, `klimaanlage`, `sonstiges`; 409, wenn eine AeroFlow daran hängt und es
  keine Heizung sein soll), `GET /api/power/<day|week|month>/JJJJ-MM-TT` (der Zeitraum mit diesem Tag: je Gerät `wh`,
  `eur`, `before_wh`, `standby_w`, `standby_eur_year`; dazu `total`, `prev`, `next`). `PUT /api/costs` nimmt
  `show_power`; in `GET /api/state` stehen je Steckdose `appliance` und `power_today` (`null`, solange der Schalter aus
  ist).

## Wäschen und Trockengänge

Ab 0.4.20. Hängt an einer Steckdose mit Strommessung eine Waschmaschine oder ein Trockner (**Angeschlossen**, siehe
[Strom](#strom-was-jedes-gerät-braucht)), erkennt die App an der Leistung, wann das Gerät läuft und wann es fertig
ist. Gerechnet wird mit dem, was die Steckdose bei jeder Abfrage meldet (alle 10 Sekunden; eine Zigbee-Steckdose
meldet nur Änderungen).

- **Läuft:** sobald das Gerät eine Minute lang mehr als 15 W zieht – bei der Waschmaschine reicht eine Minute
  innerhalb von zwei (sie zieht beim Wassereinlass in Stößen). Der Beginn ist da, wo es davor schon mehr als 8 W zog,
  höchstens 15 Minuten zurück.
- **Fertig:** die Waschmaschine nach 5 Minuten unter 8 W, der Trockner nach 3 Minuten ohne Dauerlast – die kurzen
  Stöße des Knitterschutzes (unter einer Minute) zählen nicht. Das Ende ist da, wo die Ruhe begann. Was kürzer als 5
  Minuten lief (etwa nur Abpumpen), zählt nicht.
- **„Fertig“ auf der Startseite:** „Die Waschmaschine ist fertig – seit 10:42.“ (vom Vortag „seit gestern 22:10“) mit
  **OK**, ohne PIN. Bei zwei gleichen Geräten steht der Name der Steckdose dabei. Der Hinweis geht weg mit OK, wenn
  dasselbe Gerät wieder 5 Minuten läuft, oder nach 12 Stunden – nicht schon bei 0 W, weil viele Maschinen sich nach
  dem Ende selbst ausschalten. Hängt an der Steckdose etwas anderes (Angeschlossen geändert, Heizung gewählt,
  Steckdose entfernt), geht er auch.
- **Im Tab „Strom“:** unter den Geräten je Waschmaschine und Trockner eine Karte „Wäschen“ bzw. „Trockengänge“: wie
  viele im Zeitraum, kWh, „etwa … €“ und im Schnitt je Durchgang, dazu der Zeitraum davor; während eines Durchgangs
  „Läuft gerade – seit 13:21 Uhr, bisher 0,52 kWh“. Darunter jeder Durchgang mit Uhrzeit, Dauer, kWh, € und Heizzeit.
  Ein Tipp öffnet die Leistung Minute für Minute, mit ‹ › zum Durchgang davor und danach. Minuten über 1.000 W sind
  farbig („heizt“) – dann heizt das Gerät das Wasser oder die Luft; wie warm, weiß die App nicht.
- **Grenzen:** Läuft ein Gerät länger als 8 Stunden ohne Pause, passt es nicht zu diesen Regeln – der Durchgang endet
  ohne Hinweis, und ein neuer beginnt erst, wenn es wieder unter 8 W war. Meldet die Steckdose 15 Minuten nichts,
  endet der Durchgang ohne Hinweis. Ist die App nach einem Update oder Stromausfall binnen 5 Minuten wieder da, läuft
  der Durchgang weiter; sonst endet er dort, wo sie ihn zuletzt sah, ohne Hinweis. Die Schwellen sind ein Anfang – ob
  sie zu den echten Geräten passen, muss sich zeigen.
- **Gespeichert** in `data/geraete/`: `durchgaenge-JJJJ.csv` (`beginn`, `ende` in Sekunden seit 1970, `id`, `art`,
  `wh`, `w_max`, `heiz_min`), `kurven-JJJJ-MM.json` (Watt je Minute je Durchgang, etwa 2 KB), `laufend.json` (der
  laufende, jede Minute) und `hinweise.json` (die offenen „fertig“). Ins Protokoll kommt nur, wenn etwas schiefgeht –
  wann gewaschen wird, geht niemanden etwas an.
- **Nur simuliert geprüft:** mit erfundenen Leistungskurven einer Waschmaschine und eines Trockners mit Knitterschutz
  (`tests/test_cycles.py`) und in der Demo (`uebergabe/demo_server.py … strom`: eine Waschmaschine, die gerade läuft,
  und ein Trockner, der vor 25 Minuten fertig wurde). Am echten Gerät noch nicht.
- **Schnittstelle:** `GET /api/state` hat `notices` (`[{id, plug, at, text}]`) und je Steckdose `run` (`{since,
  minutes, wh}` oder `null`); `POST /api/notices/<id>/ok` (ohne PIN); `GET /api/power/…` hat je Gerät `runs`
  (`count`, `wh`, `eur`, `avg_wh`, `before`, `items`, `running`); `GET /api/plugs/<Kennung>/runs/<beginn>` (mit PIN) –
  ein Durchgang Minute für Minute, mit `prev` und `next`.

## Licht: Zigbee-Lampen

Ab 0.4.21, Einstellungen je Lampe ab 0.4.22. Zigbee-Lampen – Philips Hue, IKEA, Innr und andere – hängen direkt am
Zigbee-Stick des Pi, ohne Hue-Bridge und ohne Cloud. Die Lampen brauchen immer Strom: Der Lichtschalter an der Wand
bleibt an, geschaltet wird in der App.

- **Welche:** gebaut für IKEA FLOALT und JETSTRÖM, Innr und Philips Hue. Andere gehen auch, wenn Zigbee2MQTT sie als
  Licht mit an/aus kennt; ein Gerät mit mehreren Lichtern, die es einzeln schaltet, nicht.
- **Anlernen:** *Einrichtung › Geräte* › **+ Gerät hinzufügen** › „Lampe“. Die App sagt je Hersteller, wie man die
  Lampe zurücksetzt, damit sie ein neues Netz sucht – Hue: in der Hue-App löschen, ohne Hue-App 5-mal 2 Sekunden aus
  und 8 Sekunden an; FLOALT: 6-mal kurz aus und wieder an; JETSTRÖM: die Reset-Taste am Netzteil; Innr: nach der
  Anleitung von Innr. Im letzten Schritt „Wohin?“ bekommt die Lampe ihren Namen und ihren Raum.
- **Einrichtung:** *Geräte › Licht* – je Lampe Name, Raum, Schalten, Helligkeit, Lichtfarbe und Farbe (wie unter
  „Einstellen ›“, zum Ausprobieren), **Beim Einschalten** und **Entfernen …**. Unter „Beim Einschalten“ stehen die
  Helligkeit (10 bis 100 %) und die Lichtfarbe, die die App bei jedem Einschalten einstellt; „wie zuletzt“ lässt die
  Lampe, wie sie war. Das gilt nur, wenn die App einschaltet – nicht beim Lichtschalter an der Wand; eine Lampe, die
  gerade an ist, bleibt, wie sie ist. Name, Raum und „Beim Einschalten“ stehen nur in der App (`data.json`:
  `light_names`, `light_rooms`, `light_colors`, `light_levels`); wird die Lampe im Zigbee-Netz entfernt, vergisst die
  App sie.
- **Einstellen ›** (ab 0.4.22): Auf der Startseite öffnet „Einstellen ›“ am Licht eines Raums ein Fenster mit jeder
  Lampe für sich, ohne PIN. **Helligkeit:** dunkler und heller in Schritten von 10 % und ein Schieberegler von 1 bis
  100 %. **Lichtfarbe:** Knöpfe für Kerzenlicht (2.200 K), warmweiß (2.700 K), neutralweiß (4.000 K), kaltweiß
  (5.000 K) und Tageslicht (6.500 K) – nur, was die Lampe kann – und ein Regler von warm bis kühl über ihren ganzen
  Bereich. **Farbe** (nur Farblampen, etwa Hue White and Color Ambiance): Rot, Orange, Gelb, Grün, Türkis, Blau, Lila
  und Pink. Ist die Lampe aus, schalten Regler und Knöpfe sie mit ein; dunkler und heller gehen nur, wenn sie an ist.
  Den Wert vom Regler bekommt die Lampe, wenn man ihn loslässt.
- **Nachfragen** (ab 0.4.22): IKEA-Lampen sagen ihre Helligkeit erst, wenn man sie fragt. Fehlt der App ein Wert –
  an oder aus, Helligkeit, Lichtfarbe –, fragt sie die Lampe: sofort, nach einer Minute und danach jedes Mal doppelt so
  lange, höchstens etwa alle halbe Stunde. Bis die Antwort da ist, steht bei der Helligkeit „–“, und „heller“ und
  „dunkler“ fehlen. So findet die App auch eine Lampe wieder, die keinen Strom hatte.
- **Schalten:** „Licht einschalten“ schickt allen Lampen des Raums den Befehl zugleich und wartet bis zu 6 Sekunden
  auf ihre Antwort. **heller** und **dunkler** stellen alle eingeschalteten Lampen des Raums auf dieselbe Helligkeit:
  von der Anzeige (dem Mittel der Lampen) auf die nächste Zehnerstufe, die mindestens 5 % entfernt ist – von 55 % also
  auf 60 % bzw. 50 %.
- **Keine Antwort:** Hat eine Lampe keinen Strom (Lichtschalter an der Wand aus), antwortet sie nicht. Die App sagt
  das, und die Lampe steht auf „keine Verbindung“, bis sie sich wieder meldet; „heller“ und „dunkler“ lassen sie so
  lange aus. Antwortet keine Lampe des Raums, kommt eine Fehlermeldung.
- **Schalter:** ab 0.4.23, siehe [Licht: Schalter](#licht-schalter). **Bewegungsmelder:** ab 0.4.24, siehe
  [Licht: Bewegungsmelder](#licht-bewegungsmelder).
- **Protokoll:** Wann jemand das Licht schaltet, steht nur im
  [ausführlichen Protokoll](#protokoll-wenn-etwas-schiefgeht); wenn eine Lampe nicht antwortet, steht es immer drin.
- **Nur simuliert geprüft:** mit nachgebauten Lampen in `tests/fake_mqtt.py` (`tests/test_light.py`) und in der Demo
  (`uebergabe/demo_server.py … licht`). Mit echten Lampen noch nicht.
- **Schnittstelle:** ohne PIN `POST /api/rooms/<Raum>/light` (`{"on": true}` oder `false`), `POST
  /api/rooms/<Raum>/light/dim` (`{"step": 1}` oder `-1`), `POST /api/lights/<IEEE-Adresse>/switch` (`{"on": …}`) und
  `POST /api/lights/<IEEE-Adresse>/set` mit genau einem von `{"brightness": 1–100}`, `{"step": 1}` oder `-1`,
  `{"kelvin": 2700}`, `{"white": "kerze"}` (`"warm"`, `"neutral"`, `"kalt"`, `"tag"`) und `{"hue": "blau"}`
  (`"rot"`, `"orange"`, `"gelb"`, `"gruen"`, `"tuerkis"`, `"lila"`, `"pink"`); mit PIN `PUT
  /api/lights/<IEEE-Adresse>/name` (`{"name": "…"}`, leer für keinen), `…/room` (`{"room": "<Raum>"}`, `null` für
  keinen), `…/color` (`{"color": "warm"}`, die Namen wie bei `white`, oder `""` für wie zuletzt) und `…/level`
  (`{"level": 40}` oder `null` für wie zuletzt). In `GET /api/state` steht `lights`: je Lampe `id`, `name`, `model`,
  `room`, `state`, `brightness` (in %), `kelvin` (`null`, solange sie eine Farbe zeigt), `hue`, `dim`, `kelvins`
  (ihr Bereich), `colors`, `hues` (kann Farben), `color`, `level`, `lost` und `heard`.

## Licht: Schalter

Ab 0.4.23. Funk-Schalter mit Zigbee schalten das Licht eines Raums – auch mehrere Schalter im selben Raum, wie bei
einer Wechselschaltung.

- **Welche:** gebaut für IKEA RODRET, Hue Smart Button und Hue Dimmschalter (beide Generationen). Diese koppelt die
  App direkt mit den Lampen: Jeder Schalter bekommt im Zigbee-Netz eine eigene Gruppe (`schalter-<IEEE-Adresse>`) mit
  den Lampen seines Raums und schaltet sie dann selbst – auch, wenn der Pi aus ist. Andere Schalter, die Zigbee2MQTT
  mit Tastendrücken kennt, schalten über die App (an, aus, umschalten, heller, dunkler) – nur, solange der Pi läuft.
- **Anlernen:** *Einrichtung › Geräte* › **+ Gerät hinzufügen** › „Schalter“. Die App sagt je Hersteller, wie der
  Schalter in den Anlern-Modus kommt – RODRET: die kleine Taste unter der hinteren Abdeckung 4-mal drücken; Smart
  Button: die Taste unter der hinteren Abdeckung kurz drücken (10 Sekunden halten setzt ihn zurück); Dimmschalter: die
  Setup-Taste unter der Batterieklappe 10 Sekunden halten. Im letzten Schritt „Wohin?“ bekommt der Schalter seinen
  Namen und seinen Raum.
- **Koppeln:** Ein Schalter schläft, um Batterie zu sparen, und nimmt die Kopplung nur kurz nach einem Tastendruck an.
  Darum koppelt ihn die App beim nächsten Druck; bis dahin – und bei diesem Druck – schaltet die App das Licht. Ein
  anderer Raum oder „kein Licht“ gilt sofort, ohne Druck: Die App ändert nur, welche Lampen in seiner Gruppe sind, und
  die Lampen haben immer Strom. Kommt eine Lampe in einen anderen Raum, wechselt sie mit; eine Lampe ohne Strom kommt
  später dazu (die App schaut alle 5 Minuten nach). Wird ein Schalter zurückgesetzt und neu angelernt, koppelt ihn die
  App beim nächsten Druck neu.
- **Abwechselnd an und aus:** Hue Smart Button und Hue Dimmschalter (2. Generation) senden beim Drücken abwechselnd
  „an“ und „aus“ – sie wissen nicht, ob das Licht gerade brennt. Hat inzwischen die App oder ein anderer Schalter
  geschaltet, geht ein Druck ins Leere. Läuft der Pi, merkt die App das und schaltet um, etwa eine Sekunde später
  (beim Dimmschalter fragt sie dafür die Lampen). Für zwei Schalter im selben Raum passt der IKEA RODRET besser: Er hat
  eigene Tasten für an und aus.
- **Batterie:** Hat ein Schalter 15 % oder weniger, steht auf der Startseite „Die Batterie im Schalter „…“ ist fast
  leer“ mit **OK**. Nach OK kommt die Meldung erst wieder, wenn eine neue Batterie drin war (mindestens 40 %).
- **Einrichtung:** *Geräte › Licht* › „Schalter“ – je Schalter Name, **Schaltet das Licht im Raum**, Zustand
  (gekoppelt, noch nicht gekoppelt, über die App – und warum Koppeln nicht geklappt hat), Batterie, wann zuletzt
  gedrückt, und **Entfernen …** (kurz vorher eine Taste drücken, damit er wach ist). Name, Raum und Kopplung stehen nur
  in der App (`data.json`: `button_names`, `button_rooms`, `button_bound`, `button_battery_ok`).
- **Nur simuliert geprüft:** mit nachgebauten Schaltern in `tests/fake_mqtt.py` (`tests/test_button.py`) und in der
  Demo (`uebergabe/demo_server.py … licht`). Mit echten Schaltern noch nicht – offen ist vor allem, ob ein gekoppelter
  Schalter dem Pi seine Tastendrücke noch meldet (sonst kann die App einen Druck ins Leere nicht nachholen) und wie
  die echten Schalter abwechseln.
- **Schnittstelle:** mit PIN `PUT /api/buttons/<IEEE-Adresse>/name` (`{"name": "…"}`, leer für keinen) und `…/room`
  (`{"room": "<Raum>"}`, `null` für keinen); ohne PIN `POST /api/notices/batterie-<IEEE-Adresse>/ok`. In `GET
  /api/state` steht `buttons`: je Schalter `id`, `name`, `model`, `kind`, `room`, `direct` (kann direkt gekoppelt
  werden), `coupled`, `error`, `battery`, `pressed` und `heard`.

## Licht: Bewegungsmelder

Ab 0.4.24. Ein Bewegungsmelder mit Zigbee schaltet das Licht seines Raums ein, wenn sich dort jemand bewegt, und
wieder aus, wenn eine Weile nichts mehr los ist – auch mehrere Melder im selben Raum, etwa an beiden Enden des Flurs.

- **Welche:** jeder Melder, den Zigbee2MQTT mit Bewegung ja/nein (`occupancy`) kennt; nachgebaut sind der Hue
  Bewegungsmelder (SML001 und SML003) und der IKEA VALLHORN. Was ein Melder sonst noch kann – Helligkeit in Lux,
  Temperatur, Empfindlichkeit, ein Licht, das bei Bewegung blinkt –, liest die App aus seiner Beschreibung in
  Zigbee2MQTT.
- **Über den Pi:** Anders als die Schalter koppelt die App einen Melder nicht direkt mit den Lampen – das Licht
  schaltet die App. Ist der Pi aus, geht das Licht bei Bewegung nicht an.
- **Anlernen:** *Einrichtung › Geräte* (oder im Raum) › **+ Gerät hinzufügen** › „Bewegungsmelder“. Hue: die Setup-Taste hinten 3 bis
  5 Sekunden halten, bis das Licht rot blinkt (der ältere SML001: danach noch einmal halten, bis es orange blinkt);
  VALLHORN: die Kopplungstaste 4-mal kurz drücken. Im letzten Schritt „Wohin?“ bekommt der Melder Name und Raum; der
  Haken „Dort das Licht bei Bewegung einschalten“ (vorgewählt) schaltet es für den Raum ein.
- **Licht bei Bewegung** (je Raum mit Melder, ab Werk aus; ab 0.4.25 in der Raum-Ansicht im Abschnitt „Licht“ –
  unter *Geräte › Licht* steht nur noch der Weg dorthin):
  - **Aus**, wenn sich 1 bis 120 Minuten nichts bewegt hat (ab Werk 10).
  - **Tagsüber** Helligkeit und Lichtfarbe (ab Werk 100 %, neutralweiß), **nachts gedämpft** von–bis (ab Werk 22 bis
    6 Uhr mit 20 %, warmweiß) – jede Lampe nur, soweit sie es kann.
  - **Nur, wenn es dunkel ist** (ab Werk aus): dunkler als 1 bis 2000 Lux (ab Werk 30). Bei brennendem Licht misst der
    Melder mehr – darum nimmt die App, was er zuletzt bei ausgeschaltetem Licht gemessen hat (höchstens 2 Stunden alt;
    bei mehreren Meldern den dunkelsten). Weiß sie es nicht, geht das Licht an.
  - **Licht, das jemand von Hand eingeschaltet hat,** bleibt an (ab Werk) oder geht auch aus. Schaltet jemand ein Licht
    von Hand ein, das die Bewegung eingeschaltet hatte, gilt es als von Hand eingeschaltet.
  - **Von Hand ausgeschaltet:** Schaltet jemand das Licht aus (Startseite, Einrichtung, Schalter oder ein Licht, das
    plötzlich aus ist), schaltet die App es erst nach einer Pause wieder bei Bewegung ein (ab Werk 1 Minute, 0 bis 30).
  - **Welche Lampen, welche Melder:** ab Werk alle des Raums – auch später dazugekommene; bei mehreren lassen sie
    sich einzeln wählen.
- **Was nicht passiert:** Brennt schon ein Licht im Raum, ändert eine Bewegung nichts daran. Was die App eingeschaltet
  hat, schaltet sie erst aus, wenn sich die eingestellte Zeit lang nichts bewegt hat. Sagt ein Melder „Bewegung“ und
  schweigt dann über eine Stunde, zählt das nicht mehr als Bewegung – sonst bliebe das Licht an, wenn sein „vorbei“
  verloren geht. Welche Lampen die App eingeschaltet hat, merkt sie sich auch über einen Neustart.
- **Einrichtung:** *Geräte › Licht* › „Bewegungsmelder“ – je Melder Name, Raum, wann zuletzt Bewegung, Helligkeit,
  Temperatur, Batterie, **Empfindlichkeit** und **blinkt bei Bewegung** (nur, was er kann; er schläft zwischendurch –
  vorher einmal vor ihm bewegen) und **Entfernen …**. Meldet er sich 6 Stunden nicht, steht das rot dabei. Name, Raum
  und Einstellungen stehen nur in der App (`data.json`: `detector_names`, `detector_rooms`, `detector_battery_ok`,
  `motion_light`, `motion_auto`).
- **Nicht gebaut:** ein Sicherheitsnetz in der Lampe selbst („geht nach 30 Minuten von allein aus“, Zigbee
  `on_time`). Ein normales „an“ hebt es nicht sicher wieder auf – das Licht könnte ausgehen, während jemand da ist.
  Erst nach einem Test mit einer echten Lampe.
- **Nur simuliert geprüft:** mit nachgebauten Meldern in `tests/fake_mqtt.py` (`tests/test_motion.py`) und in der Demo
  (`uebergabe/demo_server.py … licht`). Mit einem echten Melder noch nicht – offen ist vor allem, wann der echte Hue
  Melder „vorbei“ meldet und wie oft er die Helligkeit schickt.
- **Schnittstelle:** mit PIN `PUT /api/detectors/<IEEE-Adresse>/name` (`{"name": "…"}`, leer für keinen), `…/room`
  (`{"room": "<Raum>"}`, `null` für keinen), `…/settings` (`{"sensitivity": "high"}` – einer der Werte in
  `sensitivities` – oder `{"led": true}`) und `PUT /api/rooms/<Raum>/motion` mit einem oder mehreren von `on`,
  `minutes`, `day_level`, `day_color`, `night`, `night_from`, `night_to` (`"22:00"`), `night_level`, `night_color`,
  `dark_only`, `lux`, `hand_off`, `pause`, `lamps` und `detectors` (Listen von IEEE-Adressen, `null` für alle des
  Raums); ohne PIN `POST /api/notices/batterie-<IEEE-Adresse>/ok`. In `GET /api/state` steht `detectors`: je Melder
  `id`, `name`, `model`, `room`, `moving`, `moved`, `lux`, `temperature`, `battery`, `sensitivity`, `sensitivities`,
  `led`, `led_settable`, `silent` und `heard`; je Raum mit Melder `motion`: die Einstellungen und `auto`, `night_now`,
  `off_at`, `pause_until`, `moving`, `moved` und `detectors`.

## Verlauf und Kosten

Die Heizungen melden keinen Verbrauch und führen keinen Verlauf – weder im Gerät noch in der Cloud (Thermotecs
Data-Act-Infoblatt). Ab 0.4.13 schreibt die App deshalb selbst mit, was sie ohnehin alle 10 Sekunden abfragt; die
Heizungen werden dafür nicht öfter gefragt.

- **Was:** je Heizung, Thermostat und Fühler und je fünf Minuten die Raumtemperatur (Mittel), die eingestellte
  Temperatur und wie viele Sekunden das Gerät geheizt hat, „Fenster offen“ hatte und erreichbar war; bei Fühlern
  Temperatur und Luftfeuchte.
- **Wo:** `history/JJJJ-MM.csv` neben `data.json` (auf dem Pi in `/var/lib/flowthermpi/data/history/`), eine Datei je
  Monat, etwa 40 KB am Tag bei vier Geräten. Die App sammelt im Speicher und schreibt alle 15 Minuten – das schont
  die Speicherkarte; bei Stromausfall fehlen höchstens die letzten 15 Minuten. Aufgezeichnet wird erst, wenn die
  Uhrzeit stimmt (der Pi hat keine Batterie-Uhr). Lücken bleiben Lücken, nichts wird aufgefüllt.
- **Strom und Kosten sind eine Schätzung:** Heizdauer × eingetragene Leistung × Strompreis, ohne Grundpreis. Ob die
  Heizung beim Heizen immer die volle Leistung zieht, ist nicht geprüft (die Anleitung nennt als „maximale
  kontinuierliche Wärmeleistung“ teils weniger). Genauer wird es mit einem Zwischenstecker mit Strommessung: einen
  Tag lang nachmessen und die Leistung anpassen – ab 0.4.14 auch mit einer Steckdose mit Strommessung
  ([Zigbee](#zigbee-steckdosen-mit-strommessung) oder [WLAN](#wlan-steckdosen-mit-strommessung)), deren Messung die App
  übernimmt. Eine geänderte Leistung oder ein neuer Preis gilt auch für die
  vergangenen Tage. Thermostate an Wasser-Heizkörpern zählen nur die Heizdauer – die Wärme kommt vom Heizkessel.
- **Ein Raum** zeigt die Temperatur seiner Fühler, wenn sie gemessen haben, sonst die seiner Heizungen; „geheizt“
  ist die Zeit, in der mindestens eine seiner Heizungen geheizt hat. Zieht eine Heizung um, nimmt sie ihren Verlauf
  mit.
- **Datenschutz:** Der Verlauf zeigt, wann jemand zu Hause ist. Er bleibt auf dem Pi und kommt nicht in „Protokoll
  speichern“.
- **Noch nicht da** (geplant für 0.4.15): Woche und Monat als Balken, der Vergleich mit Vorwoche und Vormonat, „Als
  Tabelle speichern“ und das Aufräumen (5-Minuten-Werte 13 Monate, Tagessummen unbegrenzt). Bis dahin bleiben alle
  Dateien liegen.
- **Schnittstelle:** `GET /api/rooms/<id>/history/JJJJ-MM-TT` und `GET /api/heaters/<id>/history/JJJJ-MM-TT` (ohne
  PIN, wie die Startseite), `PUT /api/costs` (Einrichtung); in `GET /api/state` stehen `costs`, `history` und je Raum
  und Heizung `today`.

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

Heizungen legst du am einfachsten in der App an: Tab „Geräte“ → **+ Gerät hinzufügen** → „Heizung“, bzw. **Bearbeiten**
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
- Die Heizung speichert keinen Verlauf und meldet keinen Verbrauch – beides führt die App selbst, siehe
  [Verlauf und Kosten](#verlauf-und-kosten).

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
Passwörter und Tokens enthalten – geprüft wird, dass keines davon in der Datei landet. Verlauf und Kosten laufen mit
erfundenen Uhrzeiten und Werten (`tests/test_history.py`), auch über Monatswechsel und Sommer-/Winterzeit. Die
Steckdosen spielt `tests/fake_mqtt.py` wie eine SONOFF S60ZBTPF (`tests/test_plug.py`), die WLAN-Steckdosen
`tests/fake_shelly.py` wie eine Shelly Plug M Gen3 – auch eine mit Passwort und eine Steckdosenleiste
(`tests/test_wifi_plug.py`); wie oft und wie genau die echten melden, prüfen die Tests nicht. Den Strom in
Viertelstunden prüft `tests/test_energy.py` mit erfundenen Zählerständen – auch Neustart, Lücken, zurückspringende
Zähler und die Datei für Excel. Den Tab „Strom“ prüft `tests/test_power.py` mit erfundenen Viertelstunden – eine
laufende Woche mit der Woche davor, ein kürzerer Monat, eine Steckdose, die erst seit gestern misst (kein Vergleich),
Bereitschaft und „Strom heute“. Die Wäschen und Trockengänge prüft `tests/test_cycles.py` mit erfundenen
Leistungskurven – eine Wäsche mit Einweichpause, ein Trockner mit Knitterschutz, Bereitschaft und kurze Stöße, eine
Zigbee-Steckdose, die nur Änderungen meldet, ein Update mitten in der Wäsche, eine Steckdose, die verstummt, ein
Gerät, das nie ruht, und das „fertig“ mit OK. Wie die Waschmaschine oder der Trockner im echten Haushalt Strom ziehen,
prüfen die Tests nicht. Das Licht prüft `tests/test_light.py` mit Lampen, die `tests/fake_mqtt.py` wie eine IKEA
FLOALT, eine Innr und eine Hue nachbaut (ihre Farbbereiche sind erfunden) – auch eine Lampe ohne Strom, eine, die ihre
Helligkeit erst auf Nachfrage sagt, Helligkeit, Lichtfarbe und Farben je Lampe, „Beim Einschalten“ und das Protokoll;
wie schnell und wie zuverlässig echte Lampen antworten, prüfen die Tests nicht. Die Schalter prüft
`tests/test_button.py` mit Schaltern, die `tests/fake_mqtt.py` wie IKEA RODRET, Hue Smart Button, Hue Dimmschalter und
einen SONOFF-Taster nachbaut – Koppeln beim nächsten Druck, ein Raumwechsel ohne Druck, Lampen, die den Raum wechseln,
ein Druck ins Leere, eine Kopplung, die nicht klappt, ein alter Tastendruck nach einem Neustart und die
Batterie-Meldung; wie echte Schalter sich verhalten, prüfen die Tests nicht. Die Bewegungsmelder prüft
`tests/test_motion.py` mit Meldern, die `tests/fake_mqtt.py` wie zwei Hue-Melder und einen IKEA VALLHORN nachbaut –
Licht an und nach der Zeit wieder aus, mehrere Melder in einem Raum, tagsüber und nachts, nur wenn es dunkel ist, von
Hand ein- und ausgeschaltet, Pause, ein Neustart, ein verstummter Melder, Entfernen und die Batterie-Meldung; wie ein
echter Melder sich verhält, prüfen die Tests nicht.

## Ideen für später

- Paket für das NAS (Docker); feste Versionen (Git-Tags) für den Update-Knopf; automatische Updates.
- Raum-Ansicht, Teil 2: von der Startseite in den Raum (nach PIN); der Strom des Raums (heute, Woche).
- Licht: ein Schalter nur für einzelne Lampen eines Raums; das Sicherheitsnetz in der Lampe (`on_time`) nach einem
  Test mit einer echten Lampe.
- Regelung über einen Raumfühler.
- Verlauf und Kosten, Teil 2: Woche und Monat als Balken, Vergleich, „Als Tabelle speichern“, Aufräumen (0.4.15).
- Steckdosen, Teil 2: die gemessenen kWh in Verlauf und Kosten statt der Schätzung.
- Strom, Teil 3: ein Wächter für Kühlschrank und Gefriergerät; später die Spülmaschine (Durchgänge wie bei
  Waschmaschine und Trockner) und der Stromzähler des Hauses.
- Heizungen zusätzlich in Apple Home einbinden.

## Lizenz

Copyright © 2026 antratec

HoLiTherm ist freie Software unter der **GNU General Public License, Version 3**
(siehe [LICENSE](LICENSE)): Sie darf genutzt, verändert und weitergegeben werden – wer eine
veränderte Version weitergibt, muss deren Quellcode ebenfalls unter dieser Lizenz offenlegen.
HoLiTherm wird ohne jede Gewährleistung bereitgestellt.

Die Schrift Atkinson Hyperlegible Next (Braille Institute; lateinischer Teil, von Google Fonts) in `web/fonts/` steht
unter der SIL Open Font License 1.1 (siehe [web/fonts/OFL.txt](web/fonts/OFL.txt)).
