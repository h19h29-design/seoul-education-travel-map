"""Authenticated encryption for the minimal user-data payloads."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16
_MAX_JSON_DEPTH: Final = 64
_ALLOWED_PURPOSES: Final = frozenset(
    {"history-input", "history-summary", "user-settings"}
)


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    encryption_version: int


class UserDataUnavailableError(RuntimeError):
    """Fixed public boundary for known storage/cipher availability failures."""

    def __init__(self) -> None:
        super().__init__("ENCRYPTED_PAYLOAD_INVALID")


class InvalidEncryptedPayloadError(ValueError):
    """Fixed, non-sensitive input boundary for values that cannot be encrypted."""

    def __init__(self) -> None:
        super().__init__("INVALID_ENCRYPTED_PAYLOAD")


class PayloadCipher:
    """AES-256-GCM envelopes with row- and purpose-bound associated data."""

    __slots__ = ("_active_version", "_keys")

    def __init__(self, *, keys: Mapping[int, bytes], active_version: int = 1) -> None:
        if type(active_version) is not int or active_version <= 0:
            raise ValueError("active encryption version must be a positive integer")

        validated_keys: dict[int, bytes] = {}
        for version, key in keys.items():
            if type(version) is not int or version <= 0:
                raise ValueError("encryption versions must be positive integers")
            if type(key) is not bytes or len(key) != 32:
                raise ValueError("encryption keys must be exactly 32 bytes")
            validated_keys[version] = key
        if active_version not in validated_keys:
            raise ValueError("active encryption version must have a key")

        self._active_version = active_version
        self._keys = MappingProxyType(validated_keys)

    def encrypt_json(
        self, *, purpose: str, owner_id: str, payload: dict[str, object]
    ) -> EncryptedPayload:
        if type(payload) is not dict:
            raise InvalidEncryptedPayloadError()
        _validate_payload(payload)
        plaintext = _canonical_json(payload)
        version = self._active_version
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = AESGCM(self._keys[version]).encrypt(
            nonce, plaintext, _associated_data(purpose, version, owner_id)
        )
        return EncryptedPayload(
            ciphertext=nonce + encrypted,
            encryption_version=version,
        )

    def decrypt_json(
        self,
        *,
        purpose: str,
        owner_id: str,
        ciphertext: bytes,
        encryption_version: int,
    ) -> dict[str, object]:
        try:
            if (
                type(encryption_version) is not int
                or type(ciphertext) is not bytes
                or len(ciphertext) < _NONCE_BYTES + _TAG_BYTES
            ):
                raise ValueError("invalid encrypted envelope")
            key = self._keys[encryption_version]
            nonce = ciphertext[:_NONCE_BYTES]
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext[_NONCE_BYTES:],
                _associated_data(purpose, encryption_version, owner_id),
            )
            decoded: object = json.loads(plaintext.decode("utf-8"))
            _validate_payload(decoded)
            if type(decoded) is not dict or _canonical_json(decoded) != plaintext:
                raise ValueError("encrypted JSON payload is noncanonical")
            return decoded
        except (
            InvalidTag,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            raise UserDataUnavailableError() from None


def _associated_data(purpose: str, version: int, owner_id: str) -> bytes:
    if (
        type(purpose) is not str
        or purpose not in _ALLOWED_PURPOSES
        or type(version) is not int
        or version <= 0
        or type(owner_id) is not str
        or not owner_id
    ):
        raise ValueError("invalid encrypted payload binding")
    return f"travel-map:{purpose}:v{version}:{owner_id}".encode()


def _validate_payload(
    value: object,
    *,
    active_container_ids: set[int] | None = None,
    depth: int = 0,
) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise InvalidEncryptedPayloadError()
    active = active_container_ids if active_container_ids is not None else set()
    if type(value) is dict:
        container_id = id(value)
        if container_id in active:
            raise InvalidEncryptedPayloadError()
        active.add(container_id)
        try:
            for key, nested_value in value.items():
                if type(key) is not str:
                    raise InvalidEncryptedPayloadError()
                _validate_payload(
                    nested_value,
                    active_container_ids=active,
                    depth=depth + 1,
                )
        finally:
            active.remove(container_id)
        return
    if type(value) is list:
        container_id = id(value)
        if container_id in active:
            raise InvalidEncryptedPayloadError()
        active.add(container_id)
        try:
            for nested_value in value:
                _validate_payload(
                    nested_value,
                    active_container_ids=active,
                    depth=depth + 1,
                )
        finally:
            active.remove(container_id)
        return
    if type(value) is float:
        if not isfinite(value):
            raise InvalidEncryptedPayloadError()
        return
    if type(value) in {str, int, bool, type(None)}:
        return
    raise InvalidEncryptedPayloadError()


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
