import json
import math
from collections.abc import Callable

import pytest
from app.storage.crypto import (
    PayloadCipher,
    UserDataUnavailableError,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _cipher() -> PayloadCipher:
    return PayloadCipher(keys={1: bytes(range(32))})


# Break caught: a serializer change permits ambiguous/non-JSON user data.
def test_encrypt_json_round_trips_canonical_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_sizes: list[int] = []

    def fixed_nonce(size: int) -> bytes:
        requested_sizes.append(size)
        return b"n" * size

    monkeypatch.setattr("app.storage.crypto.os.urandom", fixed_nonce)
    cipher = _cipher()
    payload: dict[str, object] = {
        "z": None,
        "a": [True, False, 7, 1.5, {"k": "value"}],
    }

    encrypted = cipher.encrypt_json(
        purpose="user-settings", owner_id="42", payload=payload
    )

    assert encrypted.encryption_version == 1
    assert requested_sizes == [12]
    assert encrypted.ciphertext[:12] == b"n" * 12
    plaintext = AESGCM(bytes(range(32))).decrypt(
        encrypted.ciphertext[:12],
        encrypted.ciphertext[12:],
        b"travel-map:user-settings:v1:42",
    )
    assert plaintext == b'{"a":[true,false,7,1.5,{"k":"value"}],"z":null}'
    assert cipher.decrypt_json(
        purpose="user-settings",
        owner_id="42",
        ciphertext=encrypted.ciphertext,
        encryption_version=encrypted.encryption_version,
    ) == json.loads(plaintext)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"nan": math.nan},
        {"infinity": math.inf},
        {"negativeInfinity": -math.inf},
        {"tuple": ("unsupported",)},
        {"bytes": b"unsupported"},
        {1: "non-string key"},
        {"nested": {"set": {"unsupported"}}},
    ],
)
def test_encrypt_json_rejects_noncanonical_payloads(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _cipher().encrypt_json(
            purpose="history-input",
            owner_id="42:item",
            payload=payload,  # type: ignore[arg-type]
        )


# Break caught: AES-GCM nonce reuse exposes relations between user records.
def test_encrypt_json_uses_a_unique_nonce_for_equal_plaintext() -> None:
    cipher = _cipher()
    first = cipher.encrypt_json(
        purpose="history-summary", owner_id="42:item", payload={"total": 1}
    )
    second = cipher.encrypt_json(
        purpose="history-summary", owner_id="42:item", payload={"total": 1}
    )

    assert len(first.ciphertext[:12]) == 12
    assert len(second.ciphertext[:12]) == 12
    assert first.ciphertext[:12] != second.ciphertext[:12]
    assert first.ciphertext != second.ciphertext


def test_encrypt_json_preserves_boolean_without_accepting_int_subclasses() -> None:
    class IntSubclass(int):
        pass

    encrypted = _cipher().encrypt_json(
        purpose="user-settings", owner_id="42", payload={"enabled": True}
    )
    assert _cipher().decrypt_json(
        purpose="user-settings",
        owner_id="42",
        ciphertext=encrypted.ciphertext,
        encryption_version=encrypted.encryption_version,
    ) == {"enabled": True}
    with pytest.raises(ValueError) as raised:
        _cipher().encrypt_json(
            purpose="user-settings",
            owner_id="42",
            payload={"number": IntSubclass(1)},
        )
    assert str(raised.value) == "INVALID_ENCRYPTED_PAYLOAD"


def _direct_dict_cycle() -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["self"] = payload
    return payload


def _nested_list_cycle() -> dict[str, object]:
    values: list[object] = []
    values.append(values)
    return {"values": values}


def _indirect_dict_list_cycle() -> dict[str, object]:
    payload: dict[str, object] = {}
    values: list[object] = [payload]
    payload["values"] = values
    return payload


@pytest.mark.parametrize(
    "payload_factory",
    (_direct_dict_cycle, _nested_list_cycle, _indirect_dict_list_cycle),
)
def test_encrypt_json_rejects_cyclic_containers_with_a_fixed_safe_error(
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    with pytest.raises(ValueError) as raised:
        _cipher().encrypt_json(
            purpose="history-input",
            owner_id="42:item",
            payload=payload_factory(),
        )

    assert type(raised.value).__name__ == "InvalidEncryptedPayloadError"
    assert str(raised.value) == "INVALID_ENCRYPTED_PAYLOAD"
    assert "RecursionError" not in str(raised.value)


def test_encrypt_json_allows_reused_noncyclic_container_aliases() -> None:
    shared: dict[str, object] = {"value": 1}
    payload = {"first": shared, "second": shared}

    encrypted = _cipher().encrypt_json(
        purpose="user-settings", owner_id="42", payload=payload
    )

    assert _cipher().decrypt_json(
        purpose="user-settings",
        owner_id="42",
        ciphertext=encrypted.ciphertext,
        encryption_version=encrypted.encryption_version,
    ) == {"first": {"value": 1}, "second": {"value": 1}}


@pytest.mark.parametrize(
    ("purpose", "owner_id", "version", "mutate"),
    [
        ("history-input", "42:item", 1, False),
        ("history-summary", "7:item", 1, False),
        ("history-summary", "42:item", 2, False),
        ("history-summary", "42:item", 1, True),
    ],
)
def test_decrypt_rejects_wrong_owner_purpose_version_and_tampering(
    purpose: str, owner_id: str, version: int, mutate: bool
) -> None:
    encrypted = _cipher().encrypt_json(
        purpose="history-summary", owner_id="42:item", payload={"total": 1}
    )
    ciphertext = bytearray(encrypted.ciphertext)
    if mutate:
        ciphertext[-1] ^= 1

    with pytest.raises(UserDataUnavailableError) as raised:
        _cipher().decrypt_json(
            purpose=purpose,
            owner_id=owner_id,
            ciphertext=bytes(ciphertext),
            encryption_version=version,
        )

    assert str(raised.value) == "ENCRYPTED_PAYLOAD_INVALID"
    assert "InvalidTag" not in str(raised.value)
