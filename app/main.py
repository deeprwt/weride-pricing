"""WeRide pricing service entrypoint.

Phase 0: ships only `/healthz`. All pricing endpoints return 501 with a clear
message pointing to the phase they land in. The Nest core API talks to this
service over REST behind an opossum circuit breaker — so when 501s are returned,
the breaker's fallback kicks in cleanly.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.fares import LatLng, QuoteRequest, QuoteResponse, current_surge, quote_fare


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper())
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)
    log = structlog.get_logger()
    log.info("pricing.startup", env=settings.env, port=settings.port)
    yield
    log.info("pricing.shutdown")


app = FastAPI(
    title="WeRide Pricing",
    version="0.0.0",
    description="Fare quotes, surge, ETA-ML. Phase 0 stub.",
    lifespan=lifespan,
)


@app.get("/healthz", tags=["health"])
async def healthz() -> JSONResponse:
    """Liveness — always returns 200 unless the process is dying."""

    return JSONResponse(
        {
            "status": "ok",
            "service": "uride-pricing",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/readyz", tags=["health"])
async def readyz() -> JSONResponse:
    """Readiness — Phase 0 has no downstream deps to check."""

    return JSONResponse({"status": "ok"})


@app.post("/v1/quote", tags=["pricing"], response_model=QuoteResponse)
async def quote(req: QuoteRequest) -> QuoteResponse:
    """Fare estimate for a pickup -> dropoff trip. Pure, deterministic."""
    return quote_fare(req)


@app.get("/v1/surge", tags=["pricing"])
async def surge(lat: float, lng: float) -> JSONResponse:
    """Current surge multiplier at a point. Flat 1.0 until zone/ML surge."""
    return JSONResponse({"surgeMultiplier": current_surge(LatLng(lat=lat, lng=lng))})
