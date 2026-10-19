"""Client für die Tibber Data API (data-api.tibber.com).

Getrennt von der Preis-API (`client.py`): die Data API nutzt OAuth2
(Authorization Code Flow + Refresh-Token) statt eines statischen API-Keys
und liefert Gerätedaten, u.a. den Akkustand von im Tibber-Konto verbundenen
Fahrzeugen.

Token-Lebensdauer laut Doku: Access-Token ~1 h, Refresh-Token ~30 Tage und
ggf. nur einmal verwendbar. Deshalb wird jedes neue Refresh-Token sofort in
die Token-Datei geschrieben.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests

from tibber_charge.config import TibberDataApiConfig


AUTHORIZE_URL = "https://thewall.tibber.com/connect/authorize"
TOKEN_URL = "https://thewall.tibber.com/connect/token"
API_BASE_URL = "https://data-api.tibber.com"
SCOPES = (
    "openid profile email offline_access "
    "data-api-user-read data-api-homes-read data-api-vehicles-read"
)
TOKEN_FILE_NAME = "tibber_data_token.json"

# Access-Token so früh erneuern, dass er während eines Laufs nicht abläuft.
_REFRESH_MARGIN_SECONDS = 120


class TibberDataAuthError(RuntimeError):
    """Kein gültiger Zugang: Einrichtung (scripts/tibber_data_auth.py) nötig."""


def token_path(base_dir: str) -> str:
    return os.path.join(base_dir, TOKEN_FILE_NAME)


def new_pkce_pair() -> tuple[str, str]:
    """Liefert (code_verifier, code_challenge) für PKCE mit S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(cfg: TibberDataApiConfig, state: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    cfg: TibberDataApiConfig,
    code: str,
    code_verifier: str,
    timeout_seconds: float = 15.0,
) -> dict:
    """Tauscht den Anmeldecode gegen Access- und Refresh-Token."""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code_verifier": code_verifier,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return _with_expiry(response.json())


def load_tokens(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"⚠️ Tibber-Data-Token-Datei unlesbar ({e}).")
        return None


def save_tokens(path: str, tokens: dict) -> None:
    """Atomar schreiben, nur für den Besitzer lesbar (0600)."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _with_expiry(token_response: dict) -> dict:
    expires_in = int(token_response.get("expires_in", 3600))
    return {**token_response, "expires_at": int(time.time()) + expires_in}


class TibberDataClient:
    """GET-Zugriff auf die Data API mit automatischer Token-Erneuerung."""

    def __init__(self, cfg: TibberDataApiConfig, path: str, timeout_seconds: float = 15.0):
        self._cfg = cfg
        self._path = path
        self._timeout = timeout_seconds
        self._tokens = load_tokens(path)

    def get(self, api_path: str) -> dict:
        response = self._request(api_path)
        if response.status_code == 401:
            # Token ungültig, obwohl laut expires_at noch gültig: einmal erneuern.
            self._refresh()
            response = self._request(api_path)
        response.raise_for_status()
        return response.json()

    def _request(self, api_path: str) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json",
        }
        return requests.get(f"{API_BASE_URL}{api_path}", headers=headers, timeout=self._timeout)

    def _access_token(self) -> str:
        if not self._tokens or not self._tokens.get("refresh_token"):
            raise TibberDataAuthError(
                "Kein Tibber-Data-Zugang. Einrichtung mit `python scripts/tibber_data_auth.py`."
            )
        if int(self._tokens.get("expires_at", 0)) - _REFRESH_MARGIN_SECONDS <= time.time():
            self._refresh()
        return self._tokens["access_token"]

    def _refresh(self) -> None:
        if not self._tokens or not self._tokens.get("refresh_token"):
            raise TibberDataAuthError(
                "Kein Refresh-Token. Einrichtung mit `python scripts/tibber_data_auth.py`."
            )
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"],
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
            },
            timeout=self._timeout,
        )
        if response.status_code in (400, 401):
            raise TibberDataAuthError(
                f"Refresh-Token abgelehnt (HTTP {response.status_code}). "
                "Neu einrichten mit `python scripts/tibber_data_auth.py`."
            )
        response.raise_for_status()
        new_tokens = _with_expiry(response.json())
        # Manche Server liefern kein neues Refresh-Token mit, dann altes behalten.
        new_tokens.setdefault("refresh_token", self._tokens["refresh_token"])
        self._tokens = new_tokens
        save_tokens(self._path, new_tokens)
        logging.info("🔑 Tibber-Data-Token erneuert.")
