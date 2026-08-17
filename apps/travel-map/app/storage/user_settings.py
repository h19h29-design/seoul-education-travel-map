"""Encrypted full-replacement repository for the fixed user settings record."""

import sqlite3
from datetime import UTC, datetime
from typing import Literal, cast

from app.policy.models import VehicleUse
from app.routing.models import FuelType
from app.storage.crypto import PayloadCipher, UserDataUnavailableError
from app.storage.database import SqliteDatabase
from app.storage.models import (
    StorageIntegrityError,
    StoredUserSettings,
    format_storage_timestamp,
)
from app.trips.models import TripPattern

_SETTINGS_FIELDS = frozenset(
    {
        "default_origin_site_id",
        "default_trip_pattern",
        "default_duration_minutes",
        "vehicle_use",
        "fuel_type",
        "efficiency_km_per_liter",
        "parking_cost_krw",
        "route_sort",
    }
)


class UserSettingsRepository:
    def __init__(self, database: SqliteDatabase, cipher: PayloadCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def get(self, *, user_id: int) -> StoredUserSettings | None:
        _require_user_id(user_id)

        def operation(connection: sqlite3.Connection) -> StoredUserSettings | None:
            row = connection.execute(
                "SELECT encrypted_payload, encryption_version FROM user_settings "
                "WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            payload = self._cipher.decrypt_json(
                purpose="user-settings",
                owner_id=str(user_id),
                ciphertext=row[0],
                encryption_version=row[1],
            )
            return _settings_from_payload(payload)

        return await self._database.read(operation)

    async def replace(self, *, user_id: int, settings: StoredUserSettings) -> None:
        _require_user_id(user_id)
        if type(settings) is not StoredUserSettings:
            raise StorageIntegrityError("storage input is invalid")
        encrypted = self._cipher.encrypt_json(
            purpose="user-settings",
            owner_id=str(user_id),
            payload=_settings_to_payload(settings),
        )
        updated_at = format_storage_timestamp(datetime.now(UTC))

        def operation(connection: sqlite3.Connection) -> None:
            try:
                connection.execute(
                    "INSERT INTO user_settings("
                    "user_id, encrypted_payload, encryption_version, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                    "encrypted_payload=excluded.encrypted_payload, "
                    "encryption_version=excluded.encryption_version, "
                    "updated_at=excluded.updated_at",
                    (
                        user_id,
                        encrypted.ciphertext,
                        encrypted.encryption_version,
                        updated_at,
                    ),
                )
            except sqlite3.Error:
                raise StorageIntegrityError("storage input is invalid") from None

        await self._database.write(operation)


def _require_user_id(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise StorageIntegrityError("storage input is invalid")


def _settings_to_payload(settings: StoredUserSettings) -> dict[str, object]:
    return {
        "default_origin_site_id": settings.default_origin_site_id,
        "default_trip_pattern": settings.default_trip_pattern.value,
        "default_duration_minutes": settings.default_duration_minutes,
        "vehicle_use": settings.vehicle_use.value,
        "fuel_type": settings.fuel_type.value,
        "efficiency_km_per_liter": settings.efficiency_km_per_liter,
        "parking_cost_krw": settings.parking_cost_krw,
        "route_sort": settings.route_sort,
    }


def _settings_from_payload(payload: dict[str, object]) -> StoredUserSettings:
    if set(payload) != _SETTINGS_FIELDS:
        raise UserDataUnavailableError()
    default_origin_site_id = payload["default_origin_site_id"]
    default_trip_pattern = payload["default_trip_pattern"]
    default_duration_minutes = payload["default_duration_minutes"]
    vehicle_use = payload["vehicle_use"]
    fuel_type = payload["fuel_type"]
    efficiency_km_per_liter = payload["efficiency_km_per_liter"]
    parking_cost_krw = payload["parking_cost_krw"]
    route_sort = payload["route_sort"]
    if (
        (default_origin_site_id is not None and type(default_origin_site_id) is not str)
        or type(default_trip_pattern) is not str
        or type(default_duration_minutes) is not int
        or type(vehicle_use) is not str
        or type(fuel_type) is not str
        or type(efficiency_km_per_liter) is not float
        or type(parking_cost_krw) is not int
        or type(route_sort) is not str
    ):
        raise UserDataUnavailableError()
    try:
        return StoredUserSettings(
            default_origin_site_id=cast(str | None, default_origin_site_id),
            default_trip_pattern=TripPattern(cast(str, default_trip_pattern)),
            default_duration_minutes=cast(int, default_duration_minutes),
            vehicle_use=VehicleUse(cast(str, vehicle_use)),
            fuel_type=FuelType(cast(str, fuel_type)),
            efficiency_km_per_liter=cast(float, efficiency_km_per_liter),
            parking_cost_krw=cast(int, parking_cost_krw),
            route_sort=cast(Literal["time", "distance", "cost"], route_sort),
        )
    except (TypeError, ValueError):
        raise UserDataUnavailableError() from None
