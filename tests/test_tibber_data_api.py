"""Tests für den Tibber-Data-API-Client (OAuth + Token-Erneuerung)."""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import time
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from tibber_charge.config import TibberDataApiConfig
from tibber_charge.tibber.data_api import (
    API_BASE_URL,
    TOKEN_URL,
    TibberDataAuthError,
    TibberDataClient,
    build_authorize_url,
    load_tokens,
    new_pkce_pair,
    save_tokens,
)


def _cfg() -> TibberDataApiConfig:
    return TibberDataApiConfig(
        client_id="cid", client_secret="secret", redirect_uri="http://localhost:8765/callback",
    )


def _write_tokens(tmp_path, **overrides) -> str:
    path = str(tmp_path / "tibber_data_token.json")
    tokens = {
        "access_token": "old_access",
        "refresh_token": "old_refresh",
        "expires_at": int(time.time()) + 3600,
        **overrides,
    }
    save_tokens(path, tokens)
    return path


def test_pkce_challenge_ist_s256_des_verifiers():
    verifier, challenge = new_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_enthaelt_scopes_und_pkce():
    url = build_authorize_url(_cfg(), state="abc", code_challenge="chal")
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["cid"]
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]
    assert query["state"] == ["abc"]
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" in query["scope"][0]
    assert "data-api-vehicles-read" in query["scope"][0]


def test_save_tokens_setzt_dateirechte_0600(tmp_path):
    path = _write_tokens(tmp_path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


@responses.activate
def test_get_mit_gueltigem_token_ohne_refresh(tmp_path):
    path = _write_tokens(tmp_path)
    responses.get(f"{API_BASE_URL}/v1/homes", json={"homes": []})

    result = TibberDataClient(_cfg(), path).get("/v1/homes")

    assert result == {"homes": []}
    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["Authorization"] == "Bearer old_access"


@responses.activate
def test_abgelaufener_token_wird_erneuert_und_gespeichert(tmp_path):
    path = _write_tokens(tmp_path, expires_at=int(time.time()) - 10)
    responses.post(TOKEN_URL, json={
        "access_token": "new_access", "refresh_token": "new_refresh", "expires_in": 3600,
    })
    responses.get(f"{API_BASE_URL}/v1/homes", json={"homes": []})

    TibberDataClient(_cfg(), path).get("/v1/homes")

    assert responses.calls[1].request.headers["Authorization"] == "Bearer new_access"
    stored = load_tokens(path)
    # Refresh-Token kann single-use sein: das neue muss sofort persistiert sein.
    assert stored["refresh_token"] == "new_refresh"
    assert stored["expires_at"] > time.time()


@responses.activate
def test_refresh_ohne_neues_refresh_token_behaelt_altes(tmp_path):
    path = _write_tokens(tmp_path, expires_at=0)
    responses.post(TOKEN_URL, json={"access_token": "new_access", "expires_in": 3600})
    responses.get(f"{API_BASE_URL}/v1/homes", json={})

    TibberDataClient(_cfg(), path).get("/v1/homes")

    assert load_tokens(path)["refresh_token"] == "old_refresh"


@responses.activate
def test_401_fuehrt_zu_einem_refresh_und_retry(tmp_path):
    path = _write_tokens(tmp_path)
    responses.get(f"{API_BASE_URL}/v1/homes", status=401)
    responses.post(TOKEN_URL, json={
        "access_token": "new_access", "refresh_token": "new_refresh", "expires_in": 3600,
    })
    responses.get(f"{API_BASE_URL}/v1/homes", json={"homes": [{"id": "h1"}]})

    result = TibberDataClient(_cfg(), path).get("/v1/homes")

    assert result == {"homes": [{"id": "h1"}]}


@responses.activate
def test_abgelehnter_refresh_wirft_auth_error(tmp_path):
    path = _write_tokens(tmp_path, expires_at=0)
    responses.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})

    with pytest.raises(TibberDataAuthError):
        TibberDataClient(_cfg(), path).get("/v1/homes")


def test_ohne_token_datei_auth_error(tmp_path):
    client = TibberDataClient(_cfg(), str(tmp_path / "fehlt.json"))
    with pytest.raises(TibberDataAuthError):
        client.get("/v1/homes")


def test_token_datei_kaputt_wird_wie_fehlend_behandelt(tmp_path):
    path = tmp_path / "tibber_data_token.json"
    path.write_text("{kaputt")
    assert load_tokens(str(path)) is None
