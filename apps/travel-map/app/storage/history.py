"""Encrypted, seven-day calculation-history repository."""

import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime

from app.routing.models import TravelMode
from app.storage.crypto import PayloadCipher, UserDataUnavailableError
from app.storage.database import SqliteDatabase
from app.storage.models import (
    HISTORY_RETENTION,
    HistoryCursor,
    HistoryDetail,
    HistoryListItem,
    HistoryMetadata,
    HistoryPage,
    HistoryRecalculationDraft,
    HistoryRouteLegSummary,
    HistorySummary,
    StorageIntegrityError,
    expected_history_expiry_timestamp,
    format_storage_timestamp,
    parse_storage_timestamp,
)
from app.trips.models import RouteDirection, TripPattern

_DRAFT_FIELDS = frozenset(
    {
        "origin_site_id",
        "origin_name",
        "destination_name",
        "destination_address",
        "trip_pattern",
        "starts_at",
        "ends_at",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "classification",
        "allowance_status",
        "allowance_krw",
        "route_legs",
        "rule_set_id",
        "effective_from",
    }
)
_ROUTE_LEG_FIELDS = frozenset(
    {
        "direction",
        "mode",
        "duration_seconds",
        "distance_meters",
        "mobility_cost_krw",
    }
)


class HistoryRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        cipher: PayloadCipher,
        clock: Callable[[], datetime],
    ) -> None:
        self._database = database
        self._cipher = cipher
        self._clock = clock

    async def create(
        self,
        *,
        user_id: int,
        draft: HistoryRecalculationDraft,
        summary: HistorySummary,
    ) -> HistoryMetadata:
        _require_user_id(user_id)
        draft_payload = _draft_to_payload(draft)
        summary_payload = _summary_to_payload(summary)
        created_at = _trusted_now(self._clock)
        expires_at = created_at + HISTORY_RETENTION
        created_at_text = format_storage_timestamp(created_at)
        expires_at_text = format_storage_timestamp(expires_at)
        history_id = _new_history_id()
        owner_id = _history_owner_id(user_id, history_id)
        encrypted_draft = self._cipher.encrypt_json(
            purpose="history-input", owner_id=owner_id, payload=draft_payload
        )
        encrypted_summary = self._cipher.encrypt_json(
            purpose="history-summary", owner_id=owner_id, payload=summary_payload
        )

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _delete_expired_for_user(connection, user_id, created_at_text)
                connection.execute(
                    "INSERT INTO calculation_history("
                    "id, user_id, created_at, expires_at, encrypted_input, "
                    "encrypted_summary, encryption_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        history_id,
                        user_id,
                        created_at_text,
                        expires_at_text,
                        encrypted_draft.ciphertext,
                        encrypted_summary.ciphertext,
                        encrypted_draft.encryption_version,
                    ),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage input is invalid") from None
            except BaseException:
                connection.rollback()
                raise

        await self._database.write(operation)
        return HistoryMetadata(
            id=history_id,
            user_id=user_id,
            created_at=created_at,
            expires_at=expires_at,
        )

    async def list_page(
        self,
        *,
        user_id: int,
        before: HistoryCursor | None,
        limit: int = 50,
    ) -> HistoryPage:
        _require_user_id(user_id)
        _require_limit(limit)
        before_values = _cursor_values(before)
        now_text = format_storage_timestamp(_trusted_now(self._clock))

        def operation(connection: sqlite3.Connection) -> HistoryPage:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _delete_expired_for_user(connection, user_id, now_text)
                if before_values is None:
                    rows = connection.execute(
                        "SELECT id, user_id, created_at, expires_at, encrypted_input, "
                        "encrypted_summary, encryption_version FROM calculation_history "
                        "WHERE user_id=? "
                        "ORDER BY created_at DESC, id DESC LIMIT ?",
                        (user_id, limit + 1),
                    ).fetchall()
                else:
                    before_created_at, before_history_id = before_values
                    rows = connection.execute(
                        "SELECT id, user_id, created_at, expires_at, encrypted_input, "
                        "encrypted_summary, encryption_version FROM calculation_history "
                        "WHERE user_id=? AND "
                        "(created_at<? OR (created_at=? AND id<?)) "
                        "ORDER BY created_at DESC, id DESC LIMIT ?",
                        (
                            user_id,
                            before_created_at,
                            before_created_at,
                            before_history_id,
                            limit + 1,
                        ),
                    ).fetchall()
                details = tuple(
                    _detail_from_row(row, self._cipher) for row in rows[:limit]
                )
                next_cursor = (
                    HistoryCursor(
                        created_at=details[-1].metadata.created_at,
                        history_id=details[-1].metadata.id,
                    )
                    if len(rows) > limit
                    else None
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage row is invalid") from None
            except BaseException:
                connection.rollback()
                raise
            return HistoryPage(
                items=tuple(_list_item_from_detail(detail) for detail in details),
                next_cursor=next_cursor,
            )

        return await self._database.write(operation)

    async def get(self, *, user_id: int, history_id: str) -> HistoryDetail | None:
        _require_user_id(user_id)
        _require_history_id(history_id)
        now_text = format_storage_timestamp(_trusted_now(self._clock))

        def operation(connection: sqlite3.Connection) -> HistoryDetail | None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _delete_expired_for_user(connection, user_id, now_text)
                row = connection.execute(
                    "SELECT id, user_id, created_at, expires_at, encrypted_input, "
                    "encrypted_summary, encryption_version FROM calculation_history "
                    "WHERE id=? AND user_id=?",
                    (history_id, user_id),
                ).fetchone()
                detail = (
                    _detail_from_row(row, self._cipher) if row is not None else None
                )
                connection.commit()
                return detail
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage row is invalid") from None
            except BaseException:
                connection.rollback()
                raise

        return await self._database.write(operation)

    async def delete(self, *, user_id: int, history_id: str) -> bool:
        _require_user_id(user_id)
        _require_history_id(history_id)
        now_text = format_storage_timestamp(_trusted_now(self._clock))

        def operation(connection: sqlite3.Connection) -> bool:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _delete_expired_for_user(connection, user_id, now_text)
                deleted = connection.execute(
                    "DELETE FROM calculation_history WHERE id=? AND user_id=?",
                    (history_id, user_id),
                ).rowcount
                connection.commit()
                return deleted > 0
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage row is invalid") from None
            except BaseException:
                connection.rollback()
                raise

        return await self._database.write(operation)

    async def delete_all(self, *, user_id: int) -> int:
        _require_user_id(user_id)
        now_text = format_storage_timestamp(_trusted_now(self._clock))

        def operation(connection: sqlite3.Connection) -> int:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _delete_expired_for_user(connection, user_id, now_text)
                deleted = connection.execute(
                    "DELETE FROM calculation_history WHERE user_id=?", (user_id,)
                ).rowcount
                connection.commit()
                return deleted
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage row is invalid") from None
            except BaseException:
                connection.rollback()
                raise

        return await self._database.write(operation)


def _delete_expired_for_user(
    connection: sqlite3.Connection, user_id: int, now_text: str
) -> int:
    rows = connection.execute(
        "SELECT rowid, created_at, expires_at FROM calculation_history WHERE user_id=?",
        (user_id,),
    ).fetchall()
    deleted = 0
    for rowid, created_at, expires_at in rows:
        try:
            expected_expiry = _expected_expiry_from_created_at(created_at)
        except StorageIntegrityError:
            expected_expiry = None
        if (
            type(expires_at) is not str
            or expected_expiry is None
            or expires_at != expected_expiry
            or expected_expiry <= now_text
        ):
            deleted += connection.execute(
                "DELETE FROM calculation_history WHERE rowid=? AND user_id=?",
                (rowid, user_id),
            ).rowcount
    return deleted


def _expected_expiry_from_created_at(created_at: object) -> str:
    if type(created_at) is not str:
        raise StorageIntegrityError("storage row is invalid")
    return expected_history_expiry_timestamp(created_at)


def _trusted_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
        if type(value) is not datetime:
            raise TypeError
        return parse_storage_timestamp(format_storage_timestamp(value))
    except (TypeError, ValueError, StorageIntegrityError):
        raise StorageIntegrityError("storage input is invalid") from None


def _require_user_id(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise StorageIntegrityError("storage input is invalid")


def _require_limit(value: object) -> None:
    if type(value) is not int or not 1 <= value <= 100:
        raise StorageIntegrityError("storage input is invalid")


def _new_history_id() -> str:
    return secrets.token_urlsafe(16)


def _history_owner_id(user_id: int, history_id: str) -> str:
    return f"{user_id}:{history_id}"


def _require_history_id(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 22
        or not all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )
    ):
        raise StorageIntegrityError("storage input is invalid")


def _cursor_values(before: HistoryCursor | None) -> tuple[str, str] | None:
    if before is None:
        return None
    if type(before) is not HistoryCursor:
        raise StorageIntegrityError("storage input is invalid")
    try:
        _require_history_id(before.history_id)
        return format_storage_timestamp(before.created_at), before.history_id
    except (AttributeError, StorageIntegrityError):
        raise StorageIntegrityError("storage input is invalid") from None


def _draft_to_payload(draft: HistoryRecalculationDraft) -> dict[str, object]:
    if type(draft) is not HistoryRecalculationDraft:
        raise StorageIntegrityError("storage input is invalid")
    try:
        return {
            "origin_site_id": _input_text(draft.origin_site_id),
            "origin_name": _input_text(draft.origin_name),
            "destination_name": _input_text(draft.destination_name),
            "destination_address": _input_text(draft.destination_address),
            "trip_pattern": _input_enum(draft.trip_pattern, TripPattern),
            "starts_at": _input_timestamp(draft.starts_at),
            "ends_at": _input_timestamp(draft.ends_at),
        }
    except (AttributeError, StorageIntegrityError):
        raise StorageIntegrityError("storage input is invalid") from None


def _summary_to_payload(summary: HistorySummary) -> dict[str, object]:
    if type(summary) is not HistorySummary:
        raise StorageIntegrityError("storage input is invalid")
    try:
        if type(summary.route_legs) is not tuple:
            raise StorageIntegrityError("storage input is invalid")
        allowance = summary.allowance_krw
        if allowance is not None and (type(allowance) is not int or allowance < 0):
            raise StorageIntegrityError("storage input is invalid")
        return {
            "classification": _input_text(summary.classification),
            "allowance_status": _input_text(summary.allowance_status),
            "allowance_krw": allowance,
            "route_legs": [_route_leg_to_payload(leg) for leg in summary.route_legs],
            "rule_set_id": _input_optional_text(summary.rule_set_id),
            "effective_from": _input_optional_text(summary.effective_from),
        }
    except (AttributeError, StorageIntegrityError):
        raise StorageIntegrityError("storage input is invalid") from None


def _route_leg_to_payload(leg: HistoryRouteLegSummary) -> dict[str, object]:
    if type(leg) is not HistoryRouteLegSummary:
        raise StorageIntegrityError("storage input is invalid")
    if type(leg.duration_seconds) is not int or leg.duration_seconds < 0:
        raise StorageIntegrityError("storage input is invalid")
    if type(leg.distance_meters) is not int or leg.distance_meters < 0:
        raise StorageIntegrityError("storage input is invalid")
    if leg.mobility_cost_krw is not None and (
        type(leg.mobility_cost_krw) is not int or leg.mobility_cost_krw < 0
    ):
        raise StorageIntegrityError("storage input is invalid")
    return {
        "direction": _input_enum(leg.direction, RouteDirection),
        "mode": _input_enum(leg.mode, TravelMode),
        "duration_seconds": leg.duration_seconds,
        "distance_meters": leg.distance_meters,
        "mobility_cost_krw": leg.mobility_cost_krw,
    }


def _input_text(value: object) -> str:
    if type(value) is not str or not value:
        raise StorageIntegrityError("storage input is invalid")
    return value


def _input_optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _input_text(value)


def _input_enum(
    value: object,
    enum_type: type[TripPattern] | type[RouteDirection] | type[TravelMode],
) -> str:
    if type(value) is not enum_type:
        raise StorageIntegrityError("storage input is invalid")
    return value.value


def _input_timestamp(value: object) -> str:
    if type(value) is not datetime:
        raise StorageIntegrityError("storage input is invalid")
    try:
        return format_storage_timestamp(value)
    except StorageIntegrityError:
        raise StorageIntegrityError("storage input is invalid") from None


def _detail_from_row(row: tuple[object, ...], cipher: PayloadCipher) -> HistoryDetail:
    try:
        (
            history_id,
            user_id,
            created_at_text,
            expires_at_text,
            encrypted_input,
            encrypted_summary,
            encryption_version,
        ) = row
        if (
            type(history_id) is not str
            or type(user_id) is not int
            or type(created_at_text) is not str
            or type(expires_at_text) is not str
            or type(encrypted_input) is not bytes
            or type(encrypted_summary) is not bytes
            or type(encryption_version) is not int
        ):
            raise TypeError
        _require_user_id(user_id)
        _require_history_id(history_id)
        expected_expiry_text = _expected_expiry_from_created_at(created_at_text)
        if expires_at_text != expected_expiry_text:
            raise StorageIntegrityError("storage row is invalid")
        metadata = HistoryMetadata(
            id=history_id,
            user_id=user_id,
            created_at=parse_storage_timestamp(created_at_text),
            expires_at=parse_storage_timestamp(expected_expiry_text),
        )
    except (TypeError, ValueError, StorageIntegrityError):
        raise StorageIntegrityError("storage row is invalid") from None
    owner_id = _history_owner_id(metadata.user_id, metadata.id)
    draft_payload = cipher.decrypt_json(
        purpose="history-input",
        owner_id=owner_id,
        ciphertext=encrypted_input,
        encryption_version=encryption_version,
    )
    summary_payload = cipher.decrypt_json(
        purpose="history-summary",
        owner_id=owner_id,
        ciphertext=encrypted_summary,
        encryption_version=encryption_version,
    )
    return HistoryDetail(
        metadata=metadata,
        draft=_draft_from_payload(draft_payload),
        summary=_summary_from_payload(summary_payload),
    )


def _draft_from_payload(payload: dict[str, object]) -> HistoryRecalculationDraft:
    if set(payload) != _DRAFT_FIELDS:
        raise UserDataUnavailableError()
    try:
        return HistoryRecalculationDraft(
            origin_site_id=_payload_text(payload["origin_site_id"]),
            origin_name=_payload_text(payload["origin_name"]),
            destination_name=_payload_text(payload["destination_name"]),
            destination_address=_payload_text(payload["destination_address"]),
            trip_pattern=TripPattern(_payload_text(payload["trip_pattern"])),
            starts_at=_payload_timestamp(payload["starts_at"]),
            ends_at=_payload_timestamp(payload["ends_at"]),
        )
    except (KeyError, TypeError, ValueError, StorageIntegrityError):
        raise UserDataUnavailableError() from None


def _summary_from_payload(payload: dict[str, object]) -> HistorySummary:
    if set(payload) != _SUMMARY_FIELDS:
        raise UserDataUnavailableError()
    try:
        classification = _payload_text(payload["classification"])
        allowance_status = _payload_text(payload["allowance_status"])
        allowance_krw = payload["allowance_krw"]
        if allowance_krw is not None and (
            type(allowance_krw) is not int or allowance_krw < 0
        ):
            raise ValueError
        route_legs = payload["route_legs"]
        if type(route_legs) is not list:
            raise TypeError
        return HistorySummary(
            classification=classification,
            allowance_status=allowance_status,
            allowance_krw=allowance_krw,
            route_legs=tuple(_route_leg_from_payload(value) for value in route_legs),
            rule_set_id=_payload_optional_text(payload["rule_set_id"]),
            effective_from=_payload_optional_text(payload["effective_from"]),
        )
    except (KeyError, TypeError, ValueError, StorageIntegrityError):
        raise UserDataUnavailableError() from None


def _route_leg_from_payload(value: object) -> HistoryRouteLegSummary:
    if type(value) is not dict or set(value) != _ROUTE_LEG_FIELDS:
        raise UserDataUnavailableError()
    try:
        duration_seconds = value["duration_seconds"]
        distance_meters = value["distance_meters"]
        mobility_cost_krw = value["mobility_cost_krw"]
        if type(duration_seconds) is not int or duration_seconds < 0:
            raise ValueError
        if type(distance_meters) is not int or distance_meters < 0:
            raise ValueError
        if mobility_cost_krw is not None and (
            type(mobility_cost_krw) is not int or mobility_cost_krw < 0
        ):
            raise ValueError
        return HistoryRouteLegSummary(
            direction=RouteDirection(_payload_text(value["direction"])),
            mode=TravelMode(_payload_text(value["mode"])),
            duration_seconds=duration_seconds,
            distance_meters=distance_meters,
            mobility_cost_krw=mobility_cost_krw,
        )
    except (KeyError, TypeError, ValueError):
        raise UserDataUnavailableError() from None


def _payload_text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError
    return value


def _payload_optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _payload_text(value)


def _payload_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError
    return parse_storage_timestamp(value)


def _list_item_from_detail(detail: HistoryDetail) -> HistoryListItem:
    return HistoryListItem(
        metadata=detail.metadata,
        origin_name=detail.draft.origin_name,
        destination_name=detail.draft.destination_name,
        trip_pattern=detail.draft.trip_pattern,
        classification=detail.summary.classification,
        allowance_status=detail.summary.allowance_status,
        allowance_krw=detail.summary.allowance_krw,
    )
