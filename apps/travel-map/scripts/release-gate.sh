#!/bin/sh
set -eu

# This gate deliberately accepts no image, command, or host-path argument. It
# validates the reviewed local release context first and uses one local image.
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd -P)
cd "$repo_root"

blocked() {
    printf '%s\n' "$1" >&2
    exit 2
}

remove_private_directory() {
    directory=$1
    [ -n "$directory" ] || return 0
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
    find "$directory" -mindepth 1 -depth -delete >/dev/null 2>&1 && rmdir "$directory"
}

stat_owner_mode() {
    stat -c '%u:%a' "$1" 2>/dev/null || stat -f '%u:%Lp' "$1" 2>/dev/null
}

gate_parent=
gate_data=
context_parent=
gate_image=
gate_container=
image_built=0
record_written=0
gate_completed=0
interrupted=0
record_path_valid=0
host_uid=
host_gid=

run_chown_helper() {
    docker run --rm --user 0:0 --network none --read-only \
        --cap-drop ALL --cap-add CHOWN --cap-add FOWNER \
        --security-opt no-new-privileges \
        --mount "type=bind,src=$gate_data,dst=/data" \
        --entrypoint /bin/sh "$gate_image" \
        -eu -c "chown $1:$2 /data; chmod 0700 /data"
}

remove_unfinished_record() {
    [ "$record_path_valid" -eq 1 ] || return 0
    [ -n "$record_path" ] || return 1
    python3 - "$record_path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if path.exists() and not path.is_symlink() and path.is_file():
    path.unlink()
elif path.exists() or path.is_symlink():
    raise SystemExit(1)
PY
}

cleanup_gate_data() {
    [ -n "$gate_parent" ] || return 0
    [ -n "$gate_data" ] || return 1
    [ "$gate_data" = "$gate_parent/data" ] || return 1
    [ -d "$gate_parent" ] && [ ! -L "$gate_parent" ] || return 1
    [ -d "$gate_data" ] && [ ! -L "$gate_data" ] || return 1

    docker run --rm --user 10001:10001 --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges \
        --mount "type=bind,src=$gate_data,dst=/data" \
        --entrypoint /bin/sh "$gate_image" \
        -eu -c 'find /data -mindepth 1 -depth -delete'
    run_chown_helper "$host_uid" "$host_gid"
    rmdir "$gate_data" && rmdir "$gate_parent"
    gate_data=
    gate_parent=
}

cleanup() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ "$gate_completed" -ne 1 ] || [ "$interrupted" -eq 1 ]; then
        remove_unfinished_record >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$gate_container" ]; then
        docker rm -f "$gate_container" >/dev/null 2>&1 || cleanup_failed=1
        gate_container=
    fi
    if [ -n "$gate_parent" ]; then
        cleanup_gate_data >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$context_parent" ]; then
        remove_private_directory "$context_parent" || cleanup_failed=1
        context_parent=
    fi
    if { [ "$gate_completed" -ne 1 ] || [ "$interrupted" -eq 1 ] \
        || [ "$record_written" -ne 1 ]; } \
        && [ "$image_built" -eq 1 ]; then
        docker image rm "$gate_image" >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ "$cleanup_failed" -ne 0 ]; then
        printf '%s\n' 'BLOCKED_GATE_CLEANUP_FAILED' >&2
        status=2
    fi
    exit "$status"
}

interrupted_cleanup() {
    interrupted=1
    trap - HUP INT TERM
    exit 2
}

trap cleanup EXIT
trap interrupted_cleanup HUP INT TERM

case "${NAS_PLATFORM:-}" in
    linux/amd64|linux/arm64) ;;
    *) blocked 'BLOCKED_NAS_PLATFORM_UNVERIFIED' ;;
esac

if ! git diff --quiet --ignore-submodules -- \
    || ! git diff --cached --quiet --ignore-submodules -- \
    || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    blocked 'BLOCKED_DIRTY_RELEASE_SOURCE'
fi

git_sha=$(git rev-parse HEAD 2>/dev/null) || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
[ "${#git_sha}" -eq 40 ] || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
case "$git_sha" in
    *[!0-9a-f]*)
        blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
        ;;
esac
gate_image="seoul-education-travel-map:release-gate-$git_sha"

record_path=${RELEASE_GATE_IMAGE_RECORD:-}
if [ -n "$record_path" ]; then
    case "$record_path" in
        /*/gated-image.record) ;;
        *) blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH' ;;
    esac
    record_parent=${record_path%/gated-image.record}
    [ -n "$record_parent" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    [ ! -e "$record_path" ] && [ ! -L "$record_path" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    [ -d "$record_parent" ] && [ ! -L "$record_parent" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    record_parent_physical=$(CDPATH= cd -- "$record_parent" && pwd -P) || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    [ "$record_parent" = "$record_parent_physical" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    case "$(stat_owner_mode "$record_parent")" in
        "$(id -u):700") ;;
        *) blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH' ;;
    esac
    record_path_valid=1
fi

context_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-release.XXXXXX") || blocked 'BLOCKED_PRIVATE_DIRECTORY'
chmod 0700 "$context_parent" || blocked 'BLOCKED_PRIVATE_DIRECTORY'
context_root="$context_parent/context"

# Validate the recorded source, normalized geodata, hash-pinned rules, and
# current approved institution snapshot before Docker is used.
if ! snapshot_id=$(PYTHONWARNINGS=error uv run --project apps/travel-map python \
    apps/travel-map/scripts/prepare-release-context.py \
    --source apps/travel-map --destination "$context_root" 2>/dev/null); then
    blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
fi

if ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
    blocked 'BLOCKED_DOCKER_UNAVAILABLE'
fi

PYTHONWARNINGS=error uv sync --project apps/travel-map --frozen --dev
PYTHONWARNINGS=error uv run --project apps/travel-map pytest apps/travel-map/tests -q
PYTHONWARNINGS=error uv run --project apps/travel-map ruff check \
    apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
PYTHONWARNINGS=error uv run --project apps/travel-map ruff format --check \
    apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
PYTHONWARNINGS=error uv run --project apps/travel-map mypy \
    apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e

docker buildx build --platform "$NAS_PLATFORM" --build-arg SNAPSHOT_ID="$snapshot_id" \
    --load --tag "$gate_image" "$context_root"
image_built=1
image_id=$(docker image inspect --format '{{.Id}}' "$gate_image") || blocked 'BLOCKED_IMAGE_ATTESTATION'
image_digest=${image_id#sha256:}
[ "$image_digest" != "$image_id" ] && [ "${#image_digest}" -eq 64 ] \
    || blocked 'BLOCKED_IMAGE_ATTESTATION'
case "$image_digest" in *[!0-9a-f]*) blocked 'BLOCKED_IMAGE_ATTESTATION' ;; esac

gate_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-image-gate.XXXXXX") || blocked 'BLOCKED_PRIVATE_DIRECTORY'
chmod 0700 "$gate_parent" || blocked 'BLOCKED_PRIVATE_DIRECTORY'
gate_data=$gate_parent/data
(umask 077 && mkdir "$gate_data") || blocked 'BLOCKED_PRIVATE_DIRECTORY'
host_uid=$(id -u)
host_gid=$(id -g)
case "$host_uid:$host_gid" in
    *[!0-9:]*) blocked 'BLOCKED_INVALID_HOST_ID' ;;
esac
[ "$gate_data" = "$gate_parent/data" ] || blocked 'BLOCKED_UNSAFE_GATE_PATH'
case "$gate_data" in
    *,*) blocked 'BLOCKED_UNSAFE_GATE_PATH' ;;
esac

run_chown_helper 10001 10001 || blocked 'BLOCKED_PRIVATE_DIRECTORY'

run_isolated() {
    docker run --rm --user 10001:10001 --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
        --mount "type=bind,src=$gate_data,dst=/data" \
        "$gate_image" "$@"
}

run_isolated /bin/sh -eu -c \
    'umask 077; python -m app.storage.migrations migrate --database /data/travel-map.sqlite3; exec python -m app.storage.migrations verify --database /data/travel-map.sqlite3' \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_MIGRATION'

storage_sentinel=$(python3 -c 'import secrets; print("travel-map-image-gate-" + secrets.token_urlsafe(24))') \
    || blocked 'BLOCKED_PRIVATE_SENTINEL'
storage_smoke_output=$(docker run --rm --user 10001:10001 --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
    --mount "type=bind,src=$gate_data,dst=/data" \
    --env "STORAGE_SENTINEL=$storage_sentinel" \
    --entrypoint /bin/sh "$gate_image" -eu -c 'umask 077; exec python -' <<'PY'
import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.storage.crypto import PayloadCipher
from app.storage.database import SqliteDatabase
from app.storage.history import HistoryRepository
from app.storage.models import DEFAULT_USER_SETTINGS, HistoryRecalculationDraft, HistorySummary
from app.storage.user_settings import UserSettingsRepository
from app.storage.users import UserSessionRepository
from app.trips.models import TripPattern


async def smoke() -> None:
    sentinel = os.environ["STORAGE_SENTINEL"]
    now = datetime.now(UTC)
    database = SqliteDatabase(Path("/data/travel-map.sqlite3"))
    database.verify_current_schema()
    cipher = PayloadCipher(keys={1: os.urandom(32)})
    users = UserSessionRepository(database)
    user = await users.upsert_user_and_insert_session(
        subject_hmac=os.urandom(32),
        token_hmac=os.urandom(32),
        csrf_hmac=os.urandom(32),
        now=now,
        expires_at=now + timedelta(hours=1),
    )
    settings = UserSettingsRepository(database, cipher)
    settings_value = replace(DEFAULT_USER_SETTINGS, default_origin_site_id=sentinel)
    await settings.replace(user_id=user.id, settings=settings_value)
    assert await settings.get(user_id=user.id) == settings_value
    history = HistoryRepository(database, cipher, clock=lambda: now)
    metadata = await history.create(
        user_id=user.id,
        draft=HistoryRecalculationDraft(
            origin_site_id="image-gate-origin",
            origin_name=sentinel,
            destination_name=sentinel,
            destination_address=sentinel,
            trip_pattern=TripPattern.ROUND_TRIP,
            starts_at=now,
            ends_at=now + timedelta(minutes=60),
        ),
        summary=HistorySummary(
            classification="IMAGE_GATE",
            allowance_status="KNOWN",
            allowance_krw=0,
            route_legs=(),
            rule_set_id="image-gate-rule",
            effective_from="2026-01-01",
        ),
    )
    detail = await history.get(user_id=user.id, history_id=metadata.id)
    assert detail is not None
    assert detail.draft.destination_address == sentinel
    await database.checkpoint_truncate()


asyncio.run(smoke())
print("ENCRYPTED_STORAGE_SMOKE_OK")
PY
) || blocked 'BLOCKED_ENCRYPTED_STORAGE_SMOKE'
[ "$storage_smoke_output" = 'ENCRYPTED_STORAGE_SMOKE_OK' ] \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_SMOKE'

set -- $(python3 - <<'PY'
import secrets

for prefix in (
    "test-rest-",
    "test-transit-",
    "test-opinet-",
    "test-oidc-id-",
    "test-oidc-secret-",
    "",
    "",
    "",
):
    print(prefix + secrets.token_urlsafe(32))
PY
)
test_rest_key=$1
test_transit_key=$2
test_opinet_key=$3
test_oidc_id=$4
test_oidc_secret=$5
test_session_key=$6
test_subject_key=$7
test_data_key=$8

gate_container="travel-map-release-gate-$$"
docker run -d --name "$gate_container" --user 10001:10001 --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
    --mount "type=bind,src=$gate_data,dst=/data" \
    --env ENVIRONMENT=production \
    --env PUBLIC_BASE_URL=https://travel.h19h19.com \
    --env USER_DATABASE_PATH=/data/travel-map.sqlite3 \
    --env KAKAO_REST_API_KEY="$test_rest_key" \
    --env SEOUL_TRANSIT_SERVICE_KEY="$test_transit_key" \
    --env OPINET_CERT_KEY="$test_opinet_key" \
    --env KAKAO_OIDC_CLIENT_ID="$test_oidc_id" \
    --env KAKAO_OIDC_CLIENT_SECRET="$test_oidc_secret" \
    --env SESSION_HMAC_KEY="$test_session_key" \
    --env KAKAO_SUBJECT_HMAC_KEY="$test_subject_key" \
    --env DATA_ENCRYPTION_KEY_V1="$test_data_key" \
    --env TRUSTED_PROXY_CIDRS='["127.0.0.1/32"]' \
    --env ALLOWED_HOSTS='["travel.h19h19.com","127.0.0.1","localhost"]' \
    --env ALLOWED_ORIGINS='["https://travel.h19h19.com"]' \
    "$gate_image" >/dev/null || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'

docker exec -i "$gate_container" python - <<'PY' || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8080/healthz", timeout=5) as response:
    assert response.status == 200
PY
docker exec -i "$gate_container" python - <<'PY' || blocked 'BLOCKED_ENCRYPTED_STORAGE_MODE'
import os
import stat

for path in ("/data/travel-map.sqlite3", "/data/travel-map.sqlite3-wal", "/data/travel-map.sqlite3-shm"):
    if os.path.exists(path):
        details = os.stat(path)
        assert details.st_uid == 10001 and details.st_gid == 10001
        assert stat.S_IMODE(details.st_mode) == 0o600
PY

for value in "$storage_sentinel" "$test_rest_key" "$test_transit_key" \
    "$test_opinet_key" "$test_oidc_id" "$test_oidc_secret" "$test_session_key" \
    "$test_subject_key" "$test_data_key"; do
    if docker logs "$gate_container" 2>&1 | grep -aF -q -- "$value"; then
        blocked 'BLOCKED_PLAINTEXT_IN_STORAGE'
    fi
    for raw_file in "$gate_data/travel-map.sqlite3" \
        "$gate_data/travel-map.sqlite3-wal" "$gate_data/travel-map.sqlite3-shm"; do
        if [ -f "$raw_file" ] && grep -aF -q -- "$value" "$raw_file"; then
            blocked 'BLOCKED_PLAINTEXT_IN_STORAGE'
        fi
    done
done

docker rm -f "$gate_container" >/dev/null || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'
gate_container=

# A record is emitted only after every check and private-directory cleanup has
# succeeded. The record owner deliberately retains the reviewed image tag.
cleanup_gate_data || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
remove_private_directory "$context_parent" || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
context_parent=

if [ -n "$record_path" ]; then
    python3 - "$record_path" "$gate_image" "$image_id" "$NAS_PLATFORM" "$git_sha" <<'PY' \
        || blocked 'BLOCKED_IMAGE_ATTESTATION'
import os
import re
import sys
from pathlib import Path

record, image_tag, image_id, platform, git_sha = sys.argv[1:]
record_path = Path(record)
image_digest = image_id.removeprefix("sha256:")
if (
    record_path.name != "gated-image.record"
    or not record_path.is_absolute()
    or record_path.exists()
    or record_path.is_symlink()
    or not re.fullmatch(r"seoul-education-travel-map:release-gate-[0-9a-f]{40}", image_tag)
    or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
    or platform not in {"linux/amd64", "linux/arm64"}
    or not re.fullmatch(r"[0-9a-f]{40}", git_sha)
):
    raise SystemExit(2)
payload = (
    f"imageTag={image_tag}\n"
    f"imageId=sha256:{image_digest}\n"
    f"platform={platform}\n"
    f"gitSha={git_sha}\n"
).encode("ascii")
temporary = record_path.with_name(f".{record_path.name}.tmp-{os.getpid()}")
try:
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, record_path)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
    record_written=1
else
    docker image rm "$gate_image" >/dev/null || blocked 'BLOCKED_IMAGE_ATTESTATION'
    image_built=0
fi

printf '%s\n' 'ENCRYPTED_STORAGE_IMAGE_GATE_OK'
gate_completed=1
