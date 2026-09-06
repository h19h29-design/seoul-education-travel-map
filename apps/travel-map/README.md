# Seoul education travel map

This public no-login map previews routes and policy calculations. It is not an authorization or payment system. Public deployment is blocked until an approved live institution snapshot exists and manual release review is complete. Never promote test fixtures or synthetic institutions to `resources/institution-snapshots`.

## Local setup and offline checks

Run from the repository root:

```sh
uv sync --project apps/travel-map --frozen --dev
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
```

Copy the template only for local development. Never commit the result or put credentials in shell history, screenshots, issues, or logs.

```sh
cp apps/travel-map/.env.example apps/travel-map/.env
uv run --project apps/travel-map uvicorn app.main:app \
  --env-file apps/travel-map/.env --host 127.0.0.1 --port 8080
```

The application process and operator scripts are intentionally run from the
repository root with an explicit `--env-file apps/travel-map/.env`. The file is
local-only; it never overrides environment variables supplied by the deployment
platform, and neither script prints its values. Production uses the platform
secret manager rather than this file.

Register the exact public HTTPS app domain in Kakao Developers and restrict `KAKAO_JAVASCRIPT_KEY` to that domain. It is browser-only. `KAKAO_REST_API_KEY` is server-only and must never be sent to a browser or used as the JavaScript key. Store these server-side values in the deployment secret manager:

- `KAKAO_REST_API_KEY` for place and Kakao route calls.
- `SEOUL_TRANSIT_SERVICE_KEY` for Seoul transit routing.
- `OPINET_CERT_KEY` for fuel-price lookups.
- `NEIS_API_KEY` and `KINDERGARTEN_API_KEY` only for institution synchronization.

Production also requires explicit canonical HTTPS `ALLOWED_ORIGINS` and exact `ALLOWED_HOSTS`. The process fails before serving when they or runtime route credentials are incomplete.

## Provider extension points

Stage A fixes the registry order as Seoul Transit, Kakao Transit, Kakao Car,
and Kakao Walk. The follow-on plans are
[`docs/superpowers/plans/2026-08-10-seoul-public-road-routing-engine.md`](../../docs/superpowers/plans/2026-08-10-seoul-public-road-routing-engine.md)
and
[`docs/superpowers/plans/2026-08-10-seoul-public-walk-routing-engine.md`](../../docs/superpowers/plans/2026-08-10-seoul-public-walk-routing-engine.md).

Stage B changes only `build_car_provider_chain()` and retains the WALK-chain
regression test. Stage C changes only `build_walk_provider_chain()` and retains
the CAR-chain regression test. A public provider must not be promoted ahead of
Kakao as primary until it passes gold-route validation, missing-data detection,
and performance and outage fallback verification.

## Institution snapshot synchronization

Production accepts only the normalized snapshot selected by `resources/institution-snapshots/current.json`. Its pointer, approval metadata, hashes, and row schemas are validated at image build and startup. Never copy from `tests/fixtures` to this directory.

The snapshot workflow below is administrator-only. The public map does not
expose review or approval controls.

```sh
# 1. Networked, credentialed: creates .<id>.candidate only.
uv run --project apps/travel-map python apps/travel-map/scripts/sync-institutions.py \
  --env-file /secure/path/travel-map-sync.env

# 2. Credential-free: inspect source counts, observation-date histograms,
#    quarantine IDs, coordinate quality, provenance hashes, and diff.
uv run --project apps/travel-map python \
  apps/travel-map/scripts/review-institution-snapshot.py \
  --snapshot-id '<candidate-id>'

# 3. After a data steward independently records the review, publish exactly
#    the inspected digest. This is the only command that can update current.json.
uv run --project apps/travel-map python \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  --snapshot-id '<candidate-id>' --review-digest '<64-lowercase-hex>' \
  --reviewer-role data-steward
```

The expected NEIS observation-date distribution is `1413/1/1` until official
source dates converge. This histogram is review provenance: it is neither an
automatic rejection nor permission to normalize or collapse distinct dates.
Release remains blocked until step 3 publishes the independently reviewed
digest. A missing or invalid approved snapshot is a release blocker, never
permission to substitute a sample catalog.

Coordinate recovery remains fail-closed. The geocoder treats only the leading
`서울특별시`, `서울시`, and `서울` tokens as equivalent; the district, road name,
building number, and every remaining token must match exactly, and exactly one
Kakao road-address result must remain. It does not issue fallback or keyword
requests and does not lower the 98% quality gate.

Completing offline tests does not authorize another live sync. Obtain explicit
approval for one candidate-only run, inspect only aggregate coordinate-quality
and provenance counts, then use the separate review and approval commands. Never
approve a candidate that still reports a coordinate-quality issue.

### Temporary school-count variance review (administrators only)

The temporary population profile exists because the official preliminary
school-count table and the live source disclosures have different observation
dates and populations. It makes those differences explicit and reviewable; it
must not be used to silently redefine the official benchmark. The pinned source
contract is 1,415 NEIS rows fetched, 1,414 NEIS rows normalized, and 706
kindergarten rows for disclosure timing `20261` as of `2026-04-01`.

The six reviewed comparisons below use signed `actual - expected` differences:

| Category | Official expected | Profile actual | Signed difference |
| --- | ---: | ---: | ---: |
| Elementary school | 609 | 610 | +1 |
| Middle school | 390 | 390 | 0 |
| High school | 319 | 319 | 0 |
| Special school | 32 | 32 | 0 |
| Miscellaneous school | 18 | 22 | +4 |
| Kindergarten | 724 | 706 | -18 |

Broadcast middle/high schools and foreign schools remain in the normalized
catalog as supplementary populations, but they are not added to the benchmark
actuals above. The 18 lifelong-school rows remain quarantined pending official
classification. The single joint workshop row is nonselectable and excluded
from the normalized NEIS population.

For every candidate, run the sync, inspect the emitted
`PRE_PROMOTION_RECONCILIATION`, generate and inspect the credential-free review
packet, and only then pass that exact packet digest to the separate approval
command as a `data-steward`. Synchronization itself never approves or updates
`current.json`. Do not change the population profile unless there is new
official evidence, a design review, passing tests, and explicit `data-steward`
approval. General-user instructions and public UI copy must not expose internal
population labels, quarantined identifiers, provenance hashes, or credentials;
these details belong only in the administrator review workflow.

### NEIS lifelong-school quarantine review

The sync command loads the reviewed NEIS quarantine policy from
`resources/institution-sources/neis-unclassified-school-kinds.csv`. Its current
total is 18 and it contains exactly these labels and counts:

- `평생학교(고)-2년6학기`: 7
- `평생학교(고)-3년6학기`: 4
- `평생학교(중)-2년6학기`: 5
- `평생학교(초)-3년6학기`: 2

These entries must remain `UNCLASSIFIED_SCHOOL` with `REVIEW_REQUIRED` status;
they are quarantine records, not selectable schools. Before copying the review
digest in step 3, inspect the pre-promotion audit's
`reconciliation.unclassifiedSchoolKindCounts` and confirm it matches the four
labels above. A new label or any count drift fails closed: stop the workflow,
do not approve the candidate, and investigate the official source. When
official classification or revised statistics become available, make a new
reviewed policy change before resuming synchronization.

## Live smoke and manual approval

The live smoke runs exactly three bounded cases only after opt-in, a valid approved snapshot, and all runtime provider credentials:

```sh
TRAVEL_MAP_LIVE_SMOKE=1 uv run --project apps/travel-map python \
  apps/travel-map/scripts/smoke-live.py --env-file apps/travel-map/.env
```

Without `TRAVEL_MAP_LIVE_SMOKE=1`, with missing credentials, or with no approved snapshot, it exits `2` and emits one safe status. Success output has only case ID, provider status, route count, decision, and latency. Provider status distinguishes available routes, a provider outage, no route, and an out-of-coverage request that deliberately made no provider call. It never emits institution IDs, names, addresses, coordinates, route IDs, allowance amounts, credentials, headers, or raw provider responses.

Do not approve a release from this smoke alone. Record a manual review of 30 origin/destination pairs stratified across all 25 Seoul districts, institution types, and foundation types. Verify each pair's address and main-gate coordinate, multiple routes, round-trip classification near the 12 km boundary, separation of mobility cost from allowance, source references, and lookup time. A designated reviewer must record approval.

## Quotas, privacy, and rule provenance

Provider `503` and rate-limit results are unavailable data, not a reason to retry aggressively or invent a route. Respect `Retry-After`, stop the affected live check, inspect provider status privately, then retry only after its window. Do not add destination queries, addresses, route geometry, or credentials to logs or telemetry.

The current rule sources are versioned in `resources/rules/local-travel-2026-07-01.json`:

- [국가법령정보센터 여비규정](https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=287535)
- [국가법령정보센터 서울특별시교육청 조례](https://www.law.go.kr/LSW/ordinInfoP.do?ordinSeq=2099835)
- [인사혁신처 보수·여비 안내](https://www.mpm.go.kr/mpm/info/resultPay/payBoard/?boardId=bbs_0000000000000035&category=%EB%B3%B4%EC%88%98&cntId=693&mode=view)

## Container and release gate

The Docker context is staged from a reviewed workspace: it validates SGIS source and normalized geodata hashes, hash-pinned rule payloads, and `current.json` before Docker is consulted. The staged context contains no `.env`, Git metadata, source/raw provider data, geodata source, institution-source input, tests, E2E files, artifacts, or historical snapshots—only the snapshot selected by `current.json`. The runtime image uses UID `10001` and contains only application code, rules, normalized geodata/manifest, and that one approved institution snapshot. It has a `/healthz` health check and runs in production mode, so invalid settings or artifacts fail closed before serving traffic.

Stage A has an existing macOS release host requirement: its containment and
copy-on-write checks require `/usr/bin/sandbox-exec` and the Darwin
`fclonefileat` primitive. This host requirement does not change the application
image or NAS target platforms, which remain the explicitly selected
`linux/amd64` or `linux/arm64` value.

Run Stage A only after the snapshot is approved and a dedicated, authless local
Docker context exists. This stage must not receive registry credentials,
provider credentials, an auth-bearing Docker path, or an open secret file
descriptor:

```sh
record_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-release-record.XXXXXX")
record_parent=$(CDPATH= cd -- "$record_parent" && /bin/pwd -P)
/bin/chmod 0700 "$record_parent"

DOCKER_CONFIG=/protected/path/docker-local-authless \
NAS_PLATFORM=linux/amd64 \
RELEASE_GATE_IMAGE_RECORD="$record_parent/gated-image.record" \
./apps/travel-map/scripts/release-gate.sh
```

Stage A `DOCKER_CONFIG` is mandatory and has no implicit fallback. Its physical,
caller-owned directory must be `0700`, and its regular, non-symlink
`config.json` must be `0600`. It may select only a strictly described local Unix
socket context and must contain no `auths`, `credsStore`, `credHelpers`, or
`cliPluginsExtraDirs`. Select `linux/amd64` or `linux/arm64` only from the
read-only NAS platform inspection.

Every release tool executable and every directory in its physical path must be
owned by root or the caller. Tool executables must not be group- or
world-writable. A shared-writable ancestor is accepted only when that ancestor
is root- or caller-owned and has the sticky bit; this permits an owner-private
`0700` tool directory below `/tmp` while preventing cross-user sibling
replacement. A
group-writable non-sticky Homebrew `Cellar` therefore fails closed; use a
separately reviewed non-writable tool installation or an explicitly reviewed,
temporary mode-hardening procedure that is restored after the gate.

The gate tests and builds the exact clean `HEAD` bytes offline, reaps accidental
child processes, and durably creates one `0600` record only after the image and
platform are revalidated. The record has exactly these four fields:

The release host uses a dedicated dependency seed at
`~/.cache/travel-map-release/uv`; the everyday uv cache is not relocated or
modified. Populate this seed before Stage A with the locked dependency wheels
and build dependencies. It must pass the same ownership, link and content
validation as the original cache. The gate retains an immutable copy and gives
each installation phase a fresh writable copy-on-write cache so uv can create
its metadata and build the local project. Executable wheel files retain their
execute bit. Test-created cache contents are never reused for release-context
preparation. Missing offline dependencies continue to block the gate.

```text
imageTag=seoul-education-travel-map:release-gate-<40-char-git-sha>
imageId=sha256:<64-hex-local-image-id>
platform=linux/amd64|linux/arm64
gitSha=<40-char-git-sha>
```

Privately present those four values plus the SHA-256 of the exact record bytes
for action-time approval. A valid local tag may remain when Stage A is
interrupted or fails after the build; without the durable record it has no
release authority. Do not delete a possibly replaced mutable tag automatically.
After investigation or successful publication, an administrator may remove only
the exact reviewed local tag and record directory.

For an explicit build and local production run, prepare the same minimal context
first. The supported build context is the staged directory below; do not run
`docker build apps/travel-map`, which can include retained snapshot history.

```sh
context_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-release.XXXXXX")
trap 'rm -rf -- "$context_parent"' EXIT HUP INT TERM
snapshot_id=$(uv run --project apps/travel-map python \
  apps/travel-map/scripts/prepare-release-context.py \
  --source apps/travel-map --destination "$context_parent/context")
docker build --build-arg SNAPSHOT_ID="$snapshot_id" \
  -t seoul-education-travel-map:0.1.0 "$context_parent/context"
docker run --rm --init -p 8080:8080 \
  --env-file /secure/path/travel-map-production.env \
  seoul-education-travel-map:0.1.0
```

## NAS production operations (administrators only)

The only public origin and Kakao redirect/domain is
<https://travel.h19h19.com>. Cloudflare Tunnel routes that origin to the local
service at `127.0.0.1:18080`; never publish an alternate NAS hostname, including
during rollback. Keep these operational details out of the public usage panel.

### Current one-time stateless beta profile

The currently approved beta profile sets `STATELESS_BETA=1`. It keeps the
public institution/address search, all three trip patterns, route providers,
and policy calculation, but does not expose Kakao login, saved defaults,
settings, history, or any user SQLite database. Inputs and results are held
only for the active request/page; no browser storage or user-data volume is
used. Provider credentials remain server-side and are still required for the
public route/place services.

The non-secret mode fields in the beta `runtime.env` are:

```text
ENVIRONMENT=production
STATELESS_BETA=1
PUBLIC_BASE_URL=https://travel.h19h19.com
ALLOWED_HOSTS=["travel.h19h19.com","127.0.0.1","localhost"]
ALLOWED_ORIGINS=["https://travel.h19h19.com"]
```

For this profile, stage
[`deploy/nas/compose.stateless.example.yml`](deploy/nas/compose.stateless.example.yml)
and keep the application, Compose file, and `runtime.env` below `/volume1`.
There is deliberately no `/volume2` bind mount and no database migration or
backup is created. `deploy-reviewed-image.sh` reads the mode marker without
sourcing the secret-bearing environment file, skips the private migration,
and rejects a stateless Compose file that attempts to mount user data.

The persistent login profile remains documented below for a later, separately
approved change. Do not add its `USER_DATABASE_PATH`, OIDC, HMAC, or encryption
settings to the beta runtime environment.

### Filesystem and runtime boundary

The application, fixed Compose file, immutable image state, and runtime
environment stay below `/volume1/docker/seoul-education-travel-map`. In the
current stateless beta there is no user-data mount. The persistent profile,
when separately approved, uses:

```text
/volume2/docker-1/seoul-education-travel-map/data/travel-map.sqlite3
```

The directory is `0700`, the database/WAL/SHM files are `0600`, and all are
owned by `10001:10001`. The application container has a read-only root and
runs as UID/GID `10001`. The persistent profile receives only `/data` as its
writable bind mount; do not mount it into cloudflared or a backup job. Use the
reviewed digest-only Compose asset on `/volume1`; `runtime.env`, `image.env`, and
`previous-image.env` are
regular, non-symlink `0600` files and are never copied into Git, images, logs,
screenshots, reports, or shell arguments.

The production settings use only these public endpoints:

```text
PUBLIC_BASE_URL=https://travel.h19h19.com
ALLOWED_HOSTS=["travel.h19h19.com","127.0.0.1","localhost"]
ALLOWED_ORIGINS=["https://travel.h19h19.com"]
```

Record the observed Cloudflare connector socket peer as one exact `/32` or
`/128` `TRUSTED_PROXY_CIDRS` value. Verify a spoofed forwarding header from an
untrusted peer is ignored before accepting the connector configuration.

### Login keys and retention (persistent profile only)

Use a login-only Kakao application. `KAKAO_OIDC_CLIENT_ID` and its
`KAKAO_OIDC_CLIENT_SECRET` configure OIDC; `KAKAO_REST_API_KEY` is the separate
server-only route/place provider key. Register only
`https://travel.h19h19.com/auth/kakao/callback`. Do not place any of those
values in this document or a command history.

Generate `KAKAO_SUBJECT_HMAC_KEY` and `DATA_ENCRYPTION_KEY_V1` exactly once and
preserve both across routine deployment and rollback. Replacing the subject key
disconnects existing users; replacing the data key makes settings and history
undecryptable. Change either only through an explicit reviewed identity or
re-encryption migration. `SESSION_HMAC_KEY` may rotate only with an announced
all-session logout.

Encrypted calculation history expires exactly 168시간 (168 hours) after creation.
Encrypted settings remain only until the user chooses data deletion. The active
NAS backup job must exclude this private directory and all `*.sqlite3`,
`*.sqlite3-wal`, and `*.sqlite3-shm` files. These settings and history are
intentionally not disaster-restored.

### Reviewed installation, migration, and rollback

Do not deploy without an existing immutable rollback digest for the running
platform in `image.env`; a first deployment without that baseline is blocked.
For a reviewed release, stage exactly these five NAS assets from the reviewed
commit in a new private staging directory on `/volume1`:

1. `compose.stateless.example.yml` for the current beta (or
   `compose.example.yml` only for the separately approved persistent profile)
2. `migrate-user-database.sh` for the persistent profile; the beta skips it
3. `backup-excludes.txt`
4. `verify-backup-exclusion.sh`
5. `deploy-reviewed-image.sh`

Compare each staged file's SHA-256 with the reviewed source, reject extra files
and symlinks, run `sh -n` on scripts, then atomically install the Compose file,
scripts, and exclusion file with their documented private modes. Do not copy or
overwrite `runtime.env`, `image.env`, or `previous-image.env` in this step.

The supported deploy wrapper validates and pulls one reviewed immutable GHCR
digest, preserves the prior digest in `previous-image.env`, and starts the
fixed Compose configuration. In the persistent profile it also runs migration
and schema verification before the swap; in the current beta it requires the
stateless mode marker and skips all private-store work. On failure, do not
start the new image. Roll back only by restoring the preceding immutable image
behind the same `travel.h19h19.com` Cloudflare route; never substitute a tag,
a different repository, or a different public origin.

Stage B starts only after a reviewer approves the independent tuple
`(imageTag, imageId, platform, gitSha, recordSha256)`. Invoke it from the clean
approved checkout whose `HEAD` is the approved `gitSha`; do not derive the
expected arguments from the record at publish time:

```sh
DOCKER_CONFIG=/protected/path/docker-ghcr-inline-auth \
apps/travel-map/deploy/nas/publish-reviewed-image.sh \
  /physical/path/travel-map-release-record.XXXXXX/gated-image.record \
  '<approved-image-tag>' \
  'sha256:<approved-local-image-id>' \
  '<approved-linux-platform>' \
  '<approved-40-char-git-sha>' \
  '<approved-record-sha256>'
```

The five-field content tuple is the approval authority; the record path is transport only.
The approval remains unchanged when relocating identical record bytes to another valid canonical,
secure `0700` parent does not change that approval, although every record
metadata, byte-hash, and tuple check still applies at the new path.

The `DOCKER_CONFIG` directory must be `0700` and its `config.json` must be
`0600`; both must be owned by the invoking user and must not be symlinks. Stage
B accepts only inline `ghcr.io` auth plus an optional strictly validated local
Unix socket context. External credential helpers, credential stores, plugin
search paths, and remote endpoints are forbidden. User-owned local sockets must
not be group- or world-accessible. A root-owned `0660` socket is allowed only
when its group is one of the caller's groups and it has no group-execute or
world bits; world-writable sockets are always forbidden.
The publisher does not rerun Stage A or execute source tests. Before opening the
auth config it validates the record hash and every independently approved field,
requires the approved commit as a clean `HEAD` with no hidden index flags, and
binds the publisher mode and bytes to that commit's Git blob. Only a private
`0500` copy of that verified launcher may open the auth config. Before any
registry operation it revalidates the local tag, image ID, platform, and
reviewed Git identity.

The publisher hashes the exact raw root and child manifest bytes against every
manifest descriptor. Buildx may reserialize image-config JSON, so config output
is checked semantically instead of being treated as raw descriptor bytes. The
runnable config digest remains bound by the verified manifest to the approved
local image ID; attestation config descriptors must retain their allowed media
type and positive size. The local `release-gate-*` tag is retained on both
success and failure and is removed only by the administrator's exact cleanup
step.

The publisher deliberately uses the stronger content-addressed registry tag
`<git-sha>-sha256-<local-image-id>` instead of the earlier plain `<git-sha>`
operational-plan tag. This binds the immutable registry name to both the
reviewed commit and the exact locally gated image bytes. The four-field record
is evidence, not a cryptographic provenance token; Stage B's independently
approved tuple is the authority. Process-group cleanup covers accidental child
processes from the reviewed snapshot. Deliberately hostile same-UID code that
interferes during creation-to-first-descriptor-binding is outside this gate's
threat boundary.
The mkdirat/openat replacement window for record/staging/lock resources is excluded.
The invoking UID must not be shared with untrusted concurrent code.
A separate UID/sandbox or inaccessible pre-provisioned parent is the upgrade path.

Before the swap, run the read-only backup check with secret-free artifacts:

```sh
verify-backup-exclusion.sh --job-config <absolute-job-export> \
  --backup-root <absolute-backup-destination>
```

The secret-free active-job export must include exactly one enabled line in this
format, using the checked-out exclusion file's physical absolute path:

```text
ACTIVE_EXCLUSION_FILE=/absolute/path/to/backup-excludes.txt
```

The verifier fails closed for comments, disabled entries, suffixes, duplicate
formats, or unsupported exports. It must print `BACKUP_EXCLUSION_OK`. If the
active NAS job cannot provide a readable export or dry-run and an inspectable
destination listing, record
`BLOCKED_BACKUP_CONFIGURATION_UNVERIFIED` and keep deployment blocked; do not
invent backup evidence.

After a successful stateless-beta swap, smoke-test anonymous institution/address
search and all three trip patterns. Confirm the private `/auth` and `/me`
endpoints are not registered, the UI hides login/history/settings, and no
`/volume2` user-data directory is created. The persistent profile additionally
requires login, default-workplace restore, history create/detail/delete,
logout, and anonymous calculation after logout; that profile is not part of
the current beta deployment.
