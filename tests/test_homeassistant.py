"""Tests für HomeAssistantClient mit responses-Mock."""
from __future__ import annotations

from datetime import datetime, timezone

import requests
import responses

from tibber_charge.hardware.homeassistant import HomeAssistantClient


BASE = "http://homeassistant.local:8123"


def _client(token: str = "secret") -> HomeAssistantClient:
    return HomeAssistantClient(base_url=BASE, api_token=token)


@responses.activate
def test_set_input_boolean_on():
    responses.add(responses.POST, f"{BASE}/api/services/input_boolean/turn_on",
                  json={}, status=200)
    assert _client().set_input_boolean("input_boolean.foo", True) is True


@responses.activate
def test_set_input_boolean_off():
    responses.add(responses.POST, f"{BASE}/api/services/input_boolean/turn_off",
                  json={}, status=200)
    assert _client().set_input_boolean("input_boolean.foo", False) is True


@responses.activate
def test_set_input_boolean_authheader_und_entity_id():
    captured: dict = {}

    def callback(request):
        captured["auth"] = request.headers.get("Authorization", "")
        captured["body"] = request.body
        return (200, {}, "{}")

    responses.add_callback(
        responses.POST, f"{BASE}/api/services/input_boolean/turn_on", callback=callback
    )
    _client(token="t123").set_input_boolean("input_boolean.foo", True)
    assert captured["auth"] == "Bearer t123"
    assert b'"entity_id": "input_boolean.foo"' in captured["body"]


@responses.activate
def test_set_input_boolean_http_500_returnt_False():
    responses.add(responses.POST, f"{BASE}/api/services/input_boolean/turn_on", status=500)
    assert _client().set_input_boolean("input_boolean.foo", True) is False


@responses.activate
def test_set_input_boolean_netzfehler_returnt_False():
    responses.add(
        responses.POST, f"{BASE}/api/services/input_boolean/turn_on",
        body=requests.ConnectionError("boom"),
    )
    assert _client().set_input_boolean("input_boolean.foo", True) is False


@responses.activate
def test_write_input_datetime_serialisiert_iso():
    captured: dict = {}

    def callback(request):
        captured["body"] = request.body
        return (200, {}, "{}")

    responses.add_callback(
        responses.POST, f"{BASE}/api/services/input_datetime/set_datetime", callback=callback
    )
    ts = datetime(2026, 5, 21, 22, 30, tzinfo=timezone.utc)
    assert _client().write_input_datetime("input_datetime.heartbeat", ts) is True
    assert b'"datetime": "2026-05-21T22:30:00+00:00"' in captured["body"]


@responses.activate
def test_write_input_datetime_http_404_returnt_False():
    responses.add(
        responses.POST, f"{BASE}/api/services/input_datetime/set_datetime", status=404
    )
    ts = datetime(2026, 5, 21, 22, 30, tzinfo=timezone.utc)
    assert _client().write_input_datetime("input_datetime.heartbeat", ts) is False


def test_base_url_trailing_slash_wird_entfernt():
    c = HomeAssistantClient(base_url=f"{BASE}/", api_token="x")
    assert c.base_url == BASE
