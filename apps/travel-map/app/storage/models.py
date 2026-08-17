"""Shared models and canonical values for private SQLite storage."""

from datetime import UTC, datetime


class StorageIntegrityError(RuntimeError):
    """Raised when private storage is absent, malformed, or insecure."""


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
