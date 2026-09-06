#!/bin/sh
set -eu

blocked() {
    printf '%s\n' "$1" >&2
    exit 2
}

stat_mode() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

remove_private_file() {
    path=$1
    [ -n "$path" ] || return 0
    python3 - "$path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if path.exists() and not path.is_symlink() and path.is_file():
    path.unlink()
elif path.exists() or path.is_symlink():
    raise SystemExit(1)
PY
}

[ "$#" -eq 1 ] || {
    printf '%s\n' 'usage: deploy-reviewed-image.sh IMMUTABLE_GHCR_REFERENCE' >&2
    exit 64
}

reference=$1
registry=ghcr.io/h19h29-design/seoul-education-travel-map
reference_prefix=ghcr.io/h19h29-design/seoul-education-travel-map@sha256:
digest=${reference#"$reference_prefix"}
[ "$reference" = "$reference_prefix$digest" ] && [ "${#digest}" -eq 64 ] \
    || blocked 'BLOCKED_INVALID_IMAGE_REFERENCE'
case "$digest" in *[!0-9a-f]*) blocked 'BLOCKED_INVALID_IMAGE_REFERENCE' ;; esac

base=/volume1/docker/seoul-education-travel-map
compose=$base/compose.yml
migration=$base/migrate-user-database.sh
runtime_env=$base/runtime.env
image_env=$base/image.env
previous_env=$base/previous-image.env

[ -d "$base" ] && [ "$(CDPATH= cd -- "$base" && pwd -P)" = "$base" ] \
    || blocked 'BLOCKED_INVALID_DEPLOY_DIRECTORY'
[ -f "$compose" ] && [ ! -L "$compose" ] \
    || blocked 'BLOCKED_INVALID_DEPLOY_ASSET'

stateless_beta=0
if [ -e "$runtime_env" ] || [ -L "$runtime_env" ]; then
    [ -f "$runtime_env" ] && [ ! -L "$runtime_env" ] \
        || blocked 'BLOCKED_INVALID_DEPLOY_ASSET'
    [ "$(stat_mode "$runtime_env")" = 600 ] \
        || blocked 'BLOCKED_INVALID_DEPLOY_ASSET'
    mode_count=$(grep -c '^STATELESS_BETA=' "$runtime_env" || true)
    if [ "$mode_count" -gt 1 ]; then
        blocked 'BLOCKED_INVALID_RUNTIME_MODE'
    fi
    if [ "$mode_count" -eq 1 ]; then
        mode_value=$(sed -n 's/^STATELESS_BETA=//p' "$runtime_env")
        [ "$mode_value" = 1 ] || blocked 'BLOCKED_INVALID_RUNTIME_MODE'
        stateless_beta=1
    fi
fi

if [ "$stateless_beta" -eq 1 ]; then
    if grep -Fq '/volume2/docker-1/seoul-education-travel-map/data' "$compose" \
        || grep -Eq ':[[:space:]]*/data:rw([[:space:]]|$)' "$compose"; then
        blocked 'BLOCKED_STATELESS_DATA_MOUNT'
    fi
else
    [ -f "$migration" ] && [ ! -L "$migration" ] && [ -x "$migration" ] \
        || blocked 'BLOCKED_INVALID_DEPLOY_ASSET'
fi

validate_image_env() {
    path=$1
    [ -f "$path" ] && [ ! -L "$path" ] && [ "$(stat_mode "$path")" = 600 ] \
        || blocked 'BLOCKED_INVALID_IMAGE_ENV'
    [ "$(wc -l < "$path" | tr -d ' ')" -eq 1 ] \
        || blocked 'BLOCKED_INVALID_IMAGE_ENV'
    line=$(cat "$path")
    value=${line#TRAVEL_MAP_MANIFEST_DIGEST=}
    [ "$line" = "TRAVEL_MAP_MANIFEST_DIGEST=$value" ] && [ "${#value}" -eq 64 ] \
        || blocked 'BLOCKED_INVALID_IMAGE_ENV'
    case "$value" in *[!0-9a-f]*) blocked 'BLOCKED_INVALID_IMAGE_ENV' ;; esac
}

validate_image_env "$image_env"

architecture_sentinel=$(printf '\001')
nas_arch=$(
    if docker info --format '{{.Architecture}}'; then
        docker_status=0
    else
        docker_status=$?
    fi
    printf '%s' "$architecture_sentinel"
    exit "$docker_status"
) || blocked 'BLOCKED_NAS_PLATFORM_UNVERIFIED'
case "$nas_arch" in
    "amd64$architecture_sentinel"|"amd64
$architecture_sentinel"|"x86_64$architecture_sentinel"|"x86_64
$architecture_sentinel") nas_platform=linux/amd64 ;;
    "arm64$architecture_sentinel"|"arm64
$architecture_sentinel"|"aarch64$architecture_sentinel"|"aarch64
$architecture_sentinel") nas_platform=linux/arm64 ;;
    *) blocked 'BLOCKED_NAS_PLATFORM_UNVERIFIED' ;;
esac
docker pull "$reference" >/dev/null || blocked 'BLOCKED_IMAGE_PULL'
actual_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$reference") \
    || blocked 'BLOCKED_IMAGE_PLATFORM_MISMATCH'
[ "$actual_platform" = "$nas_platform" ] \
    || blocked 'BLOCKED_IMAGE_PLATFORM_MISMATCH'

previous_tmp=$(mktemp "$base/.previous-image.env.XXXXXX") \
    || blocked 'BLOCKED_ROLLBACK_COPY'
image_tmp=$(mktemp "$base/.image.env.XXXXXX") || {
    remove_private_file "$previous_tmp" >/dev/null 2>&1 || :
    blocked 'BLOCKED_IMAGE_ENV_WRITE'
}
interrupted=0
cleanup_tmp() {
    status=$?
    trap - EXIT HUP INT TERM
    remove_private_file "$previous_tmp" >/dev/null 2>&1 || status=2
    remove_private_file "$image_tmp" >/dev/null 2>&1 || status=2
    [ "$interrupted" -eq 1 ] && status=2
    exit "$status"
}
interrupted_cleanup() {
    interrupted=1
    trap - HUP INT TERM
    exit 2
}
trap cleanup_tmp EXIT
trap interrupted_cleanup HUP INT TERM
chmod 0600 "$previous_tmp" "$image_tmp" || blocked 'BLOCKED_IMAGE_ENV_WRITE'
cp -p "$image_env" "$previous_tmp" || blocked 'BLOCKED_ROLLBACK_COPY'
chmod 0600 "$previous_tmp" || blocked 'BLOCKED_ROLLBACK_COPY'
validate_image_env "$previous_tmp"
mv -f "$previous_tmp" "$previous_env" || blocked 'BLOCKED_ROLLBACK_COPY'
previous_tmp=
validate_image_env "$previous_env"

if [ "$stateless_beta" -eq 0 ]; then
    "$migration" "$reference"
fi
printf 'TRAVEL_MAP_MANIFEST_DIGEST=%s\n' "$digest" > "$image_tmp" \
    || blocked 'BLOCKED_IMAGE_ENV_WRITE'
validate_image_env "$image_tmp"
mv -f "$image_tmp" "$image_env" || blocked 'BLOCKED_IMAGE_ENV_WRITE'
image_tmp=
trap - EXIT HUP INT TERM
docker compose --env-file "$image_env" -f "$compose" up -d \
    || blocked 'BLOCKED_COMPOSE_START'
printf '%s\n' 'DEPLOYED_REVIEWED_IMAGE'
