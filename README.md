# **Tibber Smart Charging**

Ein selbstgehostetes Smart-Charging-System für Elektroautos. Das Skript:

* zieht stündliche Strompreise von [Tibber](https://tibber.com) und (für Tag 3–7) eine Börsenpreis-Prognose von [EpexPredictor](https://github.com/b3nn0/EpexPredictor) (Rückfall: Wetter-Heuristik über [Open-Meteo](https://open-meteo.com/)),
* liest aus deinem Google Calendar Ladeziele (z. B. „80 % bis Mo 07:00"),
* fragt den aktuellen Ladezustand (SoC) und den Stecker-Status deines Autos über die [Tibber Data API](https://data-api.tibber.com/docs/) ab (kostenlos, wenn das Auto in der Tibber-App verbunden ist; alternativ über [Tessie](https://tessie.com)),
* berechnet, wann zu welchem Preis geladen werden soll,
* schreibt den Plan zurück in den Kalender (Lade-Termine, Preisinfos, Prognosen),
* schaltet zur richtigen Zeit die Wallbox über [Home Assistant](https://www.home-assistant.io/) an und aus.

Alle externen Komponenten sind optional in dem Sinne, dass das Skript ohne sie nicht crasht — aber für das volle Smart-Charging brauchst du alle.

## **Architektur-Übersicht**

```
┌────────┐    Preise heute+morgen     ┌──────────────────────────┐
│ Tibber │ ──────────────────────────►│                          │
└────────┘                            │                          │
                                      │  tibber-to-google-       │
┌──────────┐  Preisprognose 7 Tage    │  calendar.py             │
│EpexPred. │ ──────────────────────►  │                          │      ┌──────────────┐
└──────────┘                          │                          │ ───► │Google Calendar│
                                      │  • Plan berechnen        │      │ #Tibber       │
┌────────────┐ SoC + Steckverbindung  │  • Pläne diff'fen        │      │ #Vorhersage   │
│Tibber Data │ ──────────────────────►│  • Calendar synchronisieren     │ #charging     │
│API / Tessie│                        │  • Wallbox schalten      │      │ #charge (input)│
└────────────┘                        │                          │      └──────────────┘
                                      └────────────┬─────────────┘
                                                   │
                                                   ▼
                                          ┌─────────────────┐    ┌─────────┐
                                          │ Home Assistant  │ ─► │ Wallbox │
                                          │ input_boolean   │    │ (KEBA…) │
                                          └─────────────────┘    └─────────┘
```

Das Skript läuft als Cronjob alle 15 Minuten und ist zustandslos in dem Sinne, dass es jeden Lauf neu plant. Lokaler Cache (`state.json`) verhindert API-Spam.

---

## **Voraussetzungen**

**Technisch:**
* Python 3.9+
* Linux/macOS/WSL (`fcntl.flock` ist POSIX-only)
* Cron oder vergleichbarer Scheduler
* Terminal-Zugriff

**Konten/APIs (jedes ist eine separate Aufgabe):**
* **Tibber** mit Smart-Meter-Vertrag und Developer-API-Zugang (kostenlos für Bestandskunden)
* **Google Cloud Project** mit aktivierter Calendar API
* **Fahrzeug-Akkustand**: entweder das Auto in der **Tibber-App** verbunden (kostenlos, empfohlen) oder ein **Tessie**-Account (kostenpflichtig)
* **Home Assistant**-Instanz, die deine Wallbox erreichen kann (z. B. via Modbus, KEBA-UDP, eigene Integration)
* Eine **kompatible Wallbox** mit HA-Anbindung (entwickelt für KEBA P30 c-series, aber alles was sich via HA schalten lässt funktioniert)

Für reines „Strompreise im Kalender visualisieren" reichen **Tibber + Google**. Für Smart-Charging brauchst du alle vier.

---

## **Installation**

```bash
git clone https://github.com/speedxy/tibber-to-google-calendar.git
cd tibber-to-google-calendar

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
# Für Tests:
pip install -r requirements-dev.txt

cp config.example.json config.json
```

`config.json` füllst du im Verlauf des nächsten Abschnitts.

---

## **Setup externer Komponenten**

### **1. Tibber API-Token**

1. Auf [developer.tibber.com](https://developer.tibber.com/) mit deinem Tibber-Account einloggen.
2. **My Access Tokens** → **Generate new token**. Scope „Read price info" reicht.
3. Token kopieren und in `config.json` unter `TIBBER_API_KEY` eintragen.

### **2. Google Calendar Service Account**

Ein Service Account erlaubt Server-zu-Server-Zugriff ohne manuelle Browser-Logins.

1. **Projekt anlegen:** [Google Cloud Console](https://console.cloud.google.com/) → neues Projekt erstellen.
2. **API aktivieren:** „APIs & Services" → „Library" → nach „Google Calendar API" suchen und aktivieren.
3. **Service Account erstellen:**
   * **IAM & Admin** → **Service Accounts** → **+ Create Service Account**
   * Name z. B. `tibber-calendar-bot`, dann **Done**.
4. **JSON-Schlüssel erzeugen:**
   * Klick auf den erstellten Service Account → Tab **Keys** → **Add Key → Create new key** → **JSON**.
   * Die Datei wird heruntergeladen.
5. **Datei verschieben und benennen:**
   * Umbenennen zu `service_account.json`
   * In das Repo-Root-Verzeichnis legen (neben `tibber-to-google-calendar.py`)
   * Datei ist via `.gitignore` ausgeschlossen — niemals committen
6. **Kalender freigeben:**
   * `service_account.json` öffnen, `"client_email"` kopieren.
   * In Google Calendar den Ziel-Kalender öffnen → **Settings and sharing** → **„Share with specific people or groups"** → Service-Account-E-Mail hinzufügen.
   * Berechtigung: **„Make changes to events"**.
7. **Calendar-ID besorgen:**
   * Im Kalender-Settings unter **„Integrate calendar"** → **„Calendar ID"** kopieren.
   * In `config.json` unter `GOOGLE_CALENDAR_ID` eintragen.

### **3. Fahrzeug-Akkustand über die Tibber Data API (empfohlen)**

Wenn dein Auto in der Tibber-App verbunden ist und du dort den Akkustand siehst, liest das Skript ihn über die [Tibber Data API](https://data-api.tibber.com/docs/) aus. Kostenlos, ohne Tessie. Tibber fragt den Hersteller selbst ab, das Skript weckt das Auto nie. Getestet mit Tesla Model 3.

1. Auf [data-api.tibber.com/clients/manage](https://data-api.tibber.com/clients/manage/) einen OAuth-Client anlegen.
   * Redirect-URI: `http://localhost:8765/callback`
   * Scopes: `openid`, `profile`, `email`, `offline_access`, `data-api-user-read`, `data-api-homes-read`, `data-api-vehicles-read`
   * Client-Secret sofort kopieren, es wird nur einmal angezeigt.
2. In `config.json` den Block `TIBBER_DATA_API` mit `client_id`, `client_secret` und `redirect_uri` ergänzen (siehe `config.example.json`).
3. Einmalig anmelden und Geräte anzeigen:

```bash
python scripts/tibber_data_auth.py
```

Das Skript öffnet die Tibber-Anmeldung im Browser, speichert das Token in `tibber_data_token.json` (gitignored, nur für dich lesbar) und listet alle Geräte mit ihren Werten. Taucht dort `stateOfCharge` auf, funktioniert der Zugang. Später erneuert sich das Token selbst. Wird es ungültig (z. B. Client gelöscht oder Skript länger als ca. 30 Tage nicht gelaufen), das Skript einfach erneut ausführen. `--probe` zeigt nur die Geräte an, `--reauth` erzwingt eine neue Anmeldung, `--headless` ist für Server ohne Browser (siehe „Headless-Betrieb").

4. In `config.json` den Provider umstellen. `tibber_vehicle_id` ist die `externalId` aus der Geräte-Liste (bei Tesla Teslas interne Fahrzeug-ID, nicht die VIN). Nur nötig, wenn mehrere Fahrzeuge im Tibber-Konto sind; sonst meldet das Skript im Log, welche zur Auswahl stehen.

```json
"VEHICLE": {
    "enabled": true,
    "provider": "tibber",
    "vehicle_name": "Model 3",
    "battery_capacity_kwh": 60.0,
    "tibber_vehicle_id": "externalId-aus-der-Geräteliste"
}
```

**Aktualität:** Tibber holt die Werte nach eigenem Rhythmus beim Hersteller ab. Der Zeitpunkt steht als „zuletzt gesehen" im Log (`--log-level INFO`). Für die Ladeplanung über Nacht reicht das; „eingesteckt" und „lädt" können aber um einige Zeit hinterherhinken.

### **3b. Alternative: Tessie für Tesla-Telemetrie**

Tessie ist die pragmatischste Alternative zur Tesla Fleet API (kein Public-Domain-OAuth-Setup, kein Virtual-Key-Pairing). Vampire-Drain-Schutz ist eingebaut — das Skript nutzt ausschließlich Tessies internen Cache, weckt das Auto **nie** selbst.

1. **Konto erstellen** auf [tessie.com](https://tessie.com) und mit deinem Tesla-Account verbinden.
2. **Trial nutzen** oder Plan abschließen (ohne Plan keine API-Zugriffe).
3. **API-Token holen** unter [dash.tessie.com/settings/api](https://dash.tessie.com/settings/api). „Long-lived token" generieren.
4. **VIN deines Autos:** in der Tesla-App unter „Software" → „Fahrzeug-ID". Oder in Tessie unter „Vehicle Settings".
5. **In `config.json`:**

```json
"VEHICLE": {
    "enabled": true,
    "provider": "tessie",
    "vehicle_name": "Model 3",
    "battery_capacity_kwh": 60.0,
    "vin": "5YJ3...",
    "api_token": "dein-tessie-bearer-token"
}
```

`battery_capacity_kwh` ist modellabhängig: Model 3 RWD 2023 ≈ 60, Long Range ≈ 75, Performance ≈ 82, Model Y ≈ 75 (LR) bzw. 60 (RWD).

**Ohne Fahrzeug-Anbindung:** setze `"provider": "manual"` und gib `"manual_soc"` / `"manual_connected"` selbst an — gut für Trockenläufe ohne Hardware-Steuerung.

### **4. Home Assistant + Wallbox**

Das Skript schaltet keinen Hardware-Befehl direkt. Es setzt einen `input_boolean` in HA an/aus, und eine HA-Automation hängt diesen Schalter an deine echte Wallbox-Steuerung. Das hat zwei Vorteile: (a) das Skript bleibt hardware-agnostisch, (b) du kannst in HA zusätzliche Bedingungen (PV-Überschuss, Anwesenheit, etc.) dazwischenschalten.

**Die drei Helfer und ihre Rolle**

| Entity | Wer schaltet | Bedeutung |
|---|---|---|
| `input_boolean.tibber_charging_active` („Tibber: Laden freigegeben") | das Skript | `on` = jetzt liegt ein geplanter Ladeblock an |
| `input_boolean.tibber_charging_control` („Tibber: Ladesteuerung aktiv") | du | `on` = das Skript steuert die Wallbox. `off` = Wallbox verhält sich wie ohne Skript. Umschalten auf `off` gibt die Wallbox sofort frei (**„Sofort laden"**) |
| `input_datetime.tibber_last_heartbeat` („Tibber: letzter Heartbeat") | das Skript | Zeitpunkt des letzten Laufs, für den Notaus |

**Achtung:** Bei aktiver Ladesteuerung ist die Wallbox gesperrt, solange kein Ladeblock geplant ist. Ohne `#charge`-Termin lädt das Auto dann nicht. Zum spontanen Laden die Ladesteuerung ausschalten.

**Schritt 1: Token holen**
* In HA: **Profil → Sicherheit → Langlebige Zugangstoken → Token erstellen**.
* Token kopieren (wird nur einmal angezeigt).

**Schritt 2: Helfer anlegen** (Einstellungen → Geräte & Dienste → Helfer → Helfer erstellen, oder per YAML):

```yaml
input_boolean:
  tibber_charging_active:
    name: "Tibber: Laden freigegeben"
    icon: mdi:ev-station
  tibber_charging_control:
    name: "Tibber: Ladesteuerung aktiv"
    icon: mdi:robot

input_datetime:
  tibber_last_heartbeat:
    name: "Tibber: letzter Heartbeat"
    has_date: true
    has_time: true
```

In der Oberfläche den Helfer zuerst mit dem technischen Namen (z. B. `tibber_charging_active`) anlegen und danach umbenennen. Die Entity-ID entsteht aus dem ersten Namen.

**Schritt 3: `config.json` ergänzen**

```json
"HOMEASSISTANT": {
    "enabled": true,
    "base_url": "http://homeassistant.local:8123",
    "api_token": "dein-long-lived-access-token",
    "charging_entity_id": "input_boolean.tibber_charging_active",
    "heartbeat_entity_id": "input_datetime.tibber_last_heartbeat",
    "plug_entity_id": "binary_sensor.keba_p30_plug",
    "timeout_seconds": 5.0
}
```

`base_url` ist die URL, unter der dein HA aus dem Skript heraus erreichbar ist. `plug_entity_id` ist optional: der Stecker-Sensor der Wallbox. Er ersetzt den Steckerstatus vom Fahrzeug-Provider, weil Tibber diesen teils stundenlang verzögert meldet.

**Schritt 4: KEBA in HA** (falls noch nicht vorhanden)

Beispiel für eine KEBA P30 (c- oder x-series) über die [KEBA-Integration](https://www.home-assistant.io/integrations/keba/). Voraussetzung: Die KEBA hängt per LAN-Kabel im Netz, und die UDP-Smart-Home-Schnittstelle ist per DIP-Schalter **DSW1.3 = ON** aktiviert (hinter der Frontabdeckung, danach Wallbox einmal stromlos machen). Die Integration ist eine Legacy-Integration und wird per YAML eingerichtet:

```yaml
keba:
  host: 192.168.x.y   # IP der Wallbox
```

**Schritt 5: Automation „Wallbox-Freigabe"**

Greift nur bei aktiver Ladesteuerung. Ausschalten der Ladesteuerung gibt die Wallbox frei.

```yaml
- id: tibber_charging_wallbox
  alias: "Tibber: Wallbox-Freigabe"
  mode: queued
  triggers:
    - trigger: state
      entity_id: input_boolean.tibber_charging_active
      to: ["on", "off"]
      id: active
    - trigger: state
      entity_id: input_boolean.tibber_charging_control
      to: ["on", "off"]
      id: control
    - trigger: homeassistant
      event: start
      id: start
  actions:
    - choose:
        - conditions:
            - condition: trigger
              id: control
            - condition: state
              entity_id: input_boolean.tibber_charging_control
              state: "off"
          sequence:
            - action: keba.enable
        - conditions:
            - condition: state
              entity_id: input_boolean.tibber_charging_control
              state: "on"
            - condition: state
              entity_id: input_boolean.tibber_charging_active
              state: "on"
          sequence:
            - action: keba.enable
        - conditions:
            - condition: state
              entity_id: input_boolean.tibber_charging_control
              state: "on"
          sequence:
            - action: keba.disable
```

`keba.enable` gibt die Wallbox frei, `keba.disable` sperrt sie. Bei anderen Wallboxen (Go-eCharger, Easee, etc.) die beiden Aktionen durch die der jeweiligen HA-Integration ersetzen.

**Wichtig:** Wenn in der Tibber-App „Smart Charging" für Auto oder Wallbox aktiv ist, steuert Tibber den Ladevorgang selbst. Das kollidiert mit diesem Skript. Nur eines von beiden nutzen.

**Schritt 6: Automation „Notaus" (sehr empfohlen)**

Ohne diesen Schutz bleibt die Wallbox freigegeben, wenn das Skript abstürzt oder der Server ausfällt. Diese Automation nimmt die Freigabe nach 20 Minuten ohne Heartbeat zurück:

```yaml
- id: tibber_charging_notaus
  alias: "Tibber: Notaus bei fehlendem Heartbeat"
  mode: single
  triggers:
    - trigger: time_pattern
      minutes: "/5"
  conditions:
    - condition: state
      entity_id: input_boolean.tibber_charging_active
      state: "on"
    - condition: template
      value_template: >
        {{ (as_timestamp(now()) - (state_attr('input_datetime.tibber_last_heartbeat', 'timestamp') or 0)) > 1200 }}
  actions:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.tibber_charging_active
    - action: persistent_notification.create
      data:
        title: "Tibber Smart Charging"
        message: "Kein Heartbeat seit über 20 Minuten. Ladefreigabe wurde zurückgenommen."
```

Das ersetzt keinen echten Hardware-Watchdog der KEBA (siehe `TODO.md`), deckt aber den häufigsten Ausfall ab: Das Skript läuft nicht mehr, die Wallbox bleibt an.

**Optional: Push bei Ladestart und Ladeende**

Reine HA-Automation, das Skript braucht dafür nichts. `notify.mobile_app_…` durch dein Gerät ersetzen.

```yaml
- id: tibber_charging_push
  alias: "Tibber: Push bei Ladestart und Ladeende"
  mode: queued
  triggers:
    - trigger: state
      entity_id: binary_sensor.keba_p30_charging_state
      from: "off"
      to: "on"
      for: { minutes: 1 }
      id: start
    - trigger: state
      entity_id: binary_sensor.keba_p30_charging_state
      from: "on"
      to: "off"
      for: { minutes: 1 }
      id: stop
  actions:
    - choose:
        - conditions: [{ condition: trigger, id: start }]
          sequence:
            - action: notify.mobile_app_dein_handy
              data:
                title: "▶️ Laden gestartet"
                message: >
                  Wallbox lädt mit {{ states('sensor.keba_p30_charging_power') }} kW,
                  {{ 'geplant (Tibber-Skript)' if is_state('input_boolean.tibber_charging_active', 'on') else 'manuell' }}.
        - conditions: [{ condition: trigger, id: stop }]
          sequence:
            - action: notify.mobile_app_dein_handy
              data:
                title: "⏹️ Laden beendet"
                message: "{{ states('sensor.keba_p30_session_energy') }} kWh in dieser Sitzung geladen."
```

---

## **Konfiguration im Überblick**

Komplettes `config.json`-Beispiel findest du in `config.example.json`. Die wichtigsten Blöcke:

| Block | Pflicht | Was |
|-------|---------|-----|
| `TIBBER_API_KEY` | ja | Token aus Tibber Developer |
| `GOOGLE_CALENDAR_ID` | ja | ID des Ziel-Kalenders |
| `FORECAST` | nein | `provider` (Default `epex_predictor`), `fallback_provider` (Default `open_meteo`), `region` (Default `DE`), Open-Meteo-Koordinaten (Default Lübeck), Horizont, Timeout. `enabled=false` → kein Forecast (Tibber-only, wie Ursprungsversion). `calendar_events_enabled=false` → Forecast nur für Planung, keine `#Vorhersage`-Events |
| `TIBBER_DATA_API` | nein | OAuth-Client (`client_id`, `client_secret`, `redirect_uri`) für den Fahrzeug-Akkustand über Tibber |
| `VEHICLE` | nein | Provider (`tibber`, `tessie` oder `manual`), `tibber_vehicle_id` bzw. VIN+Token, Batteriekapazität |
| `WALLBOX` | nein | `gross_kw` / `net_kw` deiner Wallbox (Default 11/10) |
| `CHARGING` | nein | Puffer, Slot-Länge, Cycle-Warnung |
| `HOMEASSISTANT` | nein | Default `enabled=false`. Mit Steuerung: URL+Token+Entity-IDs |

Geheimnisse (`config.json`, `service_account.json`, `tibber_data_token.json`) sind via `.gitignore` ausgeschlossen.

---

## **Bedienung**

### **Ladeziele anlegen**

Du legst einen normalen Termin im Ziel-Kalender an. Format-Beispiele für den Titel:

```
80% #charge                         → 80 %, kein Auto-Tag
80% #charge #model3                 → 80 %, Auto-Tag "model3"
[80]% #charge                       → 80 % (eckige Klammern erlaubt)
Abfahrt zur Arbeit 80% #charge      → 80 %, Freitext drumherum egal
#charge 90%                         → 90 %
```

* **Deadline** = Beginn-Zeit des Termins (also die Uhrzeit, zu der das Auto bereit sein muss). Die Dauer/Endzeit wird ignoriert.
* **All-Day-Events** werden mit Warnung übersprungen (keine konkrete Uhrzeit).
* **Werte außerhalb 1–100 %** werden abgelehnt.
* **Konflikte** (zwei Ziele mit identischer Deadline) erzeugen eine Log-Warnung — V1 unterstützt nur ein Auto, also keine parallele Ladung.
* **Description** des `#charge`-Termins wird vom Skript überschrieben — nichts Wichtiges hineinschreiben.

### **Manuell ausführen**

```bash
source .venv/bin/activate
python tibber-to-google-calendar.py              # WARNING-Level (cron-tauglich, leise)
python tibber-to-google-calendar.py --debug      # alles loggen
python tibber-to-google-calendar.py --log-level INFO
python tibber-to-google-calendar.py --help
```

### **Cron einrichten**

Für volles Smart-Charging brauchst du **alle 15 Minuten**:

```bash
crontab -e
```

```bash
TZ=Europe/Berlin
*/15 * * * * /pfad/zum/projekt/.venv/bin/python /pfad/zum/projekt/tibber-to-google-calendar.py >> /pfad/zum/projekt/cron.log 2>&1
```

`TZ=Europe/Berlin` ist wichtig, wenn dein Server auf UTC läuft (typisch für Linux/Docker). Sonst fallen Tibber-Tagesgrenzen (CEST) und Skript-Tagesgrenzen (UTC) auseinander → Mitternachts-spannende Preisperioden bekommen Duplikate im Kalender.

Für reinen Preis-Sync ohne Charging reichen 2× pro Tag:

```bash
TZ=Europe/Berlin
0 1,14 * * * /pfad/zum/projekt/.venv/bin/python /pfad/zum/projekt/tibber-to-google-calendar.py >> /pfad/zum/projekt/cron.log 2>&1
```

Die 14-Uhr-Ausführung holt den Day-Ahead-Markt von Tibber ab, die 01-Uhr-Ausführung räumt den neuen Tag auf.

### **Headless-Betrieb (Server ohne Bildschirm)**

Das Skript braucht im Betrieb keinen Browser und keine Oberfläche. Voraussetzungen auf dem Server: Linux (oder macOS), Python 3.9+, Cron und Netzzugriff auf Tibber, Google, EpexPredictor, Open-Meteo und dein Home Assistant. Windows wird nicht unterstützt (`fcntl`).

Auf den Server kopieren: das Repo, `config.json` und `service_account.json`. Dann dort ein eigenes venv anlegen (`python3 -m venv .venv` + `pip install -r requirements.txt`), venvs lassen sich nicht zwischen Rechnern kopieren.

Nur die Tibber-Anmeldung braucht einmalig einen Browser. Zwei Wege:

* **Auf dem Server mit `--headless`:** `python scripts/tibber_data_auth.py --headless` gibt einen Link aus. Den auf Laptop oder Handy öffnen und anmelden. Der Browser landet danach auf einer nicht ladbaren `localhost`-Seite. Deren komplette Adresse aus der Adresszeile kopieren und im Server-Terminal einfügen.
* **Per SSH-Tunnel:** `ssh -L 8765:localhost:8765 <server>` und dort `python scripts/tibber_data_auth.py` ohne Option starten. Den ausgegebenen Link im Browser des Laptops öffnen, die Rückleitung läuft durch den Tunnel zum Server.

Nicht empfohlen: `tibber_data_token.json` vom Laptop auf den Server kopieren und dann an beiden Stellen laufen lassen. Tibber tauscht das Erneuerungstoken bei jeder Nutzung aus; zwei Rechner würden sich gegenseitig aussperren. Wenn du die Datei kopierst, auf dem Laptop danach nicht mehr ausführen.

### **Kalibrierungs-Report**

Nach ein paar Tagen Live-Betrieb siehst du, wie gut die Prognose war:

```bash
python scripts/calibration_report.py
```

Zeigt MAE und Median-Bias zwischen prognostiziertem und realem Preis, getrennt nach Prognose-Quelle und aufgeschlüsselt nach Vorlauf und Preisbereich. Manuelles Nachjustieren ist nicht nötig: Die Umrechnung in Tibber-Preise lernt das Skript selbst.

---

## **Was im Kalender erscheint**

Das Skript erstellt vier Event-Typen, alle mit Hashtags zur Identifikation:

| Hashtag | Inhalt | Wann |
|---------|--------|------|
| `#Tibber` ⚡ | Stündliche Preis-Perioden (CHEAP/EXPENSIVE/NEGATIVE) | Tag 0–1, bei jedem Lauf neu erzeugt |
| `#Vorhersage` 🔮 | Geschätzte Preis-Perioden aus der Preisprognose | Tag 2–7, zweimal täglich aktualisiert |
| `#charging` 🔋 | Konkrete Lade-Blöcke (kommen aus Plänen) | nur bei vorhandenen `#charge`-Zielen |
| `#charge` (Input) | Du legst diese an. Skript überschreibt nur Description | Nutzer-gesteuert |

Negative-Preis-Stunden bekommen einen Popup-Reminder (30 min vorher + zum Start). Andere Events sind reminderlos.

---

## **Wie's funktioniert (Kurz-Konzepte)**

### **Smart Polling**

Tibber wird nicht bei jedem Cron-Tick abgefragt. `today`-Preise werden 1× pro Tag geholt, `tomorrow` ab 13:00 mit Backoff (15 min → 1 h) wenn Fehler. Die Preisprognose wird 2× täglich abgefragt (06:00 und 14:00). Der Fahrzeug-Status wird jeden Lauf gelesen: bei Tibber mit drei kleinen Abfragen (Home, Geräteliste, Gerätedetails), bei Tessie über den Cache-Endpoint. In beiden Fällen wird das Auto nie geweckt.

### **Lokaler State**

`state.json` (gitignored) hält den Cache:

* Tibber-Preise heute+morgen, Backoff-Timestamps
* Preisprognose (~168 Stunden, Rohwerte) und die zuletzt gelernte Umrechnung in Tibber-Preise
* Letzter bekannter Fahrzeug-Status
* Berechnete Pläne mit Hashes + erstellten Kalender-Event-IDs
* Letzter Wallbox-Schaltzustand
* Kalibrierungs-Samples (Forecast vs. realer Tibber-Preis, max. 500 FIFO)

Schreibvorgänge sind atomar (`tmp + os.replace + fsync`); `state.lock` verhindert parallele Cron-Läufe. Löschen ist immer sicher — nächster Lauf rebuilded.

### **15-Minuten-Slot-Granularität**

Tibber und die Prognose liefern stündliche Preise (EpexPredictors 15-Minuten-Werte werden zu Stundenmitteln zusammengefasst). Der Planer expandiert sie intern in 4×15-min-Sub-Slots gleichen Preises (`CHARGING.slot_minutes`, Default 15). Damit kann das Schaltfenster passgenau auf die benötigte Energiemenge zugeschnitten werden — z. B. 11:00–14:15 statt 11:00–15:00 bei 31 kWh Bedarf.

Außerdem werden die letzten ~1 kWh des teuersten gewählten Slots **cheapest-first** verteilt: günstige Stunden voll, der teuerste belegte Sub-Slot bekommt nur den Rest. Die ausgewiesene `expected_kwh` und `expected_cost_eur` matchen damit `kwh_needed`, nicht naiv `slots × kW`.

### **Hash-Diffing für Calendar-Events**

Alle drei Event-Klassen (`#Tibber`, `#Vorhersage`, `#charging`) nutzen Hash-Diffing: jeder Lauf hasht die aktuellen Eingabewerte und vergleicht mit dem gespeicherten Hash. Bei Übereinstimmung: **null** Google-Calendar-API-Calls für diese Event-Klasse. Erst bei Hash-Wechsel (neue Tibber-Preise, neuer Forecast, geänderter Plan) werden die alten Events gelöscht und neue erstellt.

- **`#Tibber` und `#Vorhersage`** löschen bei Hash-Wechsel **alle Events mit dem jeweiligen Hashtag** in einem weiten Zeitfenster (`delete_events_matching`). Das räumt auch Duplikate aus früheren fehlgeschlagenen Sync-Zyklen, Mitternachts-Stragglers und manuell kopierte Events mit weg — getrackte Event-IDs werden hier gar nicht gebraucht.
- **`#charging`** löscht per ID (jeder Plan hat seine eigenen Child-Event-IDs in `state.json`). Zusätzlich räumt ein Orphan-Scan vor jedem Sync `#charging`-Events ohne Tracking auf — robust gegen `state.json`-Verlust.

Bei 15-Min-Cron ergibt das pro Tag typischerweise 2-4 Tibber-Updates (heute + morgen-Refresh nach 13:00) und 2 Forecast-Updates (06:00 + 14:00) — alle anderen 96-Cron-Ticks machen 0 Calendar-API-Calls für Preis-Events.

### **Schalt-Idempotenz**

`_drive_charging` ruft die HA-API **nur** wenn sich der gewünschte Zustand ändert. Bei stabilem Lade-Block über mehrere Cron-Ticks: 0 HA-Calls. Heartbeat läuft trotzdem jeden Lauf.

### **Preisprognose und selbstlernende Umrechnung**

Für die Tage nach Tibbers Day-Ahead-Horizont nutzt das Skript [EpexPredictor](https://github.com/b3nn0/EpexPredictor): eine freie Open-Source-Prognose der Börsenpreise (Machine Learning auf Wetter an vielen Orten, Netzlast, Gaspreis, Kalender; lernt rollierend aus den letzten 180 Tagen). Die öffentliche Instanz läuft ohne Garantie; das Skript fragt sie nur 2× täglich ab. Antwortet sie nicht, springt die Wetter-Heuristik über Open-Meteo ein (`FORECAST.fallback_provider`). Für Selbst-Hosting `FORECAST.epex_predictor_url` umstellen.

EpexPredictor liefert Börsenpreise, Tibber berechnet daraus deinen Endpreis: **Tibber-Preis = Fixkosten + MwSt-Faktor × Börsenpreis** (im Oktober 2026 gemessen: 20,0 ct + 1,19 × Börsenpreis). Diese Formel lernt das Skript bei jedem Lauf selbst aus allen Stunden, für die es schon echte Tibber-Preise gibt, und speichert sie. Ändern sich Netzentgelt oder Steuern, passt sie sich ohne Zutun an. Dieselbe Formel gleicht bei der Wetter-Heuristik Preisniveau und Schwankungsbreite an.

**Kalibrierung:** Jede Prognose wird gespeichert und später mit dem echten Tibber-Preis verglichen. `python scripts/calibration_report.py` zeigt die Abweichung je Quelle, Vorlauf und Preisbereich.

Forecast-Preise bekommen einen **Risikoaufschlag** von 1–5 ct, der mit dem Horizont wächst. Damit bevorzugt der Planer immer einen sicheren Tibber-Preis gegenüber einer ähnlich-günstigen Forecast-Schätzung.

Heuristik ist bewusst grob — wir sehen Lokal-Wetter, nicht die nationale Erzeugungsbilanz. Verbesserungspfad in `TODO.md`.

---

## **Development**

### **Tests**

```bash
pip install -r requirements-dev.txt
pytest tests/
```

235 Tests covering Merger, Forecast (EpexPredictor + Open-Meteo + Corrently + Rückfall + Cache + Kalibrierung + selbstlernende Umrechnung), Vehicle (Manual + Tessie + Tibber), Planning (LinearStrategy inkl. 15-min-Expansion + Partial-Allokation), Calendar-Sync für alle drei Event-Typen (Hash-Diff + Hashtag-Delete für #Tibber/#Vorhersage, per-ID + Orphan-Cleanup für #charging, Pagination, per-Event Delete-Robustheit), Tibber-Day-Rollover (Yesterday-Cache für Mitternachts-spannende Perioden), Config-Parsing, Tibber Data API (OAuth-Token-Erneuerung), Hardware (HA-Client + Drive-Logik + Stecker-Sensor) und Parsing. `tests/conftest.py` injiziert `src/` in `sys.path` — kein `pip install -e .` nötig.

### **Repository-Layout**

```
tibber-to-google-calendar.py        # Cron-Entry-Point (Argparse + sys.path-Bootstrap)
src/tibber_charge/
  config.py                         # Config-Dataclasses + load_config()
  main.py                           # Orchestrator (run + _run_locked)
  state.py                          # State-Schema + atomic save + flock
  tibber/
    client.py                       # Tibber-GraphQL-Client
    cache.py                        # Smart-Polling-Logik + Backoff
    grouping.py                     # Preis-Level-Gruppierung
    data_api.py                     # Tibber Data API (OAuth, Fahrzeugdaten)
  prices/
    types.py                        # UnifiedPrice (Tibber + Forecast einheitlich)
    merger.py                       # Tibber-und-Forecast → sortierte Liste
    mapping.py                      # Selbstlernende Umrechnung Prognose → Tibber-Preis
    forecast/
      base.py                       # Provider Protocol
      epex_predictor.py             # EpexPredictorForecastProvider (Default)
      open_meteo.py                 # OpenMeteoForecastProvider (Rückfall)
      corrently.py                  # CorrentlyForecastProvider (paid tier, optional)
      cache.py                      # Forecast-Cache mit 2×/Tag Refresh
      levels.py                     # Quantilbasierte Level-Klassifizierung
      penalty.py                    # Lineare Penalty-Kurve (1-5 ct)
      calibration.py                # Forecast↔Tibber-Pairing für späteres Fitting
  vehicle/
    base.py                         # VehicleProvider Protocol + VehicleStatus
    manual.py                       # Config-basiert
    tessie.py                       # Tessie API (use_cache=true)
    tibber.py                       # Tibber Data API (Fahrzeuge aus dem Tibber-Konto)
    cache.py                        # fetch_status_with_cache mit Backoff
  planning/
    types.py                        # ChargeBlock, ChargePlan
    base.py                         # ChargingStrategy Protocol
    linear.py                       # LinearStrategy (V1)
  gcal/
    auth.py                         # Service-Account-Auth
    events.py                       # create / delete / patch / list (paginiert, per-Event Fehler-Toleranz)
    charge_targets.py               # #charge-Event-Parser
    price_events_sync.py            # Hash-Diff + Hashtag-Delete für #Tibber/#Vorhersage
    charging_sync.py                # Hash-Diff + per-ID + Orphan-Cleanup für #charging
  hardware/
    homeassistant.py                # HA-REST-Client
scripts/
  calibration_report.py             # Adhoc-Auswertung der Forecast-Genauigkeit
  tibber_data_auth.py               # Einmalige Anmeldung an der Tibber Data API + Geräte-Übersicht
tests/                              # pytest-Suite
CLAUDE.md                           # Interne Architektur-Notizen
TODO.md                             # V2-Roadmap + technische Schulden
```

### **Wartung**

* **Dependencies aktualisieren:** `pip freeze > requirements.txt` (Runtime), entsprechend für `requirements-dev.txt`.
* **Logs sind deutsch.** Beim Editieren bitte beibehalten — Konsistenz mit Bestand.
* **Python 3.9, 3.10, 3.11, 3.13** getestet. Frühere Versionen funktionieren nicht (Pattern-Matching-Syntax in einigen Modulen).

### **Zukunfts-Pläne**

Siehe `TODO.md` für:
* V2-Forecast-Verbesserungen (Multi-Location, Hour-of-Day-Faktor, Regression-Modell auf gesammelten Kalibrierungs-Daten)
* Hardware-Failsafe (KEBA Modbus-Watchdog) für produktiven Betrieb
* Edge-Cases (Kabel-vergessen-Push-Notification, Algorithmische Block-Bonus statt nur Cycle-Warnung)
