"""Application-owned dependencies and production fail-closed assembly."""

import json
import sqlite3
from base64 import b64decode
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import orjson
from pydantic import SecretStr

from app.auth.models import UserServices
from app.auth.oauth import KakaoOidcClient, OAuthAttemptRepository
from app.auth.session import SessionService
from app.cache import TtlLruCache
from app.institutions.store import InstitutionStore
from app.policy.coverage import CoverageService, verify_geodata_resources
from app.policy.engine import PolicyEngine
from app.policy.rules import RuleRepository
from app.providers.kakao_local import (
    BoundingBox,
    KakaoLocalClient,
    PlaceSearchResult,
    ReversePlaceResult,
)
from app.rate_limit import FixedWindowRateLimiter
from app.routing.bootstrap import build_classification_provider, build_route_providers
from app.routing.models import Coordinate
from app.routing.orchestrator import RouteOrchestrator
from app.routing.provider import RouteProvider
from app.settings import Settings
from app.storage.crypto import PayloadCipher
from app.storage.database import SqliteDatabase
from app.storage.history import HistoryRepository
from app.storage.models import StorageIntegrityError
from app.storage.retention import RetentionCleaner
from app.storage.user_settings import UserSettingsRepository
from app.storage.users import UserSessionRepository


class PlaceClient(Protocol):
    async def search(self, query: str, *, bounds: BoundingBox) -> PlaceSearchResult:
        raise NotImplementedError

    async def reverse_geocode(self, coordinate: Coordinate) -> ReversePlaceResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


@dataclass
class AppDependencies:
    settings: Settings
    institutions: InstitutionStore
    coverage: CoverageService
    policy: PolicyEngine
    route_orchestrator: RouteOrchestrator
    classification_provider: RouteProvider
    place_client: PlaceClient
    cache: TtlLruCache
    rate_limiter: FixedWindowRateLimiter
    seoul_geojson: bytes
    support_geojson: bytes
    user_services: UserServices | None = None
    _closed_resource_ids: set[int] = field(default_factory=set, init=False)

    async def aclose(self) -> None:
        """Close each injectable external resource once, even after one failure."""

        failures: list[Exception] = []
        for resource in self._resources_to_close():
            identifier = id(resource)
            if identifier in self._closed_resource_ids:
                continue
            close = getattr(resource, "aclose", None)
            if not callable(close):
                continue
            self._closed_resource_ids.add(identifier)
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - cleanup reaches later resources
                failures.append(exc)
        if failures:
            raise failures[0]

    def _resources_to_close(self) -> Iterable[object]:
        providers = getattr(self.route_orchestrator, "_providers", {})
        if isinstance(providers, dict):
            for chain in providers.values():
                if isinstance(chain, tuple):
                    yield from chain
        yield self.classification_provider
        yield self.place_client
        if type(self.user_services) is UserServices:
            yield self.user_services.oidc_client


def build_production_dependencies(settings: Settings) -> AppDependencies:
    """Load only verified local artifacts; missing artifacts prevent production start."""

    if type(settings) is not Settings:
        raise TypeError("settings must be an exact Settings")
    root = Path(__file__).resolve().parents[1]
    resources = root / "resources"
    snapshot_root = resources / "institution-snapshots"
    if not snapshot_root.is_dir():
        raise RuntimeError("verified institution snapshot is unavailable")
    geodata = verify_geodata_resources(resources / "geodata", verify_source=False)
    seoul_geojson = _verified_geojson_bytes(geodata.seoul_geojson, "seoul.geojson")
    support_geojson = _verified_geojson_bytes(
        geodata.support_geojson,
        "seoul-plus-12km.geojson",
    )
    route_providers = build_route_providers(settings)
    user_services = _optional_user_services(settings)
    return AppDependencies(
        settings=settings,
        institutions=InstitutionStore.load(snapshot_root),
        coverage=CoverageService.from_resources(
            resources / "geodata", verify_source=False
        ),
        policy=PolicyEngine(
            RuleRepository.from_directory(resources / "rules", require_hashes=True)
        ),
        route_orchestrator=RouteOrchestrator(
            route_providers,
            max_concurrency=settings.route_max_concurrency,
            provider_timeout_seconds=settings.provider_timeout_seconds,
        ),
        classification_provider=build_classification_provider(settings),
        place_client=KakaoLocalClient(
            rest_key=settings.kakao_rest_api_key,
            max_response_bytes=1_000_000,
        ),
        cache=TtlLruCache(max_entries=2_000),
        rate_limiter=FixedWindowRateLimiter(
            limits={
                "places": (10, 60.0),
                "preview": (20, 60.0),
                "auth-start": (10, 60.0),
                "auth-callback": (20, 60.0),
            }
        ),
        seoul_geojson=seoul_geojson,
        support_geojson=support_geojson,
        user_services=user_services,
    )


def _optional_user_services(settings: Settings) -> UserServices | None:
    """Contain verified private-store failures at the optional login boundary."""

    if settings.user_database_path is None:
        return None
    try:
        return _build_user_services(settings)
    except (StorageIntegrityError, sqlite3.Error):
        return None


def _build_user_services(settings: Settings) -> UserServices:
    """Assemble private services only after the operator-provisioned schema verifies."""

    if (
        settings.user_database_path is None
        or settings.kakao_oidc_client_id is None
        or settings.kakao_oidc_client_secret is None
        or settings.session_hmac_key is None
        or settings.kakao_subject_hmac_key is None
        or settings.data_encryption_key_v1 is None
    ):
        raise StorageIntegrityError("storage configuration is unavailable")
    database = SqliteDatabase(Path(settings.user_database_path))
    database.verify_current_schema()
    session_hmac_key = _decode_settings_key(settings.session_hmac_key)
    subject_hmac_key = _decode_settings_key(settings.kakao_subject_hmac_key)
    data_encryption_key = _decode_settings_key(settings.data_encryption_key_v1)
    cipher = PayloadCipher(keys={1: data_encryption_key})
    return UserServices(
        oauth_attempts=OAuthAttemptRepository(database, hmac_key=session_hmac_key),
        sessions=SessionService(
            UserSessionRepository(database), hmac_key=session_hmac_key
        ),
        history=HistoryRepository(database, cipher, clock=lambda: datetime.now(UTC)),
        settings=UserSettingsRepository(database, cipher),
        retention_cleaner=RetentionCleaner(database, clock=lambda: datetime.now(UTC)),
        oidc_client=KakaoOidcClient(
            client_id=settings.kakao_oidc_client_id,
            client_secret=settings.kakao_oidc_client_secret,
            subject_hmac_key=subject_hmac_key,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
    )


def _decode_settings_key(value: SecretStr) -> bytes:
    """Decode a Settings-validated unpadded base64url key without logging it."""

    return b64decode(value.get_secret_value() + "=", altchars=b"-_", validate=True)


def _verified_geojson_bytes(data: bytes, label: str) -> bytes:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"verified geodata is unavailable: {label}") from exc
    if (
        type(payload) is not dict
        or payload.get("type") != "FeatureCollection"
        or type(payload.get("features")) is not list
        or not payload["features"]
        or any(type(feature) is not dict for feature in payload["features"])
    ):
        raise RuntimeError(f"verified geodata is invalid: {label}")
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
