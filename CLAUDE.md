# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate venv (always required before running)
source .venv/bin/activate

# Run the script (one-shot; intended to be invoked from cron)
python tibber-to-google-calendar.py

# Debug a single run end-to-end (verbose logs)
python tibber-to-google-calendar.py --debug
python tibber-to-google-calendar.py --log-level INFO   # gezielter

# Update dependencies after changing libraries
pip freeze > requirements.txt
```

### Tests

```bash
pip install -r requirements-dev.txt   # one-time: pytest + responses
pytest tests/                          # runs the full suite
pytest tests/test_merger.py -v         # single file
```

`tests/conftest.py` injects `src/` into `sys.path`, so no `pip install -e .` needed. Currently 235 tests covering merger, EpexPredictor provider + fallback, self-learning price mapping, UnifiedPrice, GSI mapping, penalty curve, both Corrently and Open-Meteo providers (with `responses` HTTP mock), forecast cache lifecycle, dynamic levels, calibration logging (record/match/trim + meta-features), manual + Tessie + Tibber vehicle providers, vehicle cache lifecycle, LinearStrategy planning (incl. 15-min sub-slot expansion + partial allocation), charging sync, Tibber + Vorhersage hash-diff sync (incl. orphan-cleanup auch im Steady-State), gcal events pagination, Tibber day-rollover (yesterday-Cache), Home Assistant client (incl. get_state), `_drive_charging` logic, wallbox plug sensor override, config parsing and the Tibber Data API client (PKCE, token refresh, 401 retry).

## Repo layout

```
tibber-to-google-calendar.py    # Thin CLI wrapper (argparse + sys.path bootstrap)
src/tibber_charge/
  config.py                     # load_config() → Config dataclass
  main.py                       # run(base_dir, log_level) — orchestrator
  state.py                      # State/TibberCache dataclasses, atomic save, flock
  tibber/
    client.py                   # fetch_tibber_prices() — GraphQL POST
    cache.py                    # should_fetch(), fetch_with_cache() — Smart-Polling
    grouping.py                 # group_price_periods() + PRICE_LEVELS tuple
    data_api.py                 # TibberDataClient — OAuth (PKCE + Refresh-Token) für data-api.tibber.com
  gcal/
    auth.py                     # authenticate() — Service Account creds
    events.py                   # create_event(→id) / delete_events_matching() / delete_event_by_id() / update_event_description() / list_events_matching()
    charge_targets.py           # parse_title(), load_charge_targets(), detect_conflicts()
    charging_sync.py            # sync_charge_plans() — #charging-Events mit Hash-Diffing
    price_events_sync.py        # sync_tibber_events() + sync_forecast_events() — Hash-Diffing für #Tibber + #Vorhersage
  prices/                       # provider-agnostischer Preis-Layer
    types.py                    # UnifiedPrice (Tibber + Forecast einheitlich)
    merger.py                   # unified_prices() — Tibber wins on overlap
    mapping.py                  # fit_mapping()/apply_mapping() — selbstlernende Umrechnung Prognose → Tibber-Preis
    forecast/
      base.py                   # PriceForecastProvider Protocol
      cache.py                  # ForecastCache mit Smart-Polling (2× pro Tag)
      epex_predictor.py         # EpexPredictorForecastProvider (Default — Börsenpreis-Prognose)
      open_meteo.py             # OpenMeteoForecastProvider (Rückfall — Wetter-Proxy, no auth)
      corrently.py              # CorrentlyForecastProvider + gsi_to_total() (optional, Token-pflichtig)
      penalty.py                # forecast_penalty() + forecast_confidence()
      levels.py                 # compute_dynamic_level() + apply_dynamic_levels() — Quantil-basiert
      calibration.py            # record_predictions() + match_observations() — Kalibrierungs-Logging
  vehicle/                      # Fahrzeug-Telemetrie (SoC + Steckverbindung)
    base.py                     # VehicleProvider Protocol + VehicleStatus
    manual.py                   # ManualVehicleProvider (Default — Config-basiert)
    tessie.py                   # TessieVehicleProvider (use_cache=true, kein Wake)
    tibber.py                   # TibberVehicleProvider (Tibber Data API, Default-Empfehlung)
    cache.py                    # fetch_status_with_cache mit Backoff
  hardware/                     # externe Steuerung
    homeassistant.py            # HomeAssistantClient (set_input_boolean, write_input_datetime)
scripts/
  calibration_report.py         # Adhoc-Tool: MAE + Bias der Forecast-Heuristik aus state.json
  planning/                     # Ladeplanung
    types.py                    # ChargeBlock + ChargePlan + plan_hash
    base.py                     # ChargingStrategy Protocol
    linear.py                   # LinearStrategy (V1 — linear, Block-Merging, Zyklen-Warnung)
tests/                          # pytest, src/ über conftest.py auf sys.path
```

The wrapper inserts `src/` into `sys.path` and calls `tibber_charge.main.run()`. No `pip install -e .` needed — keeps the cron path stable.

## State persistence

`state.json` (gitignored) holds the Tibber price cache, fetch timestamps, and backoff state. Schema is versioned via `SCHEMA_VERSION` in `state.py`; on mismatch the file is discarded and rebuilt. Writes are atomic (`tmp + os.replace + fsync`) — a `kill -9` mid-write leaves the previous file intact.

`state.lock` is acquired exclusively via `fcntl.flock` (POSIX-only — works on macOS/Linux, not Windows). If a second instance starts while the first is running, it logs a warning and exits without touching anything.

**`state.json` is local cache, not source of truth.** Deleting it is always safe; the next run rebuilds from Tibber.

## Architecture

Single-purpose script, no framework, no web server, no persistence — each invocation is a complete sync.

`run()` in `src/tibber_charge/main.py` does:
1. Loads `config.json` (TIBBER_API_KEY, GOOGLE_CALENDAR_ID) from the wrapper's directory (`base_dir`), not CWD — important for cron.
2. Acquires `state.lock`. If another instance holds it, exits without sync.
3. Loads `state.json` and calls `fetch_with_cache()` (`tibber/cache.py`), which decides whether to hit the Tibber API. Decision matrix:
   - `today_date != now.date()` → fetch (always need today).
   - `tomorrow_date == now.date()+1` → skip (already cached).
   - `now.hour < 13` → skip (Tibber hasn't published tomorrow yet).
   - `next_retry_after > now` → skip (backoff active after a 504/network error; doubles from 15min up to 1h).
   - `tomorrow_given_up_for_date == today` → skip (gave up after 23:00).
   - Otherwise: fetch.
   On HTTP/network errors, backoff is applied and the cached prices are returned. On parse errors (unexpected response shape), no backoff — same cache returned.
4. Saves the state back to disk (even if Tibber failed — the backoff/error fields belong in state).
5. If the cache is empty (first run + Tibber down), the calendar sync is skipped — the script does **not** wipe the calendar when it has nothing to put back.
6. Groups consecutive hourly prices into contiguous periods via `group_price_periods`. Levels: `NEGATIVE` (any hour with `total < 0`, overrides Tibber's own level), `CHEAP`, `VERY_CHEAP`, `EXPENSIVE`, `VERY_EXPENSIVE`. `NORMAL` hours act as period terminators and produce no events. A negative-price hour is *only* in `NEGATIVE`, never also in `CHEAP`/`VERY_CHEAP` — this is deliberate; don't "fix" it.
7. Deletes all existing events in the Tibber data's time range whose summary contains `#Tibber`, then inserts fresh events. The `#Tibber` tag in the event summary is the idempotency key — do not change it without also updating the delete filter in `delete_events_matching` (`src/tibber_charge/gcal/events.py`). Note: every run still does delete+recreate — Hash-based diffing comes in Phase 6.
8. Event titles encode the price range and level via `_build_tibber_event()` in `main.py`, e.g. `⚡ Niedriger Strompreis: 12.3ct - 15.4ct [CHEAP] #Tibber`. Prices are stored in cents (`total * 100`). Title prefix: `Negativer Strompreis` for `NEGATIVE`, `Niedriger Strompreis` for `CHEAP`/`VERY_CHEAP`, else `Strompreis`.
9. **Reminders**: only `NEGATIVE` events get popup reminders (30 min before + at start). All other events are created with `useDefault: False` and no overrides — this is intentional to keep the calendar quiet despite filling it with events. Pass `reminders_minutes=[...]` to `create_event` to opt in per call.

`gcal/auth.py` authenticates via a Service Account JSON (`service_account.json`), scope `calendar.events`. The service account email must be shared on the target calendar with "Make changes to events" permission — failures here surface as 403s from the Google API, not auth errors.

Event times are written with `timeZone: 'UTC'` while `startsAt` from Tibber is already timezone-aware ISO (typically with a CET/CEST offset). `isoformat()` preserves the offset, so events display correctly in the calendar despite the `'UTC'` label.

## Forecast layer

For planning beyond Tibber's ~48h day-ahead horizon, the system pulls a renewable-energy proxy forecast from Corrently's GSI API and maps it to estimated €/kWh prices.

- **Provider (default, seit 2026-10-19)**: `EpexPredictorForecastProvider` (`prices/forecast/epex_predictor.py`) ruft `https://epexpredictor.batzill.com/prices_short?region=DE&surcharge=0&taxPercent=0&unit=EUR_PER_KWH&hours=168` ab. Antwort: parallele Arrays `s` (Epoch-Sekunden, 15-min-Raster) und `t` (€/kWh netto Börsenpreis). Für schon auktionierte Stunden echte Börsenpreise, danach LightGBM-Prognose (Backtest DE: MAE ~1,7-1,9 ct netto für 1-3 Tage). Wird zu Stundenmitteln aggregiert. `raw_total` = Börsenpreis, NICHT Endpreis; ohne gelernte Umrechnung verwirft `main._map_forecast_to_tibber` diese Prognose. Öffentliche Instanz ohne Garantie ("fair use"), Self-Hosting via Docker möglich (`FORECAST.epex_predictor_url`, ENTSO-E-Key empfohlen).
- **Rückfall**: `FORECAST.fallback_provider` (Default `open_meteo`). `prices/forecast/cache._fetch_with_fallback` versucht bei Fehler des Haupt-Providers einmal den Rückfall; erst wenn auch der scheitert, greifen Backoff + Cache.
- **Provider (Rückfall)**: `OpenMeteoForecastProvider` calls `https://api.open-meteo.com/v1/forecast?latitude=X&longitude=Y&hourly=wind_speed_100m,shortwave_radiation&forecast_days=7&timezone=UTC`. Anonymous, no auth, ~168 hourly entries. Maps `(wind_kmh, solar_wm2)` → renewable index 0–100 via `weather_to_renewable_index` (70% wind weight, 30% solar — Germany is wind-dominated). Wind reference is **30 km/h = 100 %** (initial 60 km/h was unreachable for Lübeck — P90 of the 7-day forecast was only 26 km/h, biasing the index permanently low). Index then through `_GSI_ANCHORS` in `corrently.py` → €/kWh: 0.42 at index 0 (winter peak) → 0.25 at 50 (median, fixed-cost floor) → 0.10 at 100 (max renewable surplus, taxes/grid prevent going lower).
- **Provider (optional)**: `CorrentlyForecastProvider` at `https://api.corrently.io/v2.0/gsi/prediction?zip=<PLZ>&token=<TOKEN>`. **Anonymous access (incl. free signup token) perma-returns `forecast: []`** with `"info": "Realtime computation in progress"` — only a paid Corrently tier delivers actual data. Kept in code as a Strategy-Pattern fallback for users with paid access. Logs an actionable RuntimeError pointing to console.corrently.io when empty.
- **Penalty curve** (`prices/forecast/penalty.py`): forecast prices get a risk premium that grows with horizon — 0 ct under 36 h, +1 ct at 36–48 h, linear +1 → +3 ct from 48 h to 96 h, linear +3 → +5 ct from 96 h to 168 h (capped at +5 ct). Mirror `forecast_confidence` (1.0 → 0.30) used for display.
- **Polling**: `should_fetch_forecast` refreshes 2× per day (06:00 + 14:00 local, after EPEX auction). On error: backoff 30 min → 1 h → 2 h → 4 h cap. Stale cache is returned on failure — never an empty list (unless cache is genuinely empty).
- **Merger** (`prices/merger.py`): `unified_prices(tibber, forecast, now, horizon=7d)` produces a sorted hourly vector. Tibber wins on every overlap hour. Past Tibber hours are kept (calendar visualizes them); past forecast hours are dropped. Gaps are logged but not synthesized.
- **Selbstlernende Umrechnung** (`prices/mapping.py`, ersetzt die kurzlebige Median-Niveau-Verschiebung im Merger): `main._map_forecast_to_tibber` fittet bei jedem Lauf `tibber.total = a + b × forecast.raw_total` per kleinster Quadrate über alle Stunden mit beiden Werten (Tibber yesterday/today/tomorrow), ab `MIN_OVERLAP` = 12. Gemessen 2026-10-19 mit EpexPredictor: a = 20,0 ct, b = 1,19 (= Fixkosten + MwSt, exakt). Für die Wetter-Heuristik ergibt sich b ≈ 0,2 (sie erklärt nur einen kleinen Teil der Schwankung) — gewollt, das dämpft ihre Ausschläge. b außerhalb [0, 3] oder keine Streuung → nur Niveau (b = 1, a = Median-Abstand). Der Fit wird in `state.forecast.price_mapping` gespeichert und wiederverwendet, wenn zu wenige Vergleichsstunden da sind (z.B. mittags vor Tibbers Day-Ahead), aber nur für dieselbe Quelle. `apply_mapping` erhält den Risikoaufschlag (`total - raw_total`). Der Forecast-Cache speichert Rohwerte; umgerechnet wird bei jedem Lauf neu.
- **State**: `ForecastCache` in `state.py` is additive — schema version stays at 1, old `state.json` files without a `forecast` block load cleanly with defaults. `forecasts` stores serialized `UnifiedPrice.to_dict()` with `raw_total` for later calibration (PR 4).
- **Config**: optional `FORECAST` block in `config.json` (see `config.example.json`). Missing block → defaults (Open-Meteo, Lübeck 53.87/10.69, 7 d horizon, 10 s timeout). To switch to Corrently set `"provider": "corrently"` + `"zip_code"` + `"api_token"`.

### Calibration logging

`prices/forecast/calibration.py` logs every forecast prediction and matches it against the real Tibber price once that hour gets published. Stored in `state.forecast.calibration_samples`, capped at 500 (FIFO, with preference for already-matched pairs over unmatched predictions). Inspect with:

```bash
python scripts/calibration_report.py
```

Output: MAE and median bias overall, bucketed by forecast horizon (36 h / 48 h / 96 h / 168 h) and by real-price bucket (15-20 ct / 20-25 ct / 25-30 ct / 30-40 ct / >40 ct). If a price bucket shows systematic positive bias (we predict too high), lower the matching `_GSI_ANCHORS` entry in `corrently.py`. No auto-tuning in V1.

**First-write-wins per `target_hour`**: only the earliest forecast for a given hour is kept — that's the hardest test of the heuristic. Re-forecasts of the same hour (a few hours closer) are not added. Forecasts are matched against `state.tibber.yesterday_prices + today_prices + tomorrow_prices` über `mapping.hour_key` (normalisierte Stunde), NICHT über den Text: Tibber schreibt `…T00:00:00.000+02:00`, Prognosen `…T00:00:00+02:00`. Bis 2026-10-19 wurde deshalb kein einziges Sample gematcht (0 von 336). Seit dem Umstieg auf EpexPredictor werden die umgerechneten Endpreise aufgezeichnet; der Report wertet pro `forecast_source` getrennt aus.

**Each sample carries all raw features** for later regression analysis without re-fetching weather data: `wind_kmh`, `solar_wm2`, `temperature_c`, `renewable_index`, `latitude`, `longitude` (from Open-Meteo `meta`) or `gsi`, `zip_code` (from Corrently `meta`), plus `hour_of_day_local`, `weekday`, `month` (derived from `target_hour`). When Tibber publishes the matching hour, `actual_tibber_level` (Tibber's own CHEAP/NORMAL/EXPENSIVE classification) is recorded alongside `actual_total`. Roadmap for the V2 multi-feature model is in `TODO.md`.

### Forecast calendar events (`#Vorhersage`)

PR 3 writes forecast events to the calendar with the tag `#Vorhersage`:

- **Title**: `🔮 Niedriger Strompreis (Schätzung): 18.0ct - 22.0ct [CHEAP] #Vorhersage`. Prefix mirrors Tibber but with "(Schätzung)" suffix and 🔮 instead of ⚡ to make uncertainty visually obvious.
- **Description**: explicit warning, source name (`open_meteo` / `corrently`), generation timestamp, confidence range, and per-hour breakdown showing raw price and risk premium separately.
- **No reminders, ever**. Forecast is too uncertain to wake you up — not even at NEGATIVE.
- **Level via `apply_dynamic_levels(forecast_prices)` before grouping**: quantile-based on the *current forecast distribution*, not on Tibber's absolute thresholds. So Q20 cheapest hours → VERY_CHEAP, Q90+ → VERY_EXPENSIVE.
- **Sync gating**: `#Vorhersage` events are deleted + recreated **only when `forecast_was_fresh=True`** (a refresh just happened). On runs that use cached forecast data, existing events are left alone — prevents calendar flicker every 15 min.
- **`FORECAST.calendar_events_enabled` (Config, default `true`)**: explizit Vorhersage-Events im Kalender ein-/ausschalten. Bei `false` werden Forecasts weiterhin geholt (für die Ladeplanung), aber keine `#Vorhersage`-Events geschrieben — bestehende werden beim nächsten Lauf aufgeräumt. Unterscheidet sich von `FORECAST.enabled=false`, das den Forecast komplett abschaltet (auch für Planung).
- **`force_cleanup` Parameter in `sync_forecast_events`**: wird in `main.py` gesetzt, wenn `FORECAST.enabled=false` ODER `FORECAST.calendar_events_enabled=false`. Erzwingt einen Delete-by-Hashtag-Lauf auch dann, wenn `cache.events_hash is None` (User hat `state.json` gelöscht, kommt von alter Code-Version, oder hatte Forecast nie aktiviert). `CalendarEventsCache.confirmed_empty` wird True, sobald ein erzwungener Cleanup 0 Treffer hatte — verhindert, dass jeder 15-min-Cron-Tick einen unnötigen Calendar-List-Call macht, solange Forecast aus bleibt.
- **Delete range** spans `[now, now + horizon_days]`, not just current forecast data range. Cleans up stale `#Vorhersage` events that may now be inside Tibber's domain (e.g., yesterday's day-3 forecast is now today's day-2 with real Tibber data).
- Substring-safe: `#Vorhersage` and `#Tibber` don't overlap, so `delete_events_matching` is sound.

## Charge planning (`LinearStrategy`)

For each `#charge` target the script computes a `ChargePlan` and persists it to `state.plans.by_target` (keyed by source event ID). Phase 5 only **plans + logs + persists** — no calendar writes. Phase 6 will consume `state.plans` for `#charging` event sync.

**LinearStrategy** (`planning/linear.py`) — V1:
1. `kwh_needed = (target_soc - current_soc) / 100 * battery_capacity_kwh * buffer_factor`. Default buffer +10 % covers BMS throttling above ~80 % and other modeling gaps.
2. **Slot-Expansion**: Input-Preise (typischerweise stündlich von Tibber/Open-Meteo) werden auf `slot_minutes`-Granularität (Default 15 min) expandiert. Pro Stunde 4 Sub-Slots gleichen Preises — der Allokator kann damit auf 15-min-Ebene zuschneiden statt ganze Stunden zu blockieren. Native Granularität wird aus dem Spacing zwischen den ersten zwei Preisen detektiert; bei `native <= slot_minutes` keine Expansion.
3. `slots_needed = ceil(kwh_needed / (net_kw * slot_hours))` mit `slot_hours = slot_minutes / 60`.
4. Greedy: sort all available sub-slots between `now` (hour-floor) und `target.deadline` by `total` (penalty already baked in for forecast prices), pick cheapest N.
5. **kWh-Allokation cheapest-first**: Da `ceil()` typischerweise einen Sub-Slot zu viel reserviert, wird die kWh-Menge in Preis-Reihenfolge verteilt: günstigste Sub-Slots voll, der teuerste der gewählten Sub-Slots bekommt nur den Rest. Gesamt-kWh über alle Blöcke == `kwh_needed` (statt naiv `slots × kwh_per_slot`).
6. Sort chosen sub-slots chronologically, merge adjacent ones (15-min Distanz) into `ChargeBlock`s. Block-Endzeit ist `last_subslot_start + slot_minutes`.
7. `cycles = len(blocks)`. If > `max_cycles_warning` (default 3) → warning in `plan.warnings`.
8. `plan_hash`: MD5 of `target_event_id | target_soc | deadline | blocks(start,end,price)`. Stable across runs when inputs don't change — Phase 6 uses this to skip calendar writes when the plan is unchanged.

**Block-bonus / contiguity preference** is deliberately not implemented in V1. The cycle-counter warning covers the symptom; algorithmic clustering can come later if needed.

**Forecast warning**: if >50 % of planned kWh comes from forecast (non-Tibber) slots, a warning is added to the plan — user knows the cost estimate is speculative.

**Vehicle SoC required**: if `vehicle_status.soc is None` (provider down + no cache), planning is skipped. Plans don't fall back to assumptions.

Config:
- `VEHICLE.battery_capacity_kwh` — vehicle-specific (default 60 for Model 3 RWD).
- `WALLBOX.net_kw` — what actually lands in the battery after charging losses (default 10).
- `CHARGING.buffer_factor` / `slot_minutes` / `max_cycles_warning` — planner tuning.

## Charging events sync (`#charging` + hash diffing)

Phase 6 turns `ChargePlan`s into calendar artifacts:

1. **`#charging` events** — one per `ChargeBlock`. Title: `🔋 Lade {vehicle_name} (⇒ {target_soc}%) #charging`. Description: `DD.MM.YYYY HH:MM bis HH:MM: X.X kW auf X% (Y.YY €)`. Forecast-sourced blocks add a "Schätzung"-warning in description.
2. **`#charge` target description** patched (not the title — the title is the user's input). Description gets the full plan summary: per-block lines, total cost, cycle count, warnings.

**Hash diffing** (`gcal/charging_sync.sync_charge_plans`):
- For each plan, compare `plan.plan_hash` against `state.plans.by_target[id].plan_hash`.
- **Equal hash → zero Calendar API calls.** Old `child_event_ids` carry over. This is the acceptance criterion of Phase 6.
- **Different hash** → delete old children by ID, create new children, patch target description, store new IDs.
- **Orphan cleanup** — targets in `state.plans` but no longer in current plans (e.g., user deleted the `#charge` event) → delete their old children.
- **Discard path** — if no plans at all this run (no targets, or no SoC), call `discard_charging_events_for_targets` to clean up all known children.

**Event IDs are now tracked** in `state.plans.by_target[id].child_event_ids`. `create_event()` returns the Google Calendar event ID; `delete_event_by_id()` deletes by exact ID (404 = no-op success); `update_event_description()` patches only the description without touching title/time/reminders.

**Orphan-Cleanup (vor Sync):** `cleanup_untracked_charging_events` scannt einmal pro Lauf alle `#charging`-Events im 14-Tage-Fenster und löscht alle, deren ID nicht in `state.plans.by_target` als `child_event_ids` getrackt ist. Schützt gegen Duplikate, wenn `state.json` verloren ging (manuelles `rm`, Schema-Bump, Crash) oder ein vorheriger Delete fehlschlug. Kostet einen List-Call pro Lauf — vernachlässigbar gegen den Schutz-Wert.

## Tibber + Vorhersage events sync (Hash-Diffing + Hashtag-Delete)

`#Tibber` und `#Vorhersage` haben jeweils einen `state.tibber_events` bzw. `state.forecast_events` `CalendarEventsCache` mit `events_hash` + `event_ids`. Bei jedem Lauf:

1. Hash über die aktuellen Tibber-Dicts (`startsAt | total | level`) bzw. Forecast-UnifiedPrices (nach `apply_dynamic_levels`).
2. Wenn `cache.events_hash == new_hash` → **0 Calendar-API-Calls.**
3. Sonst: `delete_events_matching(now-1d, now+3d, "#Tibber")` bzw. `delete_events_matching(now, now+horizon, "#Vorhersage")` — löscht ALLE Events mit dem Hashtag im Fenster. Danach frisch anlegen, neue IDs + neuen Hash speichern.

**Warum Hashtag-Delete statt per-ID-Delete** (Refactor 2026-05-29): Per-ID-Delete + separates `cleanup_untracked_events` brachte Duplikate nicht zuverlässig weg — Failed-Delete in einem Zyklus + Hash-Wechsel im nächsten Zyklus → die Orphans blieben hängen (besonders wenn ihr `end_time < now`, dann findet `list_events_matching` mit `timeMin=now` sie nicht). `delete_events_matching` mit weitem Fenster räumt alles auf, was den Hashtag hat — gleichzeitig Sync und Garbage-Collection. Kosten: 1 List-Call + N-Deletes pro Hash-Wechsel (statt nur N per-ID-Deletes), aber Hash-Wechsel passiert nur ~2-3×/Tag.

**Trade-off:** zwischen zwei Hash-Wechseln können externe Duplikate (manuell erstellte, durch andere Tools) ~12 h überleben. Akzeptabel — wir kontrollieren die `#Tibber`/`#Vorhersage`-Tags exklusiv.

**Pagination** (Bug 2026-05-25): `delete_events_matching` und `list_events_matching` paginieren via `nextPageToken` (default 250 Events/Page). Ohne wären Events jenseits Page 1 unsichtbar → kumulierte Duplikate über Wochen.

**Per-Event try/except in `delete_events_matching`** (Bug 2026-05-29): vorher war das `except HttpError` *außerhalb* der Delete-Schleife. Ein einzelner failed Delete (500, Quota, transient) brach den ganzen Loop ab → alle nachfolgenden Duplikate blieben für immer im Kalender. Jetzt pro Event try/except, 404 zählt als Erfolg (idempotent).

**Forecast-Cleanup auch bei leerem `forecast_only`** (Bug 2026-05-29): main.py rief `sync_forecast_events` nur auf, wenn `forecast_only` non-empty war. Konsequenz: wenn Tibber den gesamten Horizont abdeckte ODER der Forecast-Provider länger ausfiel, blieben alte `#Vorhersage`-Events ewig im Kalender. Jetzt wird `sync_forecast_events` immer aufgerufen; bei leerem Input + vorhandenem Cache räumt die Funktion alle alten Events auf und setzt den Cache zurück.

**Server-TZ vs Tibber-TZ** (Bug 2026-05-29): Wenn der Cron auf einem UTC-Server läuft (Default bei Linux ohne `TZ`, Docker), liefert `datetime.now().astimezone()` UTC. Tibber-Preise sind CEST/CET (+02 / +01). Der `now`-Wert wird für Day-Iso (`_today_iso(now)`) und Delete-Fenster (`now - 1d`) benutzt — beide Berechnungen geraten dann in der Nacht-Lücke (00:00 CEST = 22:00 UTC vortags) durcheinander. Symptom: Events von gestern-Nacht (CEST) bleiben übrig, weil ihr `end` in UTC vor `now - 1d UTC` liegt. **Workaround im Code**: Delete-Fenster auf `[now - 2d, now + 4d]` für `#Tibber` und `[now - 1d, now + horizon]` für `#Vorhersage` aufgeweitet. **Empfehlung für Cron**: `TZ=Europe/Berlin python tibber-to-google-calendar.py` setzen, dann fallen alle Day-Boundaries mit Tibbers Tagesgrenzen zusammen.

**Day-Rollover für #Tibber/#Vorhersage** (Bug 2026-05-29): `TibberCache` hat zusätzlich `yesterday_date` + `yesterday_prices`. Bei Tagewechsel rotiert `_store_prices` den alten `today_*` nach `yesterday_*` bevor er überschrieben wird. `_merge` gibt yesterday + today + tomorrow zurück. Damit überleben Preisperioden, die über Mitternacht spannen (CHEAP 22:00 day-1 bis 03:00 day), den Tagewechsel — Hash bleibt stabil, Calendar-Event behält seine ID und visuelle Spannung. Yesterday wird automatisch geleert wenn cache.today_date >1 Tag alt war (Skript war pausiert) oder wenn cache.yesterday_date sich auf vorgestern oder älter bezieht. Stale `tomorrow_*` beim Tagewechsel wird auch geleert, bis frische Daten nach 13:00 nachkommen.

Vorher (Phase 1): jeder Cron-Tick löschte und erstellte alle `#Tibber`-Events neu (bei 96 Läufen/Tag ~800-1600 Calendar-API-Calls/Tag für identische Daten). Jetzt nur noch bei tatsächlich geänderten Tibber-Preisen — meist 2× pro Tag (heute-Refresh + tomorrow-Refresh um ~13:00).

`_build_tibber_event` und `_build_forecast_event` leben jetzt in `price_events_sync.py` (waren vorher in `main.py`). Beim Refactor: keine inhaltliche Änderung an Title/Description außer der bereits in diesem Run gemachten Cleanup-Korrektur (kWh statt irreführendes Wallbox-kW im Block-Eintrag).

**`#charge` event description gets overwritten on every plan change**. The user is warned in the README not to put anything important there.

## Hardware control (Home Assistant)

Phase 7 schaltet die Wallbox über HA. Default ist `HOMEASSISTANT.enabled=false` — explizites Opt-in nötig.

- **`_drive_charging` in `main.py`** wird nach `sync_charge_plans` aufgerufen. Iteriert alle Plan-Blöcke; wenn `block.start <= now < block.end` für mindestens einen Block, soll geladen werden (ON), sonst OFF.
- **Idempotenz**: `state.hardware.last_charging_state` trackt den letzten gesetzten Wert. HA wird nur kontaktiert wenn sich der gewünschte Zustand ändert — Cron alle 15 min ergibt also typischerweise 0-2 Schalt-Calls pro Tag, nicht 96.
- **Schalt-Endpoint**: `POST /api/services/input_boolean/turn_on|turn_off` mit Body `{"entity_id": "..."}`, `Authorization: Bearer <token>`.
- **Fehler-Verhalten**: HA-Fehler werden geloggt aber crashen nichts. Bei Fehler bleibt `state.hardware.last_charging_state` unverändert → nächster Lauf versucht es erneut.

**Software-Heartbeat (optional, empfohlen)**: wenn `HOMEASSISTANT.heartbeat_entity_id` gesetzt ist, schreibt jeder Lauf den aktuellen Timestamp in eine `input_datetime`-Entity. Eine User-Automation in HA kann dann z.B. die Wallbox abschalten wenn der Heartbeat älter als 20 min ist. Pragmatischer Ersatz für den fehlenden KEBA-Hardware-Watchdog (siehe TODO.md).

Endpoint dafür: `POST /api/services/input_datetime/set_datetime` mit Body `{"entity_id": "...", "datetime": "<ISO>"}`.

**Ladesteuerung-Schalter (nur HA-seitig):** `input_boolean.tibber_charging_control` gehört dem User, das Skript kennt ihn nicht. Die HA-Automation „Tibber: Wallbox-Freigabe" (README Schritt 5) fasst die KEBA nur an, wenn er `on` ist; beim Umschalten auf `off` gibt sie die Wallbox frei („Sofort laden"). Grund: Bei aktiver Steuerung sperrt `_drive_charging` die Wallbox immer, wenn kein Ladeblock anliegt, ohne `#charge`-Termin würde das Auto also nie laden. Notaus-Template nutzt `state_attr(..., 'timestamp')`, weil `as_datetime(state)` naiv ist und mit dem aware `now()` nicht subtrahierbar.

**Steckerstatus aus der Wallbox:** optional `HOMEASSISTANT.plug_entity_id` (z.B. `binary_sensor.keba_p30_plug`). `_apply_plug_sensor` in `main.py` ersetzt damit `VehicleStatus.is_connected` direkt nach `fetch_status_with_cache` (der Cache in `state.vehicle` behält den Provider-Wert). Tibber meldete den Stecker im Test rund 2 h verzögert. Bei Lesefehler oder Wert ≠ `on`/`off` bleibt der Provider-Wert. Hinweis: `is_connected` beeinflusst Planung und Schalten aktuell nicht, nur Log/State; Grundlage für eine spätere „Kabel vergessen"-Warnung.

**Referenz-Setup:** HA als Docker-Container (ohne Supervisor) auf einem Linux-Server im Heimnetz, auf dem auch das Skript per Cron läuft; KEBA P30 c-series über die Legacy-Integration `keba` (Entities `*.keba_p30_*`). Helfer und Automationen lassen sich per HA-API anlegen (Websocket `input_boolean/create` etc., REST `POST /api/config/automation/config/<id>`, IDs `tibber_charging_wallbox`, `tibber_charging_notaus`, `tibber_charging_push`; Push über `notify.mobile_app_<gerät>`, Trigger `binary_sensor.keba_p30_charging_state` mit 1 min Entprellung). Konkrete Hostnamen/IPs/Geräte gehören NICHT in dieses öffentliche Repo. Skript nie parallel auf zwei Rechnern laufen lassen (Tibber-Refresh-Token-Rotation). macOS: Cron darf ohne Festplattenvollzugriff nicht auf `~/Documents` zugreifen (`PermissionError` beim venv-Start), deshalb Betrieb auf Linux.

**Erfahrung aus dem Schalttest 2026-10-19:** `keba.disable` zeigt die KEBA als „authorization rejected“. Ein anschließendes `keba.enable` startet beim eingesteckten Tesla sofort einen Ladevorgang, auch wenn er vorher nur auf „ready for charging“ stand. Ein Ladeplan im Auto oder Tibbers Smart Charging kollidiert mit dem Skript und muss aus sein.

## Tibber Data API

Zweite Tibber-API neben der Preis-GraphQL-API: `data-api.tibber.com`, OAuth2 statt statischem Key. Liefert u.a. Daten von Fahrzeugen, die im Tibber-Konto verbunden sind: Capabilities `storage.stateOfCharge`, `storage.targetStateOfCharge`, `connector.status`, `charging.status` (Werte `connected|disconnected|unknown` bzw. `charging|idle|unknown`), `range.remaining` (Meter). Live verifiziert 2026-10-19 mit Tesla Model 3. Kostenloser Ersatz für Tessie, genutzt vom Vehicle-Provider `tibber` (siehe unten).

- **Config**: optionaler Block `TIBBER_DATA_API` (`client_id`, `client_secret`, `redirect_uri`) → `Config.tibber_data_api` oder `None`.
- **Einrichtung**: `scripts/tibber_data_auth.py` macht den Authorization Code Flow mit PKCE (lokaler Empfänger auf der Redirect-Adresse, Fallback: Callback-URL einfügen) und listet danach alle Geräte mit Details. Gibt nie Tokens aus.
- **Tokens**: `tibber_data_token.json` (gitignored, 0600, atomar geschrieben). Bewusst NICHT in `state.json`, weil `state.json` jederzeit löschbar sein soll. Access-Token ~1 h, Refresh-Token ~30 Tage und evtl. single-use, deshalb wird jedes neue Refresh-Token sofort persistiert. `TibberDataClient.get()` erneuert 120 s vor Ablauf und bei einem 401 einmal mit Retry. Abgelehnter Refresh → `TibberDataAuthError` (Hinweis auf erneute Einrichtung).
- **Endpunkte**: `GET /v1/homes` → `{"homes": [{id, name}]}`, `GET /v1/homes/{homeId}/devices` → `{"devices": [{id, externalId, info, supportedHistory}]}` (OHNE Capabilities), `GET /v1/homes/{homeId}/devices/{deviceId}` → Details inkl. `capabilities`, `attributes`, `status.lastSeen`. Fahrzeuge erscheinen unter jedem Home. Die Geräte-`id` ist base64 von `"<marke> vehicle:<uuid>"` (z.B. `tesla vehicle:…`, `offline vehicle:…` für manuell angelegte Autos); daran erkennt `vehicle/tibber._is_vehicle` Fahrzeuge in der Liste.
- **Headless**: `--headless` gibt nur den Link aus und erwartet die Rückleitungs-Adresse per Einfügen. Alternative: SSH-Tunnel auf den Redirect-Port. Token-Datei NICHT auf zwei Rechnern parallel nutzen (Refresh-Token-Rotation sperrt den jeweils anderen aus).

## Vehicle telemetry layer

Real-time SoC and cable-connection status feed into the planner (Phase 5+). Three providers:

- **`TibberVehicleProvider`** (empfohlen, `VEHICLE.provider = "tibber"`): liest das im Tibber-Konto verbundene Fahrzeug über die Tibber Data API. 3 GET-Calls pro Lauf (Homes, Geräteliste, Details), kein Wake, kein Abo. Auswahl über `VEHICLE.tibber_vehicle_id` = `externalId` (bei Tesla Teslas interne Fahrzeug-ID, NICHT die VIN) oder Tibber-`id`; Pflicht, sobald mehrere Fahrzeuge im Konto sind (Fehlermeldung listet die Kandidaten). `connector.status == "unknown"` zählt als nicht eingesteckt, `charging.status == "charging"` impliziert eingesteckt. `last_updated_at` = `status.lastSeen` (Tibbers letzter Kontakt zum Auto, kann Stunden alt sein). Braucht `base_dir` für die Token-Datei, daher `fetch_status_with_cache(state, config, now, base_dir)`. `TibberDataAuthError` ist ein `RuntimeError` und läuft damit in den normalen Backoff.

- **`ManualVehicleProvider`** (default): reads `manual_soc`, `manual_connected`, `manual_charging` from `config.json` `VEHICLE` block. Useful for setup without a third-party account, for tests, and for dry-run planning with a hypothetical SoC.
- **`TessieVehicleProvider`** (opt-in): calls `GET https://api.tessie.com/{vin}/state?use_cache=true` with `Authorization: Bearer <token>`. **Always uses `use_cache=true`** — Tessie's internal cache is read without waking the car, which is the Vampire-Drain guard for V1. We never call `/wake` or non-cached endpoints. SoC is read from `charge_state.battery_level`, connection from `charge_state.conn_charge_cable` + `charging_state`.
- Parsing is defensive: empty/partial payloads raise `RuntimeError` with a hint about VIN/token validity.

`fetch_status_with_cache(state, config, now)` in `vehicle/cache.py` wraps both:
- Each run hits the provider (Tessie's cached endpoint is cheap; Manual is local).
- On `RequestException` or parse error: exponential backoff 5 min → 10 → 20 → 40 → 60 (capped at 1 h). Last known status is returned from `state.vehicle.last_status` until the next success.
- `last_status` is persisted as a dict in `state.json` and survives crashes.
- Provider switch and re-config trigger a fresh fetch on next run (no migration needed — additive schema).

Phase 4 currently only **logs** the status (`🚗 Auto: SoC=N%, connected=…, charging=…`). The planner in Phase 5 will consume `VehicleStatus`. The vampire-drain rule from the original concept (only query when HA reports connected) is replaced here by always-cached Tessie polling — no need for an HA dependency in V1.

## Charge target events (`#charge`)

Input events the user creates to specify charge deadlines. Read but never modified by Phase 3; Phase 6 will overwrite their `description` with the computed plan.

**Title spec** (parsed by `gcal/charge_targets.parse_title`):
- Must contain a `#charge` hashtag, case-insensitive, **exact-word** match (so `#charging` does **not** match — important, since that's our own output tag).
- Must contain a percent value: 1–3 digits + optional whitespace + `%`. Optional brackets allowed: `[80]%`, `[ 80 ] %`. Examples that all parse to 80: `80% #charge`, `80 % #charge`, `80%#charge`, `[80]% #charge`, `Drive to work 80% #charge`, `#charge 80%`.
- Optional second hashtag → `car_id` (lowercased). `80% #charge #model3` → `car_id="model3"`.
- Multiple percent values or multiple non-`#charge` hashtags → first one wins, warning recorded in `ChargeTarget.parse_warnings`.
- SoC outside 1–100 → target is rejected (logged, skipped).

**Deadline = `event.start`** (user decision 2026-05-20). The car must reach `target_soc` *by* event start. `event.end` is ignored. All-day events have no `dateTime`, so they're skipped with a warning.

**Conflict detection** (`detect_conflicts`): V1 is single-car, so any two targets with identical deadlines are flagged. Only equal-deadline conflicts here — real charge-window overlaps emerge after Phase 5 planning and are handled separately.

**Lookahead window**: `main.CHARGE_TARGET_LOOKAHEAD` = 7 days. Targets beyond Tibber's day-ahead horizon (24–48 h) are visible but not yet plannable.

## Conventions

- **Logs and code comments are in German.** Keep them in German when editing existing code; don't translate them.
- Default log level is `WARNING` (in `main.DEFAULT_LOG_LEVEL`) to keep cron silent. Use `--debug` / `--log-level` instead of editing the constant.
- Secrets (`config.json`, `service_account.json`, `your-client-secret.json`, `token.json`) are gitignored. `your-client-secret.json` and `token.json` are remnants from the older OAuth flow — current auth is Service Account only.
