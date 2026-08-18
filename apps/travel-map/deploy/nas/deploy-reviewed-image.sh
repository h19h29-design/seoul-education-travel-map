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
image_env=$base/image.env
previous_env=$base/previous-image.env

[ -d "$base" ] && [ "$(CDPATH= cd -- "$base" && pwd -P)" = "$base" ] \
    || blocked 'BLOCKED_INVALID_DEPLOY_DIRECTORY'
[ -f "$compose" ] && [ ! -L "$compose" ] \
    || blocked 'BLOCKED_INVALID_DEPLOY_ASSET'
[ -f "$migration" ] && [ ! -L "$migration" ] && [ -x "$migration" ] \
    || blocked 'BLOCKED_INVALID_DEPLOY_ASSET'

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

docker pull "$reference" >/dev/null || blocked 'BLOCKED_IMAGE_PULL'
nas_arch=$(docker info --format '{{.Architecture}}') || blocked 'BLOCKED_NAS_PLATFORM_UNVERIFIED'
case "$nas_arch" in
    amd64) nas_platform=linux/amd64 ;;
    arm64) nas_platform=linux/arm64 ;;
    *) blocked 'BLOCKED_NAS_PLATFORM_UNVERIFIED' ;;
esac
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

"$migration" "$reference"
printf 'TRAVEL_MAP_MANIFEST_DIGEST=%s\n' "$digest" > "$image_tmp" \
    || blocked 'BLOCKED_IMAGE_ENV_WRITE'
validate_image_env "$image_tmp"
mv -f "$image_tmp" "$image_env" || blocked 'BLOCKED_IMAGE_ENV_WRITE'
image_tmp=
trap - EXIT HUP INT TERM
docker compose --env-file "$image_env" -f "$compose" up -d \
    || blocked 'BLOCKED_COMPOSE_START'
printf '%s\n' 'DEPLOYED_REVIEWED_IMAGE'
