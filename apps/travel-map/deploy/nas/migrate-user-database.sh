#!/bin/sh
set -eu

blocked() {
  printf '%s\n' "$1" >&2
  exit 2
}

[ "$#" -eq 1 ] || {
  printf '%s\n' 'usage: migrate-user-database.sh IMMUTABLE_GHCR_REFERENCE' >&2
  exit 64
}

image=$1
data_dir=/volume2/docker-1/seoul-education-travel-map/data

case "$image" in
  -*|*@sha256:*@sha256:*|*' '*|*'	'*|*'
'*) blocked BLOCKED_INVALID_IMAGE_DIGEST ;;
esac

prefix=${image%@sha256:*}
digest=${image##*@sha256:}
case "$prefix" in
  ""|*[!A-Za-z0-9./:_-]*) blocked BLOCKED_INVALID_IMAGE_DIGEST ;;
esac
[ "$image" = "$prefix@sha256:$digest" ] && [ "${#digest}" -eq 64 ] \
  || blocked BLOCKED_INVALID_IMAGE_DIGEST
case "$digest" in
  *[!0-9a-f]*) blocked BLOCKED_INVALID_IMAGE_DIGEST ;;
esac
[ "$prefix" = "ghcr.io/h19h29-design/seoul-education-travel-map" ] \
  || blocked BLOCKED_INVALID_IMAGE_REPOSITORY

[ -d "$data_dir" ] || blocked BLOCKED_UNEXPECTED_DATA_DIRECTORY
physical_data_dir=$(CDPATH= cd -- "$data_dir" && pwd -P) \
  || blocked BLOCKED_UNEXPECTED_DATA_DIRECTORY
[ "$physical_data_dir" = "$data_dir" ] || blocked BLOCKED_UNEXPECTED_DATA_DIRECTORY
owner_mode=$(stat -c '%u:%g:%a' "$data_dir" 2>/dev/null \
  || stat -f '%u:%g:%Lp' "$data_dir" 2>/dev/null \
  || true)
[ "$owner_mode" = "10001:10001:700" ] || blocked BLOCKED_UNSAFE_DATA_DIRECTORY_MODE

docker run --rm --user 10001:10001 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
  --mount "type=bind,src=$data_dir,dst=/data" "$image" \
  /bin/sh -eu -c 'umask 077
    python -m app.storage.migrations migrate --database /data/travel-map.sqlite3
    exec python -m app.storage.migrations verify --database /data/travel-map.sqlite3'
