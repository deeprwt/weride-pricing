"""Fare-calculation unit tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.fares import (
    MIN_FARE_CENTS,
    QuoteRequest,
    haversine_meters,
    quote_fare,
    round_half_up,
)

CITY_HALL = {"lat": 43.6532, "lng": -79.3832}
NORTH_YORK = {"lat": 43.7615, "lng": -79.4111}  # ~12 km north


def _quote(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"pickup": CITY_HALL, "dropoff": NORTH_YORK}
    payload.update(overrides)
    return quote_fare(QuoteRequest.model_validate(payload))


def test_haversine_known_distance() -> None:
    # Toronto City Hall -> CN Tower is ~1.2 km.
    d = haversine_meters(43.6532, -79.3832, 43.6426, -79.3871)
    assert 900 < d < 1600


def test_fare_scales_with_distance() -> None:
    near = quote_fare(
        QuoteRequest.model_validate(
            {"pickup": {"lat": 43.6532, "lng": -79.3832},
             "dropoff": {"lat": 43.6426, "lng": -79.3871}}
        )
    )
    far = quote_fare(
        QuoteRequest.model_validate(
            {"pickup": {"lat": 43.6532, "lng": -79.3832},
             "dropoff": {"lat": 43.7615, "lng": -79.4111}}  # ~12 km north
        )
    )
    assert far.fare_cents > near.fare_cents
    assert far.duration_seconds > 0


def test_min_fare_floor() -> None:
    q = quote_fare(
        QuoteRequest.model_validate(
            {"pickup": {"lat": 43.6532, "lng": -79.3832},
             "dropoff": {"lat": 43.6533, "lng": -79.3833}}  # a few metres
        )
    )
    assert q.fare_cents == 600  # clamped to MIN_FARE_CENTS


def test_premium_costs_more_than_standard() -> None:
    payload = {"pickup": {"lat": 43.6532, "lng": -79.3832},
               "dropoff": {"lat": 43.7615, "lng": -79.4111}}
    std = quote_fare(QuoteRequest.model_validate({**payload, "rideClass": "standard"}))
    prem = quote_fare(QuoteRequest.model_validate({**payload, "rideClass": "premium"}))
    assert prem.fare_cents > std.fare_cents


# --- Legacy behaviour when the new inputs are absent -------------------------


def test_absent_inputs_keep_straight_line_and_no_surge() -> None:
    q = _quote()
    expected_m = round_half_up(
        haversine_meters(CITY_HALL["lat"], CITY_HALL["lng"], NORTH_YORK["lat"], NORTH_YORK["lng"])
    )
    assert q.distance_meters == expected_m
    assert q.surge_multiplier == 1.0
    assert q.breakdown.surge_multiplier == 1.0
    # Golden value: the same input must keep producing the same cents.
    assert q.fare_cents == 2607


def test_explicit_none_matches_omitted() -> None:
    omitted = _quote()
    explicit = _quote(surgeMultiplier=None, distanceMeters=None, durationSeconds=None)
    assert explicit == omitted


# --- Surge --------------------------------------------------------------------


def test_surge_is_applied_and_reported() -> None:
    base = _quote(distanceMeters=15873, durationSeconds=1561)
    surged = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=1.3)
    assert surged.fare_cents > base.fare_cents
    assert surged.surge_multiplier == 1.3
    assert surged.breakdown.surge_multiplier == 1.3
    # (250 + 1905 + 781) * 1.3 = 3816.8 -> 3817, + 99 booking fee.
    assert surged.fare_cents == 3916


@pytest.mark.parametrize(
    ("requested", "applied"),
    [
        (10.0, 3.0),   # a runaway caller is capped
        (3.04, 3.0),
        (0.2, 1.0),    # surge can never become a discount
        (-5.0, 1.0),
        (1.0, 1.0),
    ],
)
def test_surge_is_clamped_server_side(requested: float, applied: float) -> None:
    q = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=requested)
    assert q.surge_multiplier == applied
    assert q.breakdown.surge_multiplier == applied


def test_clamped_surge_charges_what_it_reports() -> None:
    capped = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=10.0)
    at_ceiling = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=3.0)
    assert capped.fare_cents == at_ceiling.fare_cents


@pytest.mark.parametrize(
    ("requested", "applied"),
    [(1.25, 1.3), (1.249, 1.2), (1.96, 2.0), (2.04, 2.0)],
)
def test_surge_is_rounded_to_one_decimal(requested: float, applied: float) -> None:
    q = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=requested)
    assert q.surge_multiplier == applied


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_surge_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate_json(
            '{"pickup": {"lat": 43.6532, "lng": -79.3832},'
            ' "dropoff": {"lat": 43.7615, "lng": -79.4111},'
            f' "surgeMultiplier": "{bad}"}}'
        )


def test_min_fare_floor_applies_after_surge() -> None:
    # 250 * 1.2 + 99 = 399: the floor lifts the surged total, and the breakdown
    # still reports the surge that was multiplied in before the floor applied.
    q = _quote(distanceMeters=0, durationSeconds=0, surgeMultiplier=1.2)
    assert q.fare_cents == MIN_FARE_CENTS
    assert q.surge_multiplier == 1.2


# --- Road distance and duration ---------------------------------------------


def test_provided_route_replaces_straight_line() -> None:
    q = _quote(distanceMeters=15873, durationSeconds=1561)
    assert q.distance_meters == 15873
    assert q.duration_seconds == 1561
    assert q.breakdown.distance_cents == 1905   # 1.20 $/km * 15.873 km
    assert q.breakdown.time_cents == 781        # 0.30 $/min * 26.02 min


def test_distance_without_duration_derives_duration_at_average_speed() -> None:
    q = _quote(distanceMeters=15873)
    assert q.distance_meters == 15873
    assert q.duration_seconds == 2041  # 15.873 km at 28 km/h


def test_longer_road_route_costs_more_than_straight_line() -> None:
    straight = _quote()
    road = _quote(distanceMeters=straight.distance_meters * 1.35,
                  durationSeconds=straight.duration_seconds * 1.35)
    assert road.fare_cents > straight.fare_cents


@pytest.mark.parametrize("field", ["distanceMeters", "durationSeconds"])
def test_negative_route_inputs_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate({"pickup": CITY_HALL, "dropoff": NORTH_YORK, field: -1})


def test_snake_case_fields_are_accepted() -> None:
    req = QuoteRequest(
        pickup=CITY_HALL,  # type: ignore[arg-type]
        dropoff=NORTH_YORK,  # type: ignore[arg-type]
        surge_multiplier=1.5,
        distance_meters=15873,
        duration_seconds=1561,
    )
    assert quote_fare(req).surge_multiplier == 1.5


# --- Rounding parity with the TypeScript fallback ---------------------------


def test_round_half_up_matches_javascript_math_round() -> None:
    assert round_half_up(2.5) == 3        # round() would give 2
    assert round_half_up(30.5) == 31      # round() would give 30
    assert round_half_up(0.49999999999999994) == 0  # floor(x + 0.5) would give 1
    assert round_half_up(-2.5) == -2      # Math.round(-2.5) === -2
    assert round_half_up(7.0) == 7


def test_ties_round_up_like_the_fallback() -> None:
    # Two ties in one quote: 30 c/min * 1201 s = 600.5 c, then
    # (250 + 1200 + 601) * 1.5 = 3076.5. Banker's rounding would quote 3174;
    # pricing-client.ts's Math.round quotes 3176, and so must this service.
    q = _quote(distanceMeters=10000, durationSeconds=1201, rideClass="xl")
    assert q.breakdown.time_cents == 601
    assert q.fare_cents == 3176


# Produced by pricing-client.ts localQuote — the TypeScript breaker fallback —
# on 2026-09-13, for the same inputs, then checked against this service.
# (distanceMeters, durationSeconds, rideClass, surge sent, fareCents,
#  surge applied, distanceCents, timeCents). Half-metre and half-second inputs
# and a surge that rounds (1.25 -> 1.3) are in here on purpose: those are where
# the two runtimes could drift. If either implementation changes, regenerate
# from the TypeScript side and change both in the same commit.
TYPESCRIPT_FALLBACK_QUOTES = [
    (850, 181, "standard", 1.25, 675, 1.3, 102, 91),
    (850, 181, "standard", 2.3, 1118, 2.3, 102, 91),
    (850, 181, "standard", 3, 1428, 3, 102, 91),
    (850, 181, "xl", 1.25, 963, 1.3, 102, 91),
    (850, 181, "xl", 2.3, 1627, 2.3, 102, 91),
    (850, 181, "xl", 3, 2093, 3, 102, 91),
    (850, 181, "premium", 1.25, 1251, 1.3, 102, 91),
    (850, 181, "premium", 2.3, 2137, 2.3, 102, 91),
    (850, 181, "premium", 3, 2757, 3, 102, 91),
    (10000, 1201, "standard", 1.25, 2765, 1.3, 1200, 601),
    (10000, 1201, "standard", 2.3, 4816, 2.3, 1200, 601),
    (10000, 1201, "standard", 3, 6252, 3, 1200, 601),
    (10000, 1201, "xl", 1.25, 4098, 1.3, 1200, 601),
    (10000, 1201, "xl", 2.3, 7175, 2.3, 1200, 601),
    (10000, 1201, "xl", 3, 9329, 3, 1200, 601),
    (10000, 1201, "premium", 1.25, 5432, 1.3, 1200, 601),
    (10000, 1201, "premium", 2.3, 9534, 2.3, 1200, 601),
    (10000, 1201, "premium", 3, 12405, 3, 1200, 601),
    (23456.5, 2999.5, "standard", 1.25, 6034, 1.3, 2815, 1500),
    (23456.5, 2999.5, "standard", 2.3, 10599, 2.3, 2815, 1500),
    (23456.5, 2999.5, "standard", 3, 13794, 3, 2815, 1500),
    (23456.5, 2999.5, "xl", 1.25, 9001, 1.3, 2815, 1500),
    (23456.5, 2999.5, "xl", 2.3, 15848, 2.3, 2815, 1500),
    (23456.5, 2999.5, "xl", 3, 20642, 3, 2815, 1500),
    (23456.5, 2999.5, "premium", 1.25, 11968, 1.3, 2815, 1500),
    (23456.5, 2999.5, "premium", 2.3, 21098, 2.3, 2815, 1500),
    (23456.5, 2999.5, "premium", 3, 27489, 3, 2815, 1500),
    (41250, 3721, "standard", 1.25, 9278, 1.3, 4950, 1861),
    (41250, 3721, "standard", 2.3, 16339, 2.3, 4950, 1861),
    (41250, 3721, "standard", 3, 21282, 3, 4950, 1861),
    (41250, 3721, "xl", 1.25, 13868, 1.3, 4950, 1861),
    (41250, 3721, "xl", 2.3, 24459, 2.3, 4950, 1861),
    (41250, 3721, "xl", 3, 31874, 3, 4950, 1861),
    (41250, 3721, "premium", 1.25, 18458, 1.3, 4950, 1861),
    (41250, 3721, "premium", 2.3, 32580, 2.3, 4950, 1861),
    (41250, 3721, "premium", 3, 42465, 3, 4950, 1861),
]


@pytest.mark.parametrize(
    ("distance", "duration", "ride_class", "surge", "fare", "applied", "distance_cents", "time_cents"),
    TYPESCRIPT_FALLBACK_QUOTES,
)
def test_quotes_the_same_cents_as_the_typescript_fallback(
    distance: float,
    duration: float,
    ride_class: str,
    surge: float,
    fare: int,
    applied: float,
    distance_cents: int,
    time_cents: int,
) -> None:
    q = _quote(distanceMeters=distance, durationSeconds=duration, rideClass=ride_class, surgeMultiplier=surge)
    assert (q.fare_cents, q.surge_multiplier, q.breakdown.distance_cents, q.breakdown.time_cents) == (
        fare,
        applied,
        distance_cents,
        time_cents,
    )


# --- Surge arriving from the core API's zone measurement ----------------------


@pytest.mark.parametrize("tenths", range(10, 31))
def test_a_multiplier_the_api_already_rounded_is_charged_exactly_as_shown(tenths: int) -> None:
    # SurgeService rounds to one decimal with Math.round(x * 10) / 10 before it
    # quotes, and the rider is shown that number. Re-rounding here must be a
    # no-op for every value it can send: 2.3 is 22.999999999999996 in binary,
    # so a truncating implementation would quietly charge 2.2x for a 2.3x quote.
    shown = round_half_up(tenths / 10 * 10) / 10  # exactly what the API sends
    q = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=shown)
    assert q.surge_multiplier == shown
    assert q.breakdown.surge_multiplier == shown


def test_surge_multiplies_the_ride_never_the_booking_fee_or_the_floor() -> None:
    base = _quote(distanceMeters=15873, durationSeconds=1561)
    subtotal = base.breakdown.base_fare_cents + base.breakdown.distance_cents + base.breakdown.time_cents
    for tenths in range(10, 31):
        surge = tenths / 10
        for ride_class, class_multiplier in (("standard", 1.0), ("xl", 1.5), ("premium", 2.0)):
            q = _quote(distanceMeters=15873, durationSeconds=1561, surgeMultiplier=surge, rideClass=ride_class)
            expected = round_half_up(subtotal * class_multiplier * q.surge_multiplier) + q.breakdown.booking_fee_cents
            assert q.breakdown.booking_fee_cents == 99
            assert q.fare_cents == max(expected, MIN_FARE_CENTS)


def test_a_higher_surge_never_quotes_a_lower_fare() -> None:
    for ride_class in ("standard", "xl", "premium"):
        previous = 0
        for tenths in range(10, 31):
            fare = _quote(
                distanceMeters=6400, durationSeconds=900, surgeMultiplier=tenths / 10, rideClass=ride_class
            ).fare_cents
            assert fare >= previous
            previous = fare
