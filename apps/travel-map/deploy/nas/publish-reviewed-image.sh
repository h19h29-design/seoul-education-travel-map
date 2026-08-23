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

validate_private_directory() {
    /usr/bin/python3 -I -S - "$1" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    details = Path(sys.argv[1]).lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
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
tag_descriptor_initial=$record_parent/tag-descriptor-initial.json
tag_descriptor_final=$record_parent/tag-descriptor-final.json
immutable_descriptor=$record_parent/immutable-descriptor.json
tag_lookup_error=$record_parent/tag-lookup.err
root_manifest=$record_parent/root-manifest.json
publisher_uid=$(/usr/bin/id -u 2>/dev/null) \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
case "$publisher_uid" in
    ''|*[!0-9]*) blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY' ;;
esac
/usr/bin/python3 -I -S - "$publisher_uid" <<'PY' \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
import os
import sys

if sys.argv[1] != str(os.getuid()):
    raise SystemExit(2)
PY
lock_parent=/tmp/travel-map-publish-locks-$publisher_uid
lock_directory=$lock_parent/$git_sha
image_tag=
tagged=
owns_tagged=0
owns_lock=0
image_built=0
interrupted=0
publish_completed=0

remove_owned_tag() {
    current_tagged=$(docker image ls --quiet --no-trunc \
        --filter "reference=$tagged" 2>/dev/null) || return 1
    [ -n "$current_tagged" ] || return 0
    [ "$current_tagged" = "$image_id" ] || return 0
    docker image rm "$tagged" >/dev/null 2>&1
}

cleanup_publish() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ "$owns_tagged" -eq 1 ] && [ -n "$tagged" ]; then
        remove_owned_tag || cleanup_failed=1
        owns_tagged=0
    fi
    if [ -n "$image_tag" ]; then
        docker image rm "$image_tag" >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$record_parent" ]; then
        remove_private_directory "$record_parent" || cleanup_failed=1
    fi
    if [ "$owns_lock" -eq 1 ] && [ -n "$lock_directory" ]; then
        /bin/rmdir "$lock_directory" >/dev/null 2>&1 || cleanup_failed=1
        owns_lock=0
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

/bin/mkdir -m 0700 "$lock_parent" 2>/dev/null || true
validate_private_directory "$lock_parent" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
# Stale locks are never removed automatically. After verifying that no publisher
# is running for this Git SHA, an operator may remove this exact empty directory.
(umask 077 && /bin/mkdir "$lock_directory") 2>/dev/null \
    || blocked 'BLOCKED_PUBLISH_LOCKED'
owns_lock=1
validate_private_directory "$lock_directory" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'

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
exact_inspected=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_id") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$exact_inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'

image_id_hex=${image_id#sha256:}
publish_tag=$git_sha-sha256-$image_id_hex
[ "${#publish_tag}" -le 128 ] || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
tagged=$registry:$publish_tag
local_tagged=$(docker image ls --quiet --no-trunc --filter "reference=$tagged" 2>/dev/null) \
    || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
[ -z "$local_tagged" ] || blocked 'BLOCKED_PUBLISH_TAG_EXISTS'
inspected=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_tag") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'

parse_descriptor() {
    python3 - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ROOT_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
}

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError
    digest = value.get("digest")
    media_type = value.get("mediaType")
    size = value.get("size")
    if (
        type(digest) is not str
        or DIGEST.fullmatch(digest) is None
        or media_type not in ROOT_MEDIA_TYPES
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(digest, media_type, size)
PY
}

resolve_descriptor() {
    reference=$1
    destination=$2
    docker "$buildx_command" imagetools inspect --format '{{json .Manifest}}' \
        "$reference" > "$destination" \
        || return 1
    parse_descriptor "$destination"
}

lookup_remote_tag() {
    : > "$tag_descriptor_initial"
    : > "$tag_lookup_error"
    if docker "$buildx_command" imagetools inspect --format '{{json .Manifest}}' \
        "$tagged" > "$tag_descriptor_initial" 2> "$tag_lookup_error"; then
        descriptor_values=$(parse_descriptor "$tag_descriptor_initial") \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        remote_lookup_state=existing
        return 0
    fi
    python3 - "$tag_descriptor_initial" "$tag_lookup_error" "$tagged" <<'PY' \
        || blocked 'BLOCKED_REMOTE_TAG_UNVERIFIED'
import re
import sys
from pathlib import Path

try:
    payload = b"".join(Path(path).read_bytes() for path in sys.argv[1:3])
    if not payload or len(payload) > 65_536:
        raise ValueError
    message = payload.decode("utf-8", errors="strict").strip().lower()
    missing_manifest = re.fullmatch(
        r"(?:error:\s*)?manifest\s+unknown"
        r"(?::\s*(?:manifest\s+unknown|not\s+found))?",
        message,
    )
    missing_exact_tag = re.fullmatch(
        r"(?:error:\s*)?" + re.escape(sys.argv[3].lower()) + r":\s*not\s+found",
        message,
    )
    ambiguous_markers = (
        "unauthorized",
        "denied",
        "timeout",
        "connection",
        "certificate",
        "credential",
        "executable",
        "helper",
        "tls",
        "network",
        "rate limit",
        "too many requests",
        "unexpected status",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    )
    if (missing_manifest is None and missing_exact_tag is None) or any(
        marker in message for marker in ambiguous_markers
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
    remote_lookup_state=missing
}

lookup_remote_tag
if [ "$remote_lookup_state" = missing ]; then
    docker tag "$image_id" "$tagged" || blocked 'BLOCKED_IMAGE_TAGGING'
    owns_tagged=1
    tagged_inspected=$(docker image inspect \
        --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$tagged") \
        || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
    [ "$tagged_inspected" = "$image_id $nas_platform" ] \
        || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
    lookup_remote_tag
    if [ "$remote_lookup_state" = missing ]; then
        docker push "$tagged" >/dev/null || blocked 'BLOCKED_IMAGE_PUSH'
        lookup_remote_tag
        [ "$remote_lookup_state" = existing ] \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    fi
fi

set -- $descriptor_values
[ "$#" -eq 3 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
remote_digest=$1
remote_media_type=$2
remote_size=$3
repo_digest=$registry@$remote_digest

docker "$buildx_command" imagetools inspect --raw "$repo_digest" > "$root_manifest" \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

root_values=$(python3 - \
    "$root_manifest" "$image_id" "$nas_platform" "$remote_digest" "$remote_media_type" <<'PY'
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
MANIFEST_MEDIA_TYPES = {OCI_MANIFEST, DOCKER_MANIFEST}
CONFIG_MEDIA_TYPES = {OCI_CONFIG, DOCKER_CONFIG}


def is_digest(value: object) -> bool:
    return type(value) is str and DIGEST.fullmatch(value) is not None


def validate_descriptor(
    value: object,
    *,
    media_types=None,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError
    digest = value.get("digest")
    media_type = value.get("mediaType")
    size = value.get("size")
    if (
        not is_digest(digest)
        or type(media_type) is not str
        or (media_types is not None and media_type not in media_types)
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError
    return value


def validate_layers(value: object) -> None:
    if type(value) is not list or not value:
        raise ValueError
    for layer in value:
        validate_descriptor(layer)


try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    image_id = sys.argv[2]
    platform = sys.argv[3]
    remote_digest = sys.argv[4]
    remote_media_type = sys.argv[5]
    if type(value) is not dict or value.get("schemaVersion") != 2:
        raise ValueError
    if value.get("mediaType") != remote_media_type:
        raise ValueError

    if remote_media_type in MANIFEST_MEDIA_TYPES:
        config = validate_descriptor(
            value.get("config"),
            media_types=CONFIG_MEDIA_TYPES,
        )
        validate_layers(value.get("layers"))
        if config["digest"] != image_id:
            raise ValueError
        print("classic")
    elif remote_media_type == OCI_INDEX:
        if remote_digest != image_id:
            raise ValueError
        manifests = value.get("manifests")
        if type(manifests) is not list or not manifests:
            raise ValueError
        runnable: list[str] = []
        attestations: list[tuple[str, str]] = []
        seen: set[str] = set()
        for candidate in manifests:
            descriptor = validate_descriptor(
                candidate,
                media_types={OCI_MANIFEST},
            )
            digest = descriptor["digest"]
            if digest in seen:
                raise ValueError
            seen.add(digest)
            descriptor_platform = descriptor.get("platform")
            if (
                type(descriptor_platform) is not dict
                or set(descriptor_platform) != {"os", "architecture"}
            ):
                raise ValueError
            os_name = descriptor_platform.get("os")
            architecture = descriptor_platform.get("architecture")
            annotations = descriptor.get("annotations", {})
            if type(annotations) is not dict or any(
                type(key) is not str or type(annotation) is not str
                for key, annotation in annotations.items()
            ):
                raise ValueError
            if os_name == "unknown" or architecture == "unknown":
                if os_name != "unknown" or architecture != "unknown":
                    raise ValueError
                reference = annotations.get("vnd.docker.reference.digest")
                if (
                    annotations.get("vnd.docker.reference.type")
                    != "attestation-manifest"
                    or not is_digest(reference)
                ):
                    raise ValueError
                attestations.append((digest, reference))
            else:
                if (
                    f"{os_name}/{architecture}" != platform
                    or "vnd.docker.reference.digest" in annotations
                    or "vnd.docker.reference.type" in annotations
                ):
                    raise ValueError
                runnable.append(digest)
        if len(runnable) != 1:
            raise ValueError
        runnable_digest = runnable[0]
        if any(reference != runnable_digest for _, reference in attestations):
            raise ValueError
        print("index", runnable_digest, *(digest for digest, _ in attestations))
    else:
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
) || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

validate_child_manifest() {
    child_path=$1
    child_role=$2
    python3 - "$child_path" "$child_role" <<'PY'
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
IN_TOTO_LAYER = "application/vnd.in-toto+json"


def validate_descriptor(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError
    if (
        type(value.get("digest")) is not str
        or DIGEST.fullmatch(value["digest"]) is None
        or type(value.get("mediaType")) is not str
        or type(value.get("size")) is not int
        or value["size"] <= 0
    ):
        raise ValueError
    return value


try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    role = sys.argv[2]
    if (
        type(value) is not dict
        or value.get("schemaVersion") != 2
        or value.get("mediaType") != OCI_MANIFEST
    ):
        raise ValueError
    config = validate_descriptor(value.get("config"))
    if config["mediaType"] != OCI_CONFIG:
        raise ValueError
    layers = value.get("layers")
    if type(layers) is not list or not layers:
        raise ValueError
    media_types = [validate_descriptor(layer)["mediaType"] for layer in layers]
    if role == "runnable":
        if IN_TOTO_LAYER in media_types:
            raise ValueError
    elif role == "attestation":
        if any(media_type != IN_TOTO_LAYER for media_type in media_types):
            raise ValueError
    else:
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
}

set -- $root_values
[ "$#" -ge 1 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
root_mode=$1
shift
case "$root_mode" in
    classic)
        [ "$#" -eq 0 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        ;;
    index)
        [ "$#" -ge 1 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        runnable_digest=$1
        shift
        runnable_manifest=$record_parent/runnable-manifest.json
        runnable_image_config=$record_parent/runnable-image-config.json
        docker "$buildx_command" imagetools inspect \
            --raw "$registry@$runnable_digest" > "$runnable_manifest" \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        validate_child_manifest "$runnable_manifest" runnable \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        docker "$buildx_command" imagetools inspect --format '{{json .Image}}' \
            "$registry@$runnable_digest" > "$runnable_image_config" \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        python3 - "$runnable_image_config" "$nas_platform" <<'PY' \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError
    os_name = value.get("os")
    architecture = value.get("architecture")
    if (
        type(os_name) is not str
        or type(architecture) is not str
        or f"{os_name}/{architecture}" != sys.argv[2]
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
        attestation_number=0
        for attestation_digest in "$@"; do
            attestation_number=$((attestation_number + 1))
            attestation_manifest=$record_parent/attestation-manifest-$attestation_number.json
            docker "$buildx_command" imagetools inspect \
                --raw "$registry@$attestation_digest" > "$attestation_manifest" \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            validate_child_manifest "$attestation_manifest" attestation \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        done
        ;;
    *)
        blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        ;;
esac

immutable_values=$(resolve_descriptor "$repo_digest" "$immutable_descriptor") \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
[ "$immutable_values" = "$remote_digest $remote_media_type $remote_size" ] \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
final_tag_values=$(resolve_descriptor "$tagged" "$tag_descriptor_final") \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
[ "$final_tag_values" = "$remote_digest $remote_media_type $remote_size" ] \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

printf '%s\n' "$repo_digest"
publish_completed=1
