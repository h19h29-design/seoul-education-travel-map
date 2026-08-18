#!/bin/sh
set -eu

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

[ "$#" -eq 2 ] || {
    printf '%s\n' 'usage: publish-reviewed-image.sh GIT_SHA NAS_PLATFORM' >&2
    exit 64
}

git_sha=$1
nas_platform=$2
registry=ghcr.io/h19h29-design/seoul-education-travel-map
buildx_command=buildx

[ "${#git_sha}" -eq 40 ] || blocked 'BLOCKED_INVALID_PUBLISH_INPUT'
case "$git_sha" in *[!0-9a-f]*) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;; esac
case "$nas_platform" in
    linux/amd64|linux/arm64) ;;
    *) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) \
    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
travel_root=$(CDPATH= cd -- "$script_dir/../.." && pwd -P) \
    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
[ -x "$travel_root/scripts/release-gate.sh" ] \
    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'

record_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-publish.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
chmod 0700 "$record_parent" || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
record=$record_parent/gated-image.record
manifest=$record_parent/manifest.json
image_tag=
tagged=
image_built=0
interrupted=0
publish_completed=0

cleanup_publish() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ -n "$tagged" ]; then
        docker image rm "$tagged" >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$image_tag" ]; then
        docker image rm "$image_tag" >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$record_parent" ]; then
        remove_private_directory "$record_parent" || cleanup_failed=1
    fi
    if [ "$interrupted" -eq 1 ] || [ "$cleanup_failed" -ne 0 ]; then
        printf '%s\n' 'BLOCKED_PUBLISH_CLEANUP_FAILED' >&2
        status=2
    fi
    exit "$status"
}

interrupted_cleanup() {
    interrupted=1
    trap - HUP INT TERM
    exit 2
}

trap cleanup_publish EXIT
trap interrupted_cleanup HUP INT TERM

NAS_PLATFORM=$nas_platform RELEASE_GATE_IMAGE_RECORD=$record \
    "$travel_root/scripts/release-gate.sh" >&2

set -- $(python3 - "$record" "$git_sha" "$nas_platform" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

record_path = Path(sys.argv[1])
git_sha = sys.argv[2]
platform = sys.argv[3]
try:
    details = record_path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_uid != os.getuid()
    ):
        raise ValueError
    lines = record_path.read_text(encoding="ascii").splitlines(keepends=True)
    if len(lines) != 4 or any(not line.endswith("\n") for line in lines):
        raise ValueError
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line[:-1].partition("=")
        if not separator or key in values:
            raise ValueError
        values[key] = value
    if set(values) != {"imageTag", "imageId", "platform", "gitSha"}:
        raise ValueError
    image_tag = values["imageTag"]
    image_id = values["imageId"]
    if (
        not re.fullmatch(
            r"seoul-education-travel-map:release-gate-[0-9a-f]{40}", image_tag
        )
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or values["platform"] != platform
        or values["gitSha"] != git_sha
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
print(image_tag)
print(image_id)
PY
) || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
image_tag=$1
image_id=$2

inspected=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_tag") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'

tagged=$registry:$git_sha
docker tag "$image_tag" "$tagged" || blocked 'BLOCKED_IMAGE_TAGGING'
docker push "$tagged" >/dev/null || blocked 'BLOCKED_IMAGE_PUSH'

repo_digest=$(docker image inspect \
    --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tagged" \
    | awk -v prefix="$registry@sha256:" 'index($0,prefix)==1 {print; exit}')
manifest_hex=${repo_digest#"$registry@sha256:"}
[ "$repo_digest" = "$registry@sha256:$manifest_hex" ] \
    && [ "${#manifest_hex}" -eq 64 ] \
    || blocked 'BLOCKED_INVALID_REMOTE_MANIFEST'
case "$manifest_hex" in *[!0-9a-f]*) blocked 'BLOCKED_INVALID_REMOTE_MANIFEST' ;; esac

docker "$buildx_command" imagetools inspect --raw "$repo_digest" > "$manifest" \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
remote_config=$(python3 - "$manifest" <<'PY'
import json
from pathlib import Path
import re
import sys

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    digest = value.get("config", {}).get("digest")
    if type(digest) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(digest)
PY
) || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
[ "$remote_config" = "$image_id" ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
docker "$buildx_command" imagetools inspect "$repo_digest" >/dev/null \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

printf '%s\n' "$repo_digest"
publish_completed=1
