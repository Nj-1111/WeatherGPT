"""§2.1 IDOR fix: static API key gate + per-key namespacing of stored user/session state.

Auth is off by default (WEATHERGPT_API_KEYS unset) so the rest of the suite is unaffected;
each test here turns it on for itself via `_with_keys`.
"""
import asyncio
import dataclasses
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.main import app
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
from app.schemas.location import ResolvedLocation
from app.services.auth import verify_api_key

START = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)


def _evidence():
    return [CanonicalEvidenceObject(
        source="OPEN_METEO", evidence_class="forecast", variable="precipitation_amount", value=1.2,
        unit="mm", statistic="accumulation", geometry=Geometry(type="GridCell", coordinates=[75.86, 22.72]),
        issued_at=START, valid_from=START, valid_to=START, accumulation_window_hours=1,
        provenance=Provenance(original_source="OPEN_METEO"))]


async def _fake_retrieve(*args, **kwargs):
    return _evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}


async def _fake_resolve_nagpur(phrase):
    return ResolvedLocation(raw=phrase, lat=21.1458, lon=79.0882, timezone="Asia/Kolkata",
                            normalized_name="Nagpur", state="Maharashtra", country="India")


async def _request(method, path, *, headers=None, json=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


def _with_keys(monkeypatch, keys):
    """Both app.main and app.services.auth bound `settings` at import time, so both must
    be repointed at the same replaced instance."""
    patched = dataclasses.replace(settings, api_keys=keys)
    monkeypatch.setattr("app.main.settings", patched)
    monkeypatch.setattr("app.services.auth.settings", patched)


def test_missing_key_returns_401(monkeypatch):
    _with_keys(monkeypatch, ("key-a",))
    response = asyncio.run(_request("POST", "/query", json={"question": "weather in Nagpur"}))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_wrong_key_returns_401(monkeypatch):
    _with_keys(monkeypatch, ("key-a",))
    response = asyncio.run(_request("POST", "/query", headers={"Authorization": "Bearer wrong"},
                                     json={"question": "weather in Nagpur"}))
    assert response.status_code == 401


def test_malformed_header_returns_401(monkeypatch):
    _with_keys(monkeypatch, ("key-a",))
    response = asyncio.run(_request("POST", "/query", headers={"Authorization": "key-a"},
                                     json={"question": "weather in Nagpur"}))
    assert response.status_code == 401


def test_health_and_root_exempt_from_key_gate(monkeypatch):
    _with_keys(monkeypatch, ("key-a",))
    assert asyncio.run(_request("GET", "/health")).status_code == 200
    assert asyncio.run(_request("GET", "/")).status_code == 200


def test_valid_key_unchanged_response_shape(monkeypatch):
    _with_keys(monkeypatch, ("key-a",))
    monkeypatch.setattr("app.main.retrieve", _fake_retrieve)
    monkeypatch.setattr("app.services.location_resolver.resolve_location", _fake_resolve_nagpur)
    response = asyncio.run(_request("POST", "/wio/query", headers={"Authorization": "Bearer key-a"},
                                     json={"question": "Will it rain in Nagpur tomorrow?"}))
    assert response.status_code == 200
    assert "wio" in response.json()


def test_gate_off_by_default(monkeypatch):
    """No WEATHERGPT_API_KEYS configured (the test-suite default) — unauthenticated
    requests must keep working exactly as before this change."""
    monkeypatch.setattr("app.main.retrieve", _fake_retrieve)
    monkeypatch.setattr("app.services.location_resolver.resolve_location", _fake_resolve_nagpur)
    response = asyncio.run(_request("POST", "/wio/query", json={"question": "Will it rain in Nagpur tomorrow?"}))
    assert response.status_code == 200


def test_cross_key_context_isolation(monkeypatch):
    """The actual §2.1 regression test: the same client-supplied user_id, presented under
    two different valid keys, must not see each other's stored context facts."""
    _with_keys(monkeypatch, ("key-a", "key-b"))
    monkeypatch.setattr("app.main.retrieve", _fake_retrieve)
    monkeypatch.setattr("app.services.location_resolver.resolve_location", _fake_resolve_nagpur)
    shared_user_id = "shared-user-42"

    write = asyncio.run(_request("POST", "/context", headers={"Authorization": "Bearer key-a"},
                                  json={"user_id": shared_user_id,
                                        "fact": {"fact": "risk_tolerance", "value": "high"}}))
    assert write.status_code == 200

    def context_claims(agents):
        return next(a for a in agents if a["agent_name"] == "context")["claims"]

    same_key = asyncio.run(_request("POST", "/wio/query", headers={"Authorization": "Bearer key-a"},
                                     json={"question": "Will it rain in Nagpur tomorrow?",
                                           "user_id": shared_user_id}))
    assert any(c["claim"] == "context.risk_tolerance" for c in context_claims(same_key.json()["agents"]))

    other_key = asyncio.run(_request("POST", "/wio/query", headers={"Authorization": "Bearer key-b"},
                                      json={"question": "Will it rain in Nagpur tomorrow?",
                                            "user_id": shared_user_id}))
    assert not any(c["claim"] == "context.risk_tolerance" for c in context_claims(other_key.json()["agents"]))


def test_non_ascii_bearer_token_returns_none_instead_of_raising(monkeypatch):
    """hmac.compare_digest raises TypeError on non-ASCII str. verify_api_key runs inside
    RequestIDMiddleware, which sits outside ExceptionMiddleware, so the raise bypassed the
    JSON error envelope entirely and produced a bare 500 — pre-auth, and only where
    WEATHERGPT_API_KEYS is actually set (production, never dev). Tested at this level
    because httpx refuses to encode a non-ASCII header, while a raw client sends it fine."""
    _with_keys(monkeypatch, ("key-a",))
    assert verify_api_key("Bearer café") is None
    assert verify_api_key("Bearer ключ") is None
    assert verify_api_key("Bearer key-a") == "key-a"
