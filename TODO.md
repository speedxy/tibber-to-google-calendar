# TODO

Geplante Arbeiten ab dem aktuellen Stand (Phase 6 + PR 4 Kalibrierungs-Logging erledigt).
Phasen-Plan zur Smart-Charging-Erweiterung ist in `CLAUDE.md` und der Conversation-Historie dokumentiert; dieses File fokussiert auf das, was als nächstes ansteht und wo wir bewusst Schulden für später aufgenommen haben.

---

## V2 — Forecast-Qualität verbessern

**Ausgangsproblem:** Die V1-Open-Meteo-Heuristik (Wind × Solar einer Lokation → linearer Index → Anker-Tabelle → €/kWh) reduziert ein 5-6-dimensionales Pricing-Problem auf 2D. Beobachtet sind nicht nur Skalierungsfehler (linear korrigierbar), sondern gegenläufige Bias je nach Tageszeit/Tag — Hinweis auf **fehlende Features**, nicht falsche Gewichtung.

**Was die Heuristik aktuell ignoriert und warum es weh tut:**

| Fehlender Faktor | Effekt | Symptom |
|---|---|---|
| Nationale Erzeugung (statt nur Lübeck) | Lübeck windig, Bayern flau → wir sagen "billig", EPEX sagt teuer | Wind-Stunden mit überraschend hohen Preisen |
| Süd-DE-Solar-Dominanz | Mittags wird der Markt von Bayern-PV bestimmt, Lübecks PV ist 50 % schwächer | Mittags zu konservative Preise |
| Tageslastkurve | Demand-Peaks 7-9 Uhr und 18-20 Uhr unabhängig vom EE-Anteil | Morgens überraschend teuer trotz Wind |
| Wochentag | Sa/So strukturell günstiger (weniger Industrie) | Sa-Bias |
| Spot vs Tibber-Gesamt | Spot kann <0 ct, Tibber-Gesamt hat Floor durch fixe Kostenkomponenten | Curve-Endpunkte schwer zu kalibrieren |
| Batteriespeicher-Glättung | Seit 2024 dämpfen Großspeicher Spitzen | Modellierte Volatilität zu hoch |

### Plan

**1. Daten sammeln (läuft bereits — PR 4 ist seit Phase 4b live).**

Mit dem aktuellen Stand werden bei jedem fresh-Forecast für jede Stunde folgende Features in `state.forecast.calibration_samples[]` geloggt:

- **Forecast-Outputs:** `forecast_raw_total`, `forecast_total`, `forecast_source`, `hours_ahead_at_forecast`
- **Provider-Rohfeatures:** `wind_kmh`, `solar_wm2`, `temperature_c`, `renewable_index`, `latitude`, `longitude` (Open-Meteo) bzw. `gsi`, `zip_code` (Corrently)
- **Kontext (aus `target_hour` abgeleitet):** `hour_of_day_local`, `weekday`, `month`
- **Reality (post-hoc gefüllt):** `actual_total`, `actual_tibber_level`, `actual_filled_at`

500 Samples FIFO mit Preference für gefüllte Pairs. Nach ~1 Woche Live-Betrieb hast du etwa 168 (Tibber-Horizont) + Forecast-Stunden gefüllte Paare zum Auswerten.

`scripts/calibration_report.py` druckt MAE/Bias-Buckets — gut für manuelle Anker-Justierung, aber nicht das Endziel.

**2. Multi-Location-Fetch (Open-Meteo) — leichtester nächster Schritt.**

Ein zweiter Open-Meteo-Call für eine süddeutsche Koordinate (München 48.14/11.58 z. B.). Wind/Solar dieser Lokation in den Renewable-Index mit einbinden — Gewichtung z. B. 50 % Lübeck + 50 % München. Bessere räumliche Auflösung.

Kosten: zusätzlicher HTTP-Call, +1-2 zusätzliche Felder im Sample.

**3. Hour-of-day als zusätzlicher Faktor.**

Demand folgt einer reproduzierbaren Tageskurve. Im V2-Modell:

```
forecast_total = f_weather(wind, solar, temp) + f_demand(hour, weekday)
```

`f_demand` kann erstmal eine fest hinterlegte Stundenkurve sein (Sinus mit Peak 8 Uhr + Peak 19 Uhr, Tal nachts/mittags), oder aus den `actual_total`-Samples per Gruppen-Mittel gelernt werden.

**4. Linearmodell statt Anker-Tabelle.**

Sobald ≥200 gefüllte Sample-Pairs vorliegen, regression-fit:

```
actual_total ~ a*wind + b*solar + c*temp + d*sin_hour + e*cos_hour + f*weekend + g
```

Werkzeug: stdlib `statistics` reicht für simple lineare Regression, oder `numpy`/`scikit-learn` falls eh installiert. Output ist ein Vektor von Koeffizienten — den persistieren wir in `state.forecast.fitted_coefficients`.

Vorteil: keine Anker-Anpassung mehr per Hand, das Modell aktualisiert sich aus den Daten.

**5. Optional: Fraunhofer Energy-Charts als Zweit-Provider.**

National-aggregierte Wind/Solar-GW als Inputs. Liefert ~48h Horizont — würde nur die nahe Zukunft besser machen, hilft aber wenig für h>48 wo Open-Meteo der einzige Anbieter mit 7-d-Horizont ist.

Bewertung: aufschiebbar, evtl. nie nötig wenn V2-Multi-Location reicht.

### Abhängigkeiten / Reihenfolge

- (1) läuft schon.
- (2) und (3) können parallel und vor (4) implementiert werden — sie verbessern V1 ohne Fit.
- (4) braucht ≥200 Sample-Pairs → frühestens 1-2 Wochen Live-Betrieb.
- (5) ist optional, am Ende.

---

## Hardware-Failsafe (KEBA-Watchdog) — vor Phase 7 produktiv

Der ursprüngliche Architektur-Plan hat die fehlende Hardware-Sicherung als bewusstes V1-Risiko notiert. Vor produktivem Betrieb der Wallbox-Steuerung (Phase 7) sollte das adressiert sein:

- **Modbus-TCP-Watchdog** der KEBA P30 c-series konfigurieren: setzt einen Timeout (z. B. 60 s); wenn der Heartbeat ausbleibt, fällt der Schütz hardwareseitig ab.
- Aus dem Python-Skript heraus: regelmäßiger Heartbeat-Befehl via `pymodbus` (oder UDP nach KEBA-Spezifikation).
- Implementierungspfad: separates Modul `hardware/keba.py`, ähnliche Cache-/Backoff-Pattern wie Tessie.

Ohne dieses Failsafe kann ein Python-Crash mit gesetztem `input_boolean=on` zu unkontrolliertem Weiterladen führen (vom User in der initialen Architekturdiskussion bewusst für V1 akzeptiert).

---

## Phase 7 — HA-Steuerung (erledigt)

Erledigt am 2026-05-21: `HOMEASSISTANT`-Config-Block, `hardware/homeassistant.py` mit `set_input_boolean()` und `write_input_datetime()`, Heartbeat optional. `_drive_charging` in `main.py` schaltet idempotent (nur bei State-Wechsel).

---

## Phase 8 — Edge Cases (offen)

- **Kabel-vergessen-Notification:** wenn ein Ladeslot beginnt und `vehicle_status.is_connected==False` → HA-Push-Notification senden.
- **Zyklen-Limit härter durchsetzen:** statt nur warnen, im Allokator einen Block-Bonus einbauen (gleicher Preis → bevorzugt zusammenhängend). Insbesondere relevant geworden seit 15-min-Granularität — mehr Fragmentierungspotenzial.

---

## Bekannte technische Schulden

- **Schema-Migrationen:** `state.json` ist Schema-Version 1. Bei Breaking-Change muss `load_state` einen Migration-Pfad bekommen (oder den State pragmatisch verwerfen — aktuell der Default).
- **No CI:** keine GitHub Actions o.ä. konfiguriert. Lokales `pytest` ist der einzige Gate. Bei zwei Mitwirkenden ändert sich das.
- **`tibber-to-google-calendar.py` als Entry-Point-Wrapper** ist OK, aber bei Wachstum sollte irgendwann ein `pyproject.toml` mit `[project.scripts]`-Entry-Point dazu. Aktuell unnötig.
- **Kein Strukturierungs-Schritt im README:** der Pfad "Tibber-Setup → Service-Account → Tessie → HA" wird länger und länger. Vor V2-Release ggf. README in mehrere Dokumente splitten.
- **`config.example.json` hat keine Sektion-Kommentare** (JSON erlaubt keine). Wenn das zur Reibung wird: Wechsel auf YAML (war im ursprünglichen Plan diskutiert, abgelehnt für V1).
