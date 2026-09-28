"""Fare quoting — deterministic, dependency-free.

Computes a fare from pickup/dropoff plus three optional inputs the core API
measures and this service cannot: the zone's surge multiplier, and the road
distance and duration of the trip. Base + per-km + per-min, times a per-class
multiplier and the surge factor.

Why the inputs arrive on the request instead of being looked up here: surge is
demand over supply per H3 zone, and supply is the live driver index held in the
core API's Redis; road distance comes from a billable maps provider the core API
already caches per quote. Re-deriving either here would mean a second copy of
the driver index and a second Google bill for the same trip. This module stays
a pure function of its request, which is what makes it testable and what lets
the core API's local fallback reproduce it to the cent.

When an input is absent the old behaviour stands — straight-line (haversine)
distance, a flat average speed, no surge — so existing callers and clients keep
getting the same quote they always did.
"""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# --- Tariff (CAD cents) -------------------------------------------------------

CURRENCY = "CAD"
BASE_FARE_CENTS = 250          # flag-drop
PER_KM_CENTS = 120             # $1.20 / km
PER_MIN_CENTS = 30             # $0.30 / min
MIN_FARE_CENTS = 600           # never quote below $6.00
BOOKING_FEE_CENTS = 99         # flat platform fee

# Estimated average urban speed (km/h) used to turn distance into a time estimate
# when the caller did not measure a duration.
AVG_SPEED_KMH = 28.0

CLASS_MULTIPLIERS: dict[str, float] = {
    "standard": 1.0,
    "xl": 1.5,
    "premium": 2.0,
}

# Surge bounds this service will apply no matter what the caller sends. The core
# API caps lower (SURGE_MAX_MULTIPLIER, 2.5 by default); this is the backstop
# for a misconfigured or compromised caller. A multiplier below 1.0 is a
# discount, and discounts are promotions with their own accounting — not
# something a surge field should be able to smuggle in.
SURGE_FLOOR = 1.0
SURGE_CEILING = 3.0

EARTH_RADIUS_M = 6_371_000


def round_half_up(x: float) -> int:
    """Round to the nearest integer, ties toward +infinity.

    Python's round() is banker's rounding (2.5 -> 2), JavaScript's Math.round is
    half-up (2.5 -> 3). The core API's breaker fallback quotes in TypeScript, so
    using round() here meant the two disagreed by a cent on every odd-second
    duration (30 c/min x 61 s = 30.5 c). This is Math.round exactly, including
    the 0.49999999999999994 edge that floor(x + 0.5) gets wrong: subtracting
    the floor of a double is exact, so the tie test compares true values.
    """
    f = math.floor(x)
    return int(f + 1) if x - f >= 0.5 else int(f)


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class LatLng(_CamelModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class QuoteRequest(_CamelModel):
    pickup: LatLng
    dropoff: LatLng
    ride_class: str = "standard"
    # Zone surge measured by the core API. Clamped, not rejected, when out of
    # range — see applied_surge(). NaN/inf are rejected outright: a clamp cannot
    # make sense of them, and min/max on NaN silently returns garbage.
    surge_multiplier: Annotated[float, Field(allow_inf_nan=False)] | None = None
    # Road distance and duration from the routing provider (or the core API's
    # detour-factor estimate). Negative values are a caller bug, not a trip.
    distance_meters: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class FareBreakdown(_CamelModel):
    base_fare_cents: int
    distance_cents: int
    time_cents: int
    booking_fee_cents: int
    class_multiplier: float
    surge_multiplier: float


class QuoteResponse(_CamelModel):
    currency: str
    ride_class: str
    distance_meters: int
    duration_seconds: int
    fare_cents: int
    surge_multiplier: float
    breakdown: FareBreakdown


def current_surge(_pickup: LatLng) -> float:
    """Surge for a caller that did not measure one: none.

    The zone multiplier is computed in the core API, which owns the live driver
    index; this service has no supply signal of its own. Answering 1.0 rather
    than guessing keeps a caller that skipped surge on the base price — an
    unmeasured shortage is never a reason to charge more.
    """
    return 1.0


def applied_surge(requested: float | None, pickup: LatLng) -> float:
    """The multiplier this quote will actually use.

    Clamped to [SURGE_FLOOR, SURGE_CEILING] and rounded to one decimal. The
    rounding is part of the contract, not cosmetics: riders are shown "1.3x",
    and the breakdown must be the number that multiplied their fare, not
    1.2849 dressed up as 1.3.
    """
    raw = current_surge(pickup) if requested is None else requested
    clamped = min(max(raw, SURGE_FLOOR), SURGE_CEILING)
    return round_half_up(clamped * 10) / 10


def quote_fare(req: QuoteRequest) -> QuoteResponse:
    """Compute a fare estimate. Pure function — same input, same output.

    Every rounding step happens on integers of metres and seconds, in the same
    order as the core API's TypeScript fallback (pricing-client.ts). When the
    caller supplies distance and duration the whole calculation is integer
    inputs through IEEE-754 double arithmetic, which both runtimes perform
    identically, so the two quote the same cents by construction. Keep them in
    lock-step: a fallback that disagrees charges riders two different prices
    for the same trip depending on whether this service was up.
    """
    ride_class = req.ride_class if req.ride_class in CLASS_MULTIPLIERS else "standard"
    class_mult = CLASS_MULTIPLIERS[ride_class]
    surge = applied_surge(req.surge_multiplier, req.pickup)

    if req.distance_meters is not None:
        distance_m = round_half_up(req.distance_meters)
    else:
        distance_m = round_half_up(
            haversine_meters(req.pickup.lat, req.pickup.lng, req.dropoff.lat, req.dropoff.lng)
        )

    if req.duration_seconds is not None:
        duration_s = round_half_up(req.duration_seconds)
    else:
        duration_s = round_half_up((distance_m / 1000.0) / AVG_SPEED_KMH * 3600.0)

    distance_cents = round_half_up(PER_KM_CENTS * distance_m / 1000.0)
    time_cents = round_half_up(PER_MIN_CENTS * duration_s / 60.0)

    subtotal = (BASE_FARE_CENTS + distance_cents + time_cents) * class_mult * surge
    fare_cents = round_half_up(subtotal) + BOOKING_FEE_CENTS
    fare_cents = max(fare_cents, MIN_FARE_CENTS)

    return QuoteResponse(
        currency=CURRENCY,
        ride_class=ride_class,
        distance_meters=distance_m,
        duration_seconds=duration_s,
        fare_cents=fare_cents,
        surge_multiplier=surge,
        breakdown=FareBreakdown(
            base_fare_cents=BASE_FARE_CENTS,
            distance_cents=distance_cents,
            time_cents=time_cents,
            booking_fee_cents=BOOKING_FEE_CENTS,
            class_multiplier=class_mult,
            surge_multiplier=surge,
        ),
    )
