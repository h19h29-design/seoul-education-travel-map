"""Shared models and canonical values for private SQLite storage."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Literal

from app.policy.models import VehicleUse
from app.routing.models import FuelType, TravelMode
from app.trips.models import RouteDirection, TripPattern


class StorageIntegrityError(RuntimeError):
    """Raised when private storage is absent, malformed, or insecure."""


HISTORY_RETENTION = timedelta(hours=168)


def format_storage_timestamp(value: datetime) -> str:
    """Return the only timestamp representation allowed in private storage."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageIntegrityError("storage timestamp timezone is invalid")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def parse_storage_timestamp(value: str) -> datetime:
    """Parse a canonical UTC microsecond timestamp without accepting aliases."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise StorageIntegrityError("storage timestamp is invalid") from None
    if format_storage_timestamp(parsed) != value:
        raise StorageIntegrityError("storage timestamp is invalid")
    return parsed


def expected_history_expiry_timestamp(created_at: str) -> str:
    """Derive the only allowed history expiry from its canonical creation time."""
    return format_storage_timestamp(
        parse_storage_timestamp(created_at) + HISTORY_RETENTION
    )


@dataclass(frozen=True)
class UserRecord:
    id: int
    created_at: datetime
    last_login_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "id")
        format_storage_timestamp(self.created_at)
        format_storage_timestamp(self.last_login_at)


@dataclass(frozen=True)
class SessionRecord:
    user_id: int
    token_hmac: bytes
    csrf_hmac: bytes
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.user_id, "user_id")
        _require_digest(self.token_hmac, "token_hmac")
        _require_digest(self.csrf_hmac, "csrf_hmac")
        format_storage_timestamp(self.created_at)
        format_storage_timestamp(self.expires_at)


@dataclass(frozen=True)
class HistoryRecalculationDraft:
    origin_site_id: str
    origin_name: str
    destination_name: str
    destination_address: str
    trip_pattern: TripPattern
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class HistoryRouteLegSummary:
    direction: RouteDirection
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None


@dataclass(frozen=True)
class HistorySummary:
    classification: str
    allowance_status: str
    allowance_krw: int | None
    route_legs: tuple[HistoryRouteLegSummary, ...]
    rule_set_id: str | None
    effective_from: str | None


@dataclass(frozen=True)
class HistoryMetadata:
    id: str
    user_id: int
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class HistoryListItem:
    metadata: HistoryMetadata
    origin_name: str
    destination_name: str
    trip_pattern: TripPattern
    classification: str
    allowance_status: str
    allowance_krw: int | None


@dataclass(frozen=True)
class HistoryDetail:
    metadata: HistoryMetadata
    draft: HistoryRecalculationDraft
    summary: HistorySummary


@dataclass(frozen=True)
class HistoryCursor:
    created_at: datetime
    history_id: str


@dataclass(frozen=True)
class HistoryPage:
    items: tuple[HistoryListItem, ...]
    next_cursor: HistoryCursor | None


@dataclass(frozen=True)
class CleanupCounts:
    oauth_attempts: int
    sessions: int
    history: int
    users: int


@dataclass(frozen=True)
class StoredUserSettings:
    default_origin_site_id: str | None
    default_trip_pattern: TripPattern
    default_duration_minutes: int
    vehicle_use: VehicleUse
    fuel_type: FuelType
    efficiency_km_per_liter: float
    parking_cost_krw: int
    route_sort: Literal["time", "distance", "cost"]

    def __post_init__(self) -> None:
        if self.default_origin_site_id is not None and (
            type(self.default_origin_site_id) is not str
            or not self.default_origin_site_id
        ):
            raise ValueError("default_origin_site_id must be a nonempty string or None")
        if type(self.default_trip_pattern) is not TripPattern:
            raise TypeError("default_trip_pattern must be TripPattern")
        if (
            type(self.default_duration_minutes) is not int
            or not 2 <= self.default_duration_minutes <= 1_440
        ):
            raise ValueError("default_duration_minutes must be an integer in [2, 1440]")
        if type(self.vehicle_use) is not VehicleUse:
            raise TypeError("vehicle_use must be VehicleUse")
        if type(self.fuel_type) is not FuelType:
            raise TypeError("fuel_type must be FuelType")
        if (
            type(self.efficiency_km_per_liter) is not float
            or not isfinite(self.efficiency_km_per_liter)
            or not 3.0 <= self.efficiency_km_per_liter <= 30.0
        ):
            raise ValueError(
                "efficiency_km_per_liter must be a finite float in [3, 30]"
            )
        if (
            type(self.parking_cost_krw) is not int
            or not 0 <= self.parking_cost_krw <= 100_000
        ):
            raise ValueError("parking_cost_krw must be an integer in [0, 100000]")
        if self.route_sort not in {"time", "distance", "cost"}:
            raise ValueError("route_sort is invalid")


DEFAULT_USER_SETTINGS = StoredUserSettings(
    default_origin_site_id=None,
    default_trip_pattern=TripPattern.ROUND_TRIP,
    default_duration_minutes=300,
    vehicle_use=VehicleUse.NONE,
    fuel_type=FuelType.GASOLINE,
    efficiency_km_per_liter=10.0,
    parking_cost_krw=0,
    route_sort="time",
)


def _require_positive_int(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_digest(value: object, name: str) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes")
