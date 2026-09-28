"""Smoke tests for the Phase 0 pricing service."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_healthz() -> None:
    client = TestClient(app)
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "uride-pricing"


def test_readyz() -> None:
    client = TestClient(app)
    assert client.get("/readyz").status_code == 200


def test_quote_returns_fare() -> None:
    client = TestClient(app)
    res = client.post(
        "/v1/quote",
        json={
            "pickup": {"lat": 43.6532, "lng": -79.3832},   # Toronto City Hall
            "dropoff": {"lat": 43.6426, "lng": -79.3871},  # CN Tower (~1.2 km)
            "rideClass": "standard",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["currency"] == "CAD"
    assert body["distanceMeters"] > 0
    assert body["durationSeconds"] > 0
    assert body["fareCents"] >= 600  # min-fare floor
