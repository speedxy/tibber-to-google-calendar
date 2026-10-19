#!/usr/bin/env python3
"""Einmalige Einrichtung des Tibber-Data-API-Zugangs + Geräte-Übersicht.

Aufruf (aus dem Repo-Verzeichnis, venv aktiv):
    python scripts/tibber_data_auth.py            # anmelden (falls nötig) + Geräte anzeigen
    python scripts/tibber_data_auth.py --reauth   # Anmeldung erzwingen
    python scripts/tibber_data_auth.py --probe    # nur Geräte anzeigen, keine Anmeldung
    python scripts/tibber_data_auth.py --headless # Server ohne Browser: Link auf einem
                                                  # anderen Gerät öffnen, Rückleitungs-
                                                  # Adresse hier einfügen

Voraussetzung: Block `TIBBER_DATA_API` in config.json (client_id, client_secret,
redirect_uri). Das Refresh-Token landet in `tibber_data_token.json` (gitignored,
Rechte 0600). Tokens werden nie ausgegeben.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import requests  # noqa: E402

from tibber_charge.config import load_config  # noqa: E402
from tibber_charge.tibber.data_api import (  # noqa: E402
    TibberDataAuthError,
    TibberDataClient,
    build_authorize_url,
    exchange_code,
    load_tokens,
    new_pkce_pair,
    save_tokens,
    token_path,
)

_CALLBACK_TIMEOUT_SECONDS = 300
_MAX_DETAIL_CHARS = 6000


def main() -> int:
    parser = argparse.ArgumentParser(description="Tibber Data API einrichten und Geräte anzeigen.")
    parser.add_argument("--reauth", action="store_true", help="Anmeldung erzwingen")
    parser.add_argument("--probe", action="store_true", help="nur Geräte anzeigen")
    parser.add_argument(
        "--headless", action="store_true",
        help="kein Browser, kein lokaler Empfänger: Rückleitungs-Adresse manuell einfügen",
    )
    args = parser.parse_args()

    config = load_config(BASE_DIR)
    cfg = config.tibber_data_api
    if cfg is None:
        print("❌ In config.json fehlt der Block TIBBER_DATA_API mit client_id und client_secret.")
        return 1

    path = token_path(BASE_DIR)
    has_token = bool((load_tokens(path) or {}).get("refresh_token"))

    if not args.probe and (args.reauth or not has_token):
        if not _authorize(cfg, path, headless=args.headless):
            return 1
    elif not has_token:
        print("❌ Noch kein Token vorhanden. Ohne --probe aufrufen, um dich anzumelden.")
        return 1

    return _probe(TibberDataClient(cfg, path))


# ---------- Anmeldung ----------

def _authorize(cfg, path: str, headless: bool = False) -> bool:
    verifier, challenge = new_pkce_pair()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(cfg, state, challenge)

    if headless:
        print("🔐 Öffne diese Adresse in einem Browser auf einem beliebigen Gerät:\n")
        print(f"   {url}\n")
        callback_url = None
    else:
        print("🔐 Öffne die Tibber-Anmeldung im Browser.")
        print("   Falls sich kein Browser öffnet, diese Adresse manuell aufrufen:\n")
        print(f"   {url}\n")
        webbrowser.open(url)
        callback_url = _wait_for_callback(cfg.redirect_uri)
    if callback_url is None:
        print("Nach der Anmeldung leitet Tibber auf deine Redirect-Adresse um.")
        print("Kopiere die komplette Adresse aus der Adresszeile des Browsers hierher")
        print("(auch wenn der Browser eine Fehlerseite zeigt):")
        callback_url = input("> ").strip()

    query = parse_qs(urlparse(callback_url).query)
    if "error" in query:
        print(f"❌ Tibber meldet: {query.get('error')} {query.get('error_description', '')}")
        return False
    code = (query.get("code") or [None])[0]
    if not code:
        print("❌ In der Adresse ist kein Anmeldecode (code=...) enthalten.")
        return False
    if (query.get("state") or [None])[0] != state:
        print("❌ state-Parameter passt nicht. Anmeldung bitte neu starten.")
        return False

    try:
        tokens = exchange_code(cfg, code, verifier)
    except requests.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else ""
        print(f"❌ Token-Tausch fehlgeschlagen: {e}\n{body}")
        return False

    if not tokens.get("refresh_token"):
        print("⚠️ Kein Refresh-Token erhalten. Ist der Scope offline_access im Client angehakt?")
    save_tokens(path, tokens)
    print(f"✅ Anmeldung erfolgreich. Token gespeichert in {os.path.basename(path)}.\n")
    return True


def _wait_for_callback(redirect_uri: str) -> str | None:
    """Lokalen Empfänger starten, wenn die Redirect-Adresse auf localhost zeigt.

    Liefert die vollständige Callback-Adresse oder None (dann manuelles Einfügen).
    """
    parsed = urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        return None
    port = parsed.port or 80
    expected_path = parsed.path or "/"
    received: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (Name von BaseHTTPRequestHandler vorgegeben)
            if urlparse(self.path).path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            received["url"] = f"{parsed.scheme}://{parsed.netloc}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<h2>Anmeldung abgeschlossen.</h2><p>Du kannst dieses Fenster schließen.</p>".encode()
            )

        def log_message(self, *args):  # Zugriffslog unterdrücken
            pass

    try:
        server = HTTPServer((parsed.hostname, port), _Handler)
    except OSError as e:
        print(f"⚠️ Lokaler Empfänger auf Port {port} nicht möglich ({e}).")
        return None

    print(f"⏳ Warte auf die Rückleitung von Tibber (max. {_CALLBACK_TIMEOUT_SECONDS // 60} min) ...")
    server.timeout = 1
    deadline = time.time() + _CALLBACK_TIMEOUT_SECONDS
    try:
        while "url" not in received and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return received.get("url")


# ---------- Geräte anzeigen ----------

def _probe(client: TibberDataClient) -> int:
    try:
        homes = _extract_list(client.get("/v1/homes"), ("homes", "items", "data"))
    except TibberDataAuthError as e:
        print(f"❌ {e}")
        return 1
    except requests.HTTPError as e:
        print(f"❌ Abfrage der Homes fehlgeschlagen: {e}")
        return 1

    print(f"🏠 {len(homes)} Home(s) gefunden.")
    seen_devices: set[str] = set()
    vehicle_found = False

    for home in homes:
        home_id = home.get("id")
        if not home_id:
            continue
        try:
            devices = _extract_list(
                client.get(f"/v1/homes/{home_id}/devices"), ("devices", "items", "data")
            )
        except requests.HTTPError as e:
            print(f"⚠️ Geräte für Home {home_id} nicht abrufbar: {e}")
            continue

        for device in devices:
            device_id = device.get("id")
            if not device_id or device_id in seen_devices:
                continue
            seen_devices.add(device_id)
            try:
                details = client.get(f"/v1/homes/{home_id}/devices/{device_id}")
            except requests.HTTPError as e:
                print(f"⚠️ Details für Gerät {device_id} nicht abrufbar: {e}")
                details = device

            text = json.dumps(details, indent=2, ensure_ascii=False)
            if "stateOfCharge" in text:
                vehicle_found = True
            print(f"\n📟 Gerät {device_id}:")
            print(text[:_MAX_DETAIL_CHARS])
            if len(text) > _MAX_DETAIL_CHARS:
                print("   ... (gekürzt)")

    print()
    if not seen_devices:
        print("❌ Keine Geräte gefunden. Ist das Fahrzeug in der Tibber-App verbunden")
        print("   und der Scope data-api-vehicles-read im Client angehakt?")
        return 1
    if vehicle_found:
        print("✅ Akkustand (stateOfCharge) gefunden. Die Tibber Data API ist nutzbar.")
    else:
        print("⚠️ Geräte gefunden, aber kein Feld stateOfCharge. Ausgabe bitte prüfen.")
    return 0


def _extract_list(payload, keys: tuple[str, ...]) -> list[dict]:
    """Die Antwort ist je nach Endpunkt eine Liste oder ein Objekt mit Liste."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
    return []


if __name__ == "__main__":
    sys.exit(main())
