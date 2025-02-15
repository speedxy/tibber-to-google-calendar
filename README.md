# Tibber zu Google Calendar

Dieses Skript ruft die aktuellen Strompreise von [Tibber](https://tibber.com) ab und trägt relevante Zeiträume als Ereignisse in den Google Kalender ein.

## 🔧 Installation & Einrichtung

### 1️⃣ Repository klonen
```sh
git clone https://github.com/speedxy/tibber-to-google-calendar.git
cd tibber-to-google-calendar
```

### 2️⃣ Abhängigkeiten installieren
Das Skript benötigt Python 3 und die Pakete in `requirements.txt`:

```sh
pip install -r requirements.txt
```

### 3️⃣ API-Zugangsdaten einrichten
Kopiere die Datei `config.example.json` und benenne sie in `config.json` um:

```sh
cp config.example.json config.json
```

Öffne `config.json` und trage deine API-Keys und Werte ein:

```json
{
  "TIBBER_API_KEY": "dein-tibber-api-key-hier",
  "GOOGLE_CALENDAR_ID": "dein-google-kalender-id-hier"
}
```

#### 🔐 Tibber Access Token erhalten
1. Gehe auf [Tibber Developer](https://developer.tibber.com/).
2. Melde dich mit deinem Tibber-Account an.
3. Erstelle einen Access Token und kopiere ihn in `config.json` unter `TIBBER_API_KEY`.

#### 👤 Google API Zugang einrichten
1. Öffne die [Google Cloud Console](https://console.cloud.google.com/).
2. Erstelle ein neues Projekt oder wähle ein bestehendes.
3. Aktiviere die **Google Calendar API** unter `APIs & Dienste`.
4. Erstelle Zugangsdaten vom Typ **OAuth 2.0 Client-ID**.
5. Lade die Datei `your-client-secret.json` herunter und speichere sie im Projektverzeichnis.

#### 📅 Google Kalender-ID abrufen
1. Öffne Google Kalender unter [calendar.google.com](https://calendar.google.com/).
2. Klicke auf das Zahnrad-Symbol ⚙ und wähle `Einstellungen`.
3. Wähle den gewünschten Kalender aus und kopiere die **Kalender-ID**.
4. Trage die ID in `config.json` unter `GOOGLE_CALENDAR_ID` ein.

### 4️⃣ Ersten Lauf starten
```sh
python tibber-to-google-calendar.py
```

Beim ersten Start wirst du zur Authentifizierung bei Google aufgefordert. Danach speichert das Skript das OAuth-Token in `token.json`.

## ⚙️ Automatisierung mit Cron (optional)
Füge das Skript in den Cron-Job-Manager ein, um es täglich um 18:00 Uhr auszuführen:

```sh
crontab -e
```

Füge folgende Zeile hinzu:
```sh
0 18 * * * /usr/bin/python3 /pfad/zu/tibber-to-google-calendar.py
```

## 🛠️ Fehlersuche
Falls es Probleme gibt, aktiviere das Logging:

```sh
python tibber-to-google-calendar.py > log.txt 2>&1
```

Prüfe `log.txt` auf Fehlermeldungen.