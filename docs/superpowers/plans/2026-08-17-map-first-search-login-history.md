# Map-First Search, Login, and 7-Day History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public travel map usable without login for institution/address search and three directional trip patterns, while adding optional Kakao login for an encrypted default workplace, settings, and exactly seven days of calculation history.

**Architecture:** Preserve the existing FastAPI, verified institution/rule snapshots, provider adapters, and vanilla JavaScript map. Split search, schedule, panels, auth, history, and settings into small ES modules; represent actual movement as explicit outbound/return route legs; and add an optional same-origin Kakao OIDC/session subsystem backed by application-encrypted SQLite on the NAS `/volume2` data volume. Public calculation must remain available when user storage is degraded.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, httpx, standard-library `sqlite3`/`asyncio`/`hashlib`/`hmac`/`secrets`, PyJWT with asymmetric-key verification, `cryptography` AES-GCM, vanilla ES modules, Kakao Maps/Local/OIDC, pytest, Ruff, mypy, Playwright, Docker Compose, Cloudflare Tunnel.

## Global Constraints

- The binding design is `docs/superpowers/specs/2026-08-17-map-first-search-login-history-design.md`. If code and this plan appear ambiguous, stop and reconcile them against that approved design before editing production code.
- Anonymous users retain all institution search, destination search, map, route, and allowance-preview functionality. Authentication is never required for calculation.
- The only public policy profile is `SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED`. The request model contains no `policyProfile`; sending it must return 422.
- `TripPattern` contains exactly `ROUND_TRIP`, `OUTBOUND_ONLY_END_AFTER_SCHEDULE`, and `RETURN_ONLY_DIRECT_TO_DESTINATION`.
- Business duration is always `startsAt` to `endsAt`, at least 2 minutes and no more than 24 hours. Only route legs implied by the selected pattern are queried, displayed, and charged.
- A typed string is never an authoritative origin or destination. The user must select a server-returned institution/place candidate or confirm a map-click reverse result.
- Kakao login proves only control of a Kakao account. It must not be described or persisted as proof of employment or public-official status.
- Store no Kakao email, nickname, profile image, access token, refresh token, raw `sub`, home address, route geometry, provider response, search query, or raw place candidate.
- History expires exactly 168 hours after creation. Expired rows are invisible before physical cleanup and are physically deleted at least hourly.
- User settings persist until “내 데이터 삭제”; calculation history does not. The default workplace is one verified active `siteId`, not an address or coordinate.
- SQLite lives only at `/data/travel-map.sqlite3` in the container, mounted from `/volume2/docker-1/seoul-education-travel-map/data`. The image, Compose, and container remain on SSD `/volume1`.
- The history database is excluded from long-term backup. The deployment must not copy it into Git, images, release contexts, NAS backup archives, logs, screenshots, Notion, or test artifacts.
- State-changing authenticated APIs require both an exact configured HTTPS `Origin` and a session-bound CSRF token. Do not enable credentialed cross-origin CORS.
- Public calculation survives user-storage open/write/cleanup failures. Auth/history/settings fail closed with 503; a logged-in calculation returns its preview plus `HISTORY_NOT_SAVED` if persistence fails.
- Keep the browser CSP free of `unsafe-inline` and render all server/provider strings with `textContent` or DOM properties.
- Tasks 0–8 are RED → minimal GREEN → focused verification → one commit. Larger Tasks 9–13 are split into the lettered review units below; each lettered unit has its own RED/GREEN cycle and commit. Do not combine units because they touch adjacent files.
- Run Python tests with `PYTHONWARNINGS=error`. Preserve the existing strict camelCase HTTP boundary, request-size cap, host validation, query-string redaction, CSP, and provider secret-isolation regressions.

---

## Target file structure

```text
apps/travel-map/
├── app/
│   ├── api/
│   │   ├── auth.py                         # Kakao callback, session/logout
│   │   ├── institutions.py                 # facets + paginated search
│   │   ├── me.py                           # history/settings/data deletion
│   │   ├── places.py                       # merged place search boundary
│   │   ├── policy.py                       # current fixed-rule disclosure
│   │   └── trips.py                        # anonymous preview + optional save
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── models.py                       # exact session/user values
│   │   ├── oauth.py                        # Kakao OIDC validation
│   │   └── session.py                      # opaque token + CSRF lifecycle
│   ├── institutions/
│   │   └── facets.py                       # canonical facet IDs/labels
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── crypto.py                       # AES-GCM envelopes
│   │   ├── database.py                     # schema/migration/PRAGMAs
│   │   ├── history.py                      # 168-hour repository
│   │   ├── migrations.py                   # explicit operator migration CLI
│   │   ├── models.py                       # exact encrypted-storage records
│   │   ├── retention.py                    # hourly physical cleanup
│   │   ├── users.py                        # HMAC-subject user lifecycle
│   │   └── user_settings.py                # encrypted settings repository
│   ├── trips/
│   │   ├── __init__.py
│   │   └── models.py                       # trip-pattern/leg domain values
│   └── static/
│       ├── app.js                          # composition root only
│       ├── auth.js
│       ├── combobox.js
│       ├── destination-picker.js
│       ├── help.js
│       ├── history.js
│       ├── institution-picker.js
│       ├── route-results.js
│       ├── schedule.js
│       ├── settings.js
│       └── trip-form.js
├── e2e/
│   ├── auth-history-settings.spec.ts
│   ├── destination-picker.spec.ts
│   ├── info-panels.spec.ts
│   ├── institution-picker.spec.ts
│   └── trip-patterns.spec.ts
└── tests/
    ├── api/
    ├── auth/
    ├── storage/
    └── security/
```

---

## One-time worktree setup

Run these commands once in the fresh implementation worktree before Task 0. They install only the frozen lockfile state and make every later pytest/Playwright command independently executable:

```sh
uv sync --project apps/travel-map --frozen --dev
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map exec playwright install chromium
```

Do not update either lockfile in this setup step. Record both exit codes in the Task 0 report.

---

### Task 0: Normalize the pre-existing Ruff format baseline

**Files:**
- Modify mechanically: `apps/travel-map/app/dependencies.py`
- Modify mechanically: `apps/travel-map/app/institutions/{models.py,snapshot.py,store.py,sync.py}`
- Modify mechanically: `apps/travel-map/app/institutions/sources/{common.py,kindergarten.py,neis.py,neis_classification.py,school_count_profile.py,sen.py,sen_counts.py,standard_school.py}`
- Modify mechanically: `apps/travel-map/app/main.py`
- Modify mechanically: `apps/travel-map/app/policy/{coverage.py,rules.py}`
- Modify mechanically: `apps/travel-map/scripts/{build-geodata.py,extract-sgis-seoul.py,prepare-release-context.py,smoke-live.py,sync-institutions.py}`
- Modify mechanically: `apps/travel-map/tests/api/test_institutions.py`
- Modify mechanically: `apps/travel-map/tests/institutions/{population_fixtures.py,test_snapshot.py,test_store.py,test_sync.py}`
- Modify mechanically: `apps/travel-map/tests/policy/{test_engine.py,test_production_geodata.py,test_sgis_extract.py}`
- Modify mechanically: `apps/travel-map/tests/providers/test_opinet.py`
- Modify mechanically: `apps/travel-map/tests/test_release.py`

**Produces:** A zero-diff `ruff format --check` baseline so Task 13 can gate the whole app/tests/scripts tree without bundling unrelated legacy formatting into feature commits.

- [ ] **Step 1: Reproduce the baseline RED**

```sh
UV_CACHE_DIR=/tmp/travel-map-format-baseline \
  uv run --project apps/travel-map ruff format --check --output-format concise \
  apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
```

Expected: exit 1 with exactly `31 files would be reformatted, 63 files already formatted`. If the count/list changed, update this task from the actual clean base before writing.

- [ ] **Step 2: Apply only Ruff's deterministic formatter**

```sh
UV_CACHE_DIR=/tmp/travel-map-format-baseline \
  uv run --project apps/travel-map ruff format \
  apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
```

Do not hand-edit or combine behavior changes in this commit.

- [ ] **Step 3: Verify the format GREEN and whitespace-safe diff**

```sh
UV_CACHE_DIR=/tmp/travel-map-format-baseline \
  uv run --project apps/travel-map ruff format --check \
  apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
git diff --check
```

Expected: `94 files already formatted`, exit 0, and a diff containing only formatter output in the 31 named files.

- [ ] **Step 4: Run behavior/static regressions**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map test:e2e
```

Expected: all existing gates pass with no production behavior change.

- [ ] **Step 5: Commit the isolated baseline**

```sh
git add apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
git commit -m "style: normalize travel map formatting"
```

---

### Task 1: Canonical institution facets, display names, and pagination

**Depends on:** Task 0.

**Files:**
- Create: `apps/travel-map/app/institutions/facets.py`
- Modify: `apps/travel-map/app/institutions/store.py`
- Modify: `apps/travel-map/app/institutions/models.py`
- Modify: `apps/travel-map/app/contracts.py`
- Modify: `apps/travel-map/app/api/institutions.py`
- Modify: `apps/travel-map/app/services/trip_preview.py`
- Modify: `apps/travel-map/app/static/app.js`
- Create: `apps/travel-map/tests/institutions/test_facets.py`
- Modify: `apps/travel-map/tests/institutions/test_store.py`
- Modify: `apps/travel-map/tests/api/test_institutions.py`
- Modify: `apps/travel-map/tests/api/test_trips.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class InstitutionFacetOption:
    value: str
    label: str
    count: int

@dataclass(frozen=True)
class InstitutionFacets:
    snapshot_id: str
    institution_types: tuple[InstitutionFacetOption, ...]
    foundation_types: tuple[InstitutionFacetOption, ...]
    education_offices: tuple[InstitutionFacetOption, ...]
    districts: tuple[InstitutionFacetOption, ...]

@dataclass(frozen=True)
class InstitutionSearchPage:
    items: tuple[InstitutionSearchItem, ...]
    total: int
    next_offset: int | None
    snapshot_id: str

class InstitutionSearchItemResponse(ApiModel):
    institution_id: str
    site_id: str
    site_name: str
    official_name: str
    display_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    road_address: str
    district: str
    coordinate: CoordinateResponse
    coordinate_quality: str
    snapshot_id: str
    snapshot_as_of: str

    @classmethod
    def from_domain(cls, value: InstitutionSearchItem) -> Self:
        raise NotImplementedError

def canonical_education_office(value: str | None) -> tuple[str | None, str | None]:
    raise NotImplementedError

def institution_display_name(
    official_name: str, site_name: str, site_count: int,
) -> str:
    raise NotImplementedError

class InstitutionStore:
    def facets(self) -> InstitutionFacets:
        raise NotImplementedError

    def search_page(
        self, *, query: str = "", institution_type: str | None = None,
        foundation_type: str | None = None,
        education_office: str | None = None,
        district: str | None = None,
        limit: int = 20, offset: int = 0,
    ) -> InstitutionSearchPage:
        raise NotImplementedError

    def display_name_for_site(self, site_id: str) -> str:
        raise NotImplementedError
```

The canonical registry is exact:

| Stable value | Korean label | Accepted raw aliases |
|---|---|---|
| `SEOUL_EDU_OFFICE` | 서울특별시교육청 | `서울특별시교육청` |
| `MINISTRY_OF_EDUCATION` | 교육부 | `교육부` |
| `SEOUL_EDU_SUPPORT_DONGBU` | 동부교육지원청 | `동부교육지원청`, `서울특별시동부교육지원청` |
| `SEOUL_EDU_SUPPORT_SEOBU` | 서부교육지원청 | `서부교육지원청`, `서울특별시서부교육지원청` |
| `SEOUL_EDU_SUPPORT_NAMBU` | 남부교육지원청 | `남부교육지원청`, `서울특별시남부교육지원청` |
| `SEOUL_EDU_SUPPORT_BUKBU` | 북부교육지원청 | `북부교육지원청`, `서울특별시북부교육지원청` |
| `SEOUL_EDU_SUPPORT_JUNGBU` | 중부교육지원청 | `중부교육지원청`, `서울특별시중부교육지원청` |
| `SEOUL_EDU_SUPPORT_GANGDONG_SONGPA` | 강동송파교육지원청 | unprefixed and `서울특별시`-prefixed forms |
| `SEOUL_EDU_SUPPORT_GANGSEO_YANGCHEON` | 강서양천교육지원청 | unprefixed and `서울특별시`-prefixed forms |
| `SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO` | 강남서초교육지원청 | unprefixed and `서울특별시`-prefixed forms |
| `SEOUL_EDU_SUPPORT_DONGJAK_GWANAK` | 동작관악교육지원청 | unprefixed and `서울특별시`-prefixed forms |
| `SEOUL_EDU_SUPPORT_SEONGBUK_GANGBU` | 성북강북교육지원청 | unprefixed and `서울특별시`-prefixed forms |
| `SEOUL_EDU_SUPPORT_SEONGDONG_GWANGJIN` | 성동광진교육지원청 | unprefixed and `서울특별시`-prefixed forms |

Strip `서울특별시` only when it produces one of those 11 support-office labels; keep the central office and Ministry distinct. Unknown nonblank offices fail the canonical mapper test rather than becoming arbitrary facet IDs. A regression loads every active office value from the approved production-shaped snapshot and proves the registry is complete. Facets are computed once from active institution/site records, not manifest totals or quarantined rows.

`institution_display_name()` returns `officialName` when the physical site is `main`, equal to the official name, or the institution has only one active site. For a multi-site institution, both `본관` and meaningful branch names are retained as `공식명 · 본관` / `공식명 · 분관` so selectable rows remain distinguishable. `TripPreviewService` uses the same function for `origin.name`.

Every search item exposes `coordinate` from the active site's verified routing anchor, not an address-derived browser coordinate. The API never accepts this coordinate back as authority—the selected `siteId` remains authoritative—but the picker may use it to pan and place the origin marker.

`GET /api/v1/institutions/facets` returns counted options and `snapshotId`. `GET /api/v1/institutions` adds `offset` (`0..100000`) and returns `items`, `total`, `nextOffset`, and `snapshotId`; ordering stays match-rank, normalized official name, `siteId`.

- [ ] **Step 1: Add RED tests for production-shaped names, canonical facets, and paging**

Add tests named:

```text
test_main_site_uses_official_name_in_search_and_trip_origin
test_multisite_headquarters_and_branch_have_distinct_display_names
test_search_item_exposes_only_the_verified_routing_anchor_coordinate
test_canonical_office_merges_prefixed_and_unprefixed_support_offices
test_facets_include_all_active_values_and_exclude_quarantined_records
test_search_page_has_stable_nonoverlapping_offsets_and_total
test_institutions_api_lists_items_for_filter_only_blank_query
test_institution_facets_api_uses_counted_camel_case_options
```

The name fixture must contain `{officialName: "샘물초등학교", siteName: "main"}`. Paging must insert more than 20 records with equal match ranks and assert the concatenated IDs contain no duplicates or omissions.

- [ ] **Step 2: Run focused tests and verify RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_facets.py \
  apps/travel-map/tests/institutions/test_store.py \
  apps/travel-map/tests/api/test_institutions.py \
  apps/travel-map/tests/api/test_trips.py -q
```

Expected: missing facet/page interfaces and the current `origin.name == "main"` assertion failures.

- [ ] **Step 3: Implement the canonical store boundary and response models**

Keep `search()` as a compatibility wrapper around `search_page(offset=0)` until all callers are migrated. Build one immutable `site_id -> display_name` index in `InstitutionStore.__init__`; do not scan on every trip preview.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: PASS with warning-strict mode.

- [ ] **Step 5: Commit Task 1**

```sh
git add apps/travel-map/app/institutions/facets.py \
  apps/travel-map/app/institutions/store.py \
  apps/travel-map/app/institutions/models.py \
  apps/travel-map/app/contracts.py \
  apps/travel-map/app/api/institutions.py \
  apps/travel-map/app/services/trip_preview.py \
  apps/travel-map/tests/institutions/test_facets.py \
  apps/travel-map/tests/institutions/test_store.py \
  apps/travel-map/tests/api/test_institutions.py \
  apps/travel-map/tests/api/test_trips.py
git commit -m "feat: expose canonical institution facets"
```

---

### Task 2: Merge Kakao keyword and address destination search

**Depends on:** Task 1 (serial because both change `contracts.py`, API dependencies, and shared API fixtures).

**Files:**
- Modify: `apps/travel-map/app/providers/kakao_local.py`
- Modify: `apps/travel-map/app/dependencies.py`
- Modify: `apps/travel-map/app/api/places.py`
- Modify: `apps/travel-map/tests/providers/test_kakao_local.py`
- Modify: `apps/travel-map/tests/api/test_places.py`
- Create: `apps/travel-map/tests/fixtures/providers/kakao-address-search.json`

**Interfaces:**

```python
@dataclass(frozen=True)
class PlaceSearchResult:
    candidates: tuple[PlaceCandidate, ...]
    warnings: tuple[str, ...]

@dataclass(frozen=True)
class ReversePlaceResult:
    candidate: PlaceCandidate | None
    warnings: tuple[str, ...]

class PlaceClient(Protocol):
    async def search(
        self, query: str, *, bounds: BoundingBox,
    ) -> PlaceSearchResult:
        raise NotImplementedError

    async def reverse_geocode(
        self, coordinate: Coordinate,
    ) -> ReversePlaceResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

async def _search_keyword(
    self, query: str, bounds: BoundingBox,
) -> PlaceSearchResult:
    raise NotImplementedError

async def _search_address(
    self, query: str, bounds: BoundingBox,
) -> PlaceSearchResult:
    raise NotImplementedError

def _merge_place_candidates(
    query: str,
    keyword: tuple[PlaceCandidate, ...],
    addresses: tuple[PlaceCandidate, ...],
    *,
    limit: int = 15,
) -> tuple[PlaceCandidate, ...]:
    raise NotImplementedError
```

Run the keyword and address endpoints concurrently through the existing bounded HTTP client. One endpoint failure yields the other endpoint’s candidates plus a safe code such as `KEYWORD_SEARCH_UNAVAILABLE`; only both failures produce `PLACE_PROVIDER_UNAVAILABLE`. Keyword candidates win a duplicate because they have a meaningful place name/provider ID. Address-only IDs are deterministic SHA-256 identifiers from canonical address plus exact coordinate, never Python `hash()`.

Warnings belong to the immutable per-call result. Remove mutable `last_warnings` from the public client protocol so concurrent search/reverse requests cannot overwrite another request’s status before the API reads it. The places cache stores/returns the complete `PlaceSearchResult`, including warnings; it never reconstructs warning state from a singleton client after an await.

Deduplicate first by exact provider ID, then by coordinate quantized to five decimals plus canonical nonblank road/lot address. Rank exact normalized name/address, prefix, then substring; break ties by canonical display name, address, and `placeId`. Cap after merging.

- [ ] **Step 1: Add RED provider and API tests**

```text
test_place_search_merges_keyword_and_road_address_candidates
test_place_search_returns_lot_address_only_candidate
test_place_search_deduplicates_address_candidate_in_favor_of_named_place
test_place_search_keeps_address_results_when_keyword_endpoint_fails
test_place_search_reports_unavailable_only_when_both_endpoints_fail
test_interleaved_search_and_reverse_keep_their_own_warnings
test_places_search_caches_the_merged_result_and_its_warnings
```

- [ ] **Step 2: Verify RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/providers/test_kakao_local.py \
  apps/travel-map/tests/api/test_places.py -q
```

Expected: address candidates are missing and partial provider failure currently returns no merged result.

- [ ] **Step 3: Implement bounded concurrent search and stable merge**

Do not include query/address/coordinate in exception messages or logs. Clear temporary authorization header dictionaries in `finally`, preserving the existing credential-isolation guarantees.

- [ ] **Step 4: Verify GREEN and secret regressions**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/providers/test_kakao_local.py \
  apps/travel-map/tests/providers/test_http.py \
  apps/travel-map/tests/api/test_places.py \
  apps/travel-map/tests/security/test_key_exposure.py -q
```

- [ ] **Step 5: Commit Task 2**

```sh
git add apps/travel-map/app/providers/kakao_local.py \
  apps/travel-map/app/dependencies.py \
  apps/travel-map/app/api/places.py \
  apps/travel-map/tests/providers/test_kakao_local.py \
  apps/travel-map/tests/api/test_places.py \
  apps/travel-map/tests/fixtures/providers/kakao-address-search.json
git commit -m "feat: merge Kakao keyword and address search"
```

---

### Task 3: Directional trip contracts, route legs, and fixed policy

**Depends on:** Task 2 (serial shared `contracts.py`, dependencies, and trip API fixtures).

**Files:**
- Create: `apps/travel-map/app/trips/__init__.py`
- Create: `apps/travel-map/app/trips/models.py`
- Modify: `apps/travel-map/app/contracts.py`
- Modify: `apps/travel-map/app/policy/models.py`
- Modify: `apps/travel-map/app/policy/engine.py`
- Modify: `apps/travel-map/app/services/trip_preview.py`
- Modify: `apps/travel-map/app/static/app.js`
- Modify: `apps/travel-map/tests/api/conftest.py`
- Modify: `apps/travel-map/tests/api/test_trips.py`
- Modify: `apps/travel-map/tests/policy/test_engine.py`
- Modify: `apps/travel-map/e2e/fixtures/preview.json`
- Modify: `apps/travel-map/e2e/route-preview.spec.ts`

**Interfaces:**

```python
class TripPattern(StrEnum):
    ROUND_TRIP = "ROUND_TRIP"
    OUTBOUND_ONLY_END_AFTER_SCHEDULE = "OUTBOUND_ONLY_END_AFTER_SCHEDULE"
    RETURN_ONLY_DIRECT_TO_DESTINATION = "RETURN_ONLY_DIRECT_TO_DESTINATION"

class RouteDirection(StrEnum):
    OUTBOUND = "OUTBOUND"
    RETURN = "RETURN"

class DistanceEvidenceBasis(StrEnum):
    ROUND_TRIP_EXACT = "ROUND_TRIP_EXACT"
    ONE_WAY_LOWER_BOUND = "ONE_WAY_LOWER_BOUND"

@dataclass(frozen=True)
class PlannedTripLeg:
    direction: RouteDirection
    query: RouteQuery

@dataclass(frozen=True)
class PolicyInput:
    destination_in_seoul: bool
    measured_distance_m: int | None
    distance_evidence_basis: DistanceEvidenceBasis | None
    starts_at: datetime
    ends_at: datetime
    policy_profile: PolicyProfile
    vehicle_use: VehicleUse
    has_other_local_trips_today: bool
    previous_allowance_krw: int

    def __post_init__(self) -> None:
        if (self.measured_distance_m is None) != (
            self.distance_evidence_basis is None
        ):
            raise ValueError("distance and evidence basis must be both present or absent")
        if self.measured_distance_m is not None and self.measured_distance_m < 0:
            raise ValueError("measured distance must be nonnegative")

class RouteLegResponse(ApiModel):
    direction: RouteDirection
    depart_at: datetime
    routes: tuple[RouteResponse, ...]
    best: BestResponse
    mobility_cost: AmountResponse

class TripPreviewRequest(ApiRequestModel):
    origin_site_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9-]*:[A-Za-z0-9:_-]+$"),
    ]
    destination: DestinationInput
    starts_at: datetime
    ends_at: datetime
    trip_pattern: TripPattern
    vehicle_use: VehicleUse
    car_assumptions: CarAssumptionsInput
    has_other_local_trips_today: bool
    previous_allowance_krw: Annotated[int, Field(ge=0, le=20_000)]

    @model_validator(mode="after")
    def interval_is_aware_and_bounded(self) -> Self:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.starts_at, self.ends_at)
        ):
            raise ValueError("startsAt and endsAt must be timezone-aware")
        duration = self.ends_at - self.starts_at
        if not timedelta(minutes=2) <= duration <= timedelta(hours=24):
            raise ValueError("trip duration must be in [2 minutes, 24 hours]")
        return self

class TripPreviewResponse(ApiModel):
    coverage: CoverageResponse
    origin: OriginResponse
    institution_snapshot_id: str | None
    trip_pattern: TripPattern
    route_legs: tuple[RouteLegResponse, ...]
    policy_scope: PolicyProfile
    classification: Classification
    classification_distance_meters: int | None
    classification_distance_basis: DistanceEvidenceBasis | None
    classification_path: ClassificationPathResponse | None
    mobility_cost: AmountResponse
    allowance: AllowanceResponse
    rule_set_id: str | None
    effective_from: str | None
    source_refs: tuple[str, ...]
    warnings: tuple[str, ...]
```

Define `DistanceEvidenceBasis` in `app/policy/models.py`; `app/trips/models.py` imports and re-exports it. This keeps the policy model independent of trip-service modules and prevents a policy↔trips import cycle.

Remove top-level `routes` and `best`; the same image deploy updates the browser and API together. Route IDs only need to be unique inside a leg. The overall `mobilityCost` sums each leg’s fastest route only when every selected leg has a known/estimated amount; otherwise it returns `UNKNOWN` with `PARTIAL_MOBILITY_COST` and the per-leg amounts remain visible.

Rename the policy-domain `round_trip_distance_m` input to `measured_distance_m` and add `distance_evidence_basis`; no one-way value may enter a field described as an exact round trip. Leg planning is exact:

```text
ROUND_TRIP:
  OUTBOUND origin→destination at startsAt
  RETURN   destination→origin at endsAt
OUTBOUND_ONLY_END_AFTER_SCHEDULE:
  OUTBOUND origin→destination at startsAt
RETURN_ONLY_DIRECT_TO_DESTINATION:
  RETURN   destination→origin at endsAt
```

Display providers and classification provider are called only for these legs. For round trip, classification distance is the two leg distances summed and basis is `ROUND_TRIP_EXACT`. For a Seoul one-way leg, the known leg distance is a safe lower bound with basis `ONE_WAY_LOWER_BOUND`: if it already exceeds the rule’s actual-expense-inclusive threshold, allowance calculation may proceed with warning `ONE_WAY_DISTANCE_LOWER_BOUND`; otherwise allowance is `REVIEW_REQUIRED` with `TRIP_PATTERN_DISTANCE_RULE_UNVERIFIED`. BUFFER/outside-Seoul one-way never invents a round-trip distance and always withholds the allowance, but it still queries and returns the one actual leg when that provider supports the coordinates. Only a genuine provider range/failure removes route cards. If any classification leg required by the pattern is missing/failed, pass `measured_distance_m=None` and `distance_evidence_basis=None`; `PolicyEngine` selects the current rule but returns `Classification.REVIEW_REQUIRED`, `AllowanceStatus.REVIEW_REQUIRED`, no amount, and `DISTANCE_EVIDENCE_UNAVAILABLE`. Never substitute zero or claim an evidence basis. Duration always uses `startsAt`→`endsAt`.

Apply `parkingCostKrw` exactly once per trip. For a round trip, the outbound car leg carries the configured parking amount and the return car leg carries zero. For either one-way pattern, its sole car leg carries the configured amount. Transit fare, fuel, and toll continue to come from each actual queried leg.

`TripPreviewService` injects `PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED`. `policyProfile` and `returnsAt` are forbidden extras.

- [ ] **Step 1: Add RED request, route-leg, provider-call, and fixed-policy tests**

```text
test_round_trip_queries_both_display_and_classification_legs_at_exact_times
test_outbound_only_never_queries_return_providers_or_counts_return_cost
test_return_only_queries_destination_to_workplace_at_ends_at_only
test_route_legs_keep_directional_routes_and_costs_separate
test_round_trip_applies_parking_cost_exactly_once
test_outside_one_way_returns_its_actual_route_leg_but_requires_allowance_review
test_buffer_one_way_returns_its_actual_route_leg_but_requires_allowance_review
test_short_seoul_one_way_requires_distance_rule_review
test_missing_classification_leg_has_no_distance_basis_and_requires_review
test_policy_input_rejects_half_present_or_negative_distance_evidence
test_preview_rejects_legacy_returns_at_and_caller_policy_profile
test_preview_rejects_naive_reversed_one_minute_and_over_twenty_four_hour_intervals
test_preview_rejects_invalid_origin_id_and_previous_allowance_bounds
test_preview_always_reports_seoul_education_policy_scope
test_browser_renders_route_legs_without_legacy_top_level_routes
```

Extend `FakeRouteProvider` to retain complete `queries`, not only a call count. Test coordinates, `depart_at`, and modes for every call.

- [ ] **Step 2: Verify RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/policy/test_engine.py -q
```

Expected: request fields/enum/leg response are absent and current display routing performs only the outbound query.

- [ ] **Step 3: Implement the domain enums, leg planner, per-leg collections, and fixed policy**

Extract `_plan_trip_legs()`, `_display_leg()`, `_classification_routes()`, and `_aggregate_mobility_cost()` as pure/testable boundaries. Preserve per-mode TTL behavior independently for each leg via the existing route-cache key. In the same commit, update the existing `app.js` renderer to iterate `preview.routeLegs`, label each direction, and key route selection as `${direction}:${route.id}`; remove every read of legacy `preview.routes`/`preview.best`. Task 6 later extracts this already-GREEN behavior into `route-results.js` rather than repairing a broken intermediate commit.

- [ ] **Step 4: Verify GREEN and provider/cache regressions**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/policy \
  apps/travel-map/tests/routing \
  apps/travel-map/tests/providers -q
pnpm --dir apps/travel-map exec playwright test e2e/route-preview.spec.ts
```

- [ ] **Step 5: Commit Task 3**

```sh
git add apps/travel-map/app/trips \
  apps/travel-map/app/contracts.py \
  apps/travel-map/app/policy/models.py \
  apps/travel-map/app/policy/engine.py \
  apps/travel-map/app/services/trip_preview.py \
  apps/travel-map/app/static/app.js \
  apps/travel-map/tests/api/conftest.py \
  apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/policy/test_engine.py \
  apps/travel-map/e2e/fixtures/preview.json \
  apps/travel-map/e2e/route-preview.spec.ts
git commit -m "feat: model directional trip route legs"
```

---

### Task 4: Current policy disclosure API

**Depends on:** Task 3 (serial shared policy engine/contracts/API router).

**Files:**
- Create: `apps/travel-map/app/api/policy.py`
- Modify: `apps/travel-map/app/api/__init__.py`
- Modify: `apps/travel-map/app/contracts.py`
- Modify: `apps/travel-map/app/policy/engine.py`
- Create: `apps/travel-map/tests/api/test_policy.py`

**Interfaces:**

```python
class PolicyEngine:
    def rule_for_date(self, on_date: date) -> RuleSet:
        raise NotImplementedError

class PolicyDisclosureResponse(ApiModel):
    profile: Literal["SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"]
    profile_label: str
    rule_set_id: str
    effective_from: str
    local_round_trip_exclusive_meters: int
    actual_expense_inclusive_meters: int
    four_hours_minutes: int
    under_four_hours_krw: int
    four_hours_or_more_krw: int
    official_vehicle_deduction_krw: int
    source_refs: tuple[str, ...]
```

`GET /api/v1/policy/current` selects the rule for the current Asia/Seoul date using an injectable `_today_in_seoul()` seam. HTML/JS contains no duplicated rule amounts or thresholds.

- [ ] **Step 1: Add RED disclosure tests**

```text
test_current_policy_disclosure_matches_effective_rule_and_fixed_profile
test_policy_disclosure_exposes_only_validated_https_sources
test_policy_disclosure_does_not_expose_rule_repository_paths
```

- [ ] **Step 2: Run RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_policy.py \
  apps/travel-map/tests/policy/test_rules.py -q
```

- [ ] **Step 3: Implement the read-only endpoint and GREEN it**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 4: Commit Task 4**

```sh
git add apps/travel-map/app/api/policy.py \
  apps/travel-map/app/api/__init__.py \
  apps/travel-map/app/contracts.py \
  apps/travel-map/app/policy/engine.py \
  apps/travel-map/tests/api/test_policy.py
git commit -m "feat: disclose the active travel policy"
```

---

### Task 5: Map-first institution and destination pickers

**Depends on:** Tasks 1 and 2.

**Files:**
- Create: `apps/travel-map/app/static/combobox.js`
- Create: `apps/travel-map/app/static/institution-picker.js`
- Create: `apps/travel-map/app/static/destination-picker.js`
- Modify: `apps/travel-map/app/static/api.js`
- Modify: `apps/travel-map/app/static/kakao-map.js`
- Modify: `apps/travel-map/app/static/app.js`
- Modify: `apps/travel-map/app/static/index.html`
- Modify: `apps/travel-map/app/static/styles.css`
- Create: `apps/travel-map/e2e/fixtures/institution-facets.json`
- Modify: `apps/travel-map/e2e/fixtures/institutions.json`
- Modify: `apps/travel-map/e2e/fixtures/places.json`
- Modify: `apps/travel-map/e2e/helpers.ts`
- Create: `apps/travel-map/e2e/institution-picker.spec.ts`
- Create: `apps/travel-map/e2e/destination-picker.spec.ts`

**Interfaces:**

```javascript
export function createCombobox({
  input, listbox, status, debounceMs, minLength,
  search, renderOption, onSelect, onInvalidate,
})
// => { searchNow(), clear(), selected(), destroy() }

export function createInstitutionPicker({ api, map, elements, onSelectionChange })
// => {
//   initialize(), selected(), selectResolved(item), clear(), destroy()
// }

export function createDestinationPicker({ api, map, elements, onSelectionChange })
// => {
//   initialize(), selected(), setQueryAndSearch(query), clear(),
//   confirmReverseCandidate(place), destroy()
// }

class KakaoMapController {
  showOriginCandidate(placeOrSite): void
  clearOriginCandidate(): void
  showDestinationCandidate(place): void
  clearDestinationCandidate(): void
  setClickHandler(handler): void
  setActiveRoute(direction, routeId): void
}
```

The institution combobox uses `minLength=0`: focus, filter change, and “더 보기” work with an empty query. Selects are populated only from `/institutions/facets`. If facets return 503, the picker still initializes text search, disables only the unavailable filters, announces a retry action, and keeps anonymous calculation reachable. The destination combobox uses `minLength=2` and 250 ms debounce. Both use `AbortController` plus a monotonically increasing request ID so a late response cannot overwrite a newer one.

Typing after either selection immediately invalidates the selected object, calls the corresponding `clearOriginCandidate()` / `clearDestinationCandidate()`, and disables calculation. `createInstitutionPicker` receives `map` explicitly rather than relying on a global callback. Selecting an institution places/pans an origin marker from its verified routing anchor; selecting a destination places/pans exactly one replaceable destination marker. Map click clears the old destination, reverse-geocodes, and presents an explicit confirmable candidate; it never silently authorizes calculation.

`selectResolved(item)` accepts only an item from a current server response (including settings/history `resolvedDefaultOrigin`/`resolvedOrigin`) and reuses the same selection validation/render path as a listbox click. `setQueryAndSearch(query)` first clears destination authority/marker, writes the bounded text query, runs the normal debounced search immediately, and resolves only after the latest response is rendered; it never auto-selects a result.

- [ ] **Step 1: Add RED Playwright tests and production-shaped fixtures**

```text
filter_only_blank_query_displays_institutions
facets_populate_every_server_option_and_count
main_site_displays_and_selects_official_name
institution_selection_pans_to_the_verified_origin_marker
editing_selected_institution_clears_origin_marker_and_disables_submit
institution_picker_announces_loading_zero_and_error_states
facet_failure_leaves_text_search_and_anonymous_calculation_available
institution_pagination_appends_without_duplicates
road_or_lot_address_can_be_selected_as_destination
destination_selection_shows_one_replaceable_marker_and_pans_map
editing_selected_destination_invalidates_selection_and_marker
map_click_requires_reverse_candidate_confirmation
keyboard_arrows_enter_and_escape_control_both_listboxes
```

Update the E2E institution fixture so `siteName` is literally `main`; the old unrealistic fixture must not remain.

- [ ] **Step 2: Verify RED**

```sh
cd apps/travel-map
pnpm exec playwright test \
  e2e/institution-picker.spec.ts \
  e2e/destination-picker.spec.ts
```

- [ ] **Step 3: Implement shared combobox, two pickers, map candidate methods, and A-layout markup**

`app.js` only composes the modules and owns shared selected origin/destination. Do not copy search logic into it.

- [ ] **Step 4: Verify GREEN and existing map lifecycle tests**

```sh
cd apps/travel-map
pnpm exec playwright test \
  e2e/institution-picker.spec.ts \
  e2e/destination-picker.spec.ts \
  e2e/route-preview.spec.ts
```

- [ ] **Step 5: Commit Task 5**

```sh
git add apps/travel-map/app/static \
  apps/travel-map/e2e/fixtures \
  apps/travel-map/e2e/helpers.ts \
  apps/travel-map/e2e/institution-picker.spec.ts \
  apps/travel-map/e2e/destination-picker.spec.ts
git commit -m "feat: add map-first origin and destination pickers"
```

---

### Task 6: Synchronized duration controls and route-leg results

**Depends on:** Tasks 3 and 5.

**Files:**
- Create: `apps/travel-map/app/static/schedule.js`
- Create: `apps/travel-map/app/static/trip-form.js`
- Create: `apps/travel-map/app/static/route-results.js`
- Modify: `apps/travel-map/app/static/app.js`
- Modify: `apps/travel-map/app/static/kakao-map.js`
- Modify: `apps/travel-map/app/static/index.html`
- Modify: `apps/travel-map/app/static/styles.css`
- Modify: `apps/travel-map/e2e/helpers.ts`
- Modify: `apps/travel-map/e2e/fixtures/preview.json`
- Create: `apps/travel-map/e2e/trip-patterns.spec.ts`
- Modify: `apps/travel-map/e2e/route-preview.spec.ts`

**Interfaces:**

```javascript
export function addLocalMinutes(dateValue, timeValue, minutes)
// => { date: "YYYY-MM-DD", time: "HH:MM" } | null

export function differenceLocalMinutes(startDate, startTime, endDate, endTime)
// => integer | null

export function createScheduleController({ elements, onValidityChange })
// => {
//   startsAt(), endsAt(), durationMinutes(), tripPattern(), valid(),
//   applyDefaults({ tripPattern, durationMinutes }),
//   applyDraft({ startsAt, endsAt, tripPattern }), destroy()
// }

export function createTripForm({ originPicker, destinationPicker, schedule, elements })
// => {
//   valid(), payload(),
//   applySettings(settings, resolvedDefaultOrigin),
//   applyRecalculationDraft(draft, resolvedOrigin),
//   clearResultState()
// }

export function createRouteResults({ elements, map })
// => {
//   render(preview, destination), clear(), setSort(sort),
//   selectedRouteIdsByDirection()
// }
```

Do local calendar arithmetic from the visible date/time components; do not rely on adding milliseconds to a browser-local `Date` and then slicing UTC ISO strings. `payload()` constructs timezone-aware Asia/Seoul strings with the current `+09:00` offset.

`applySettings()` sets every vehicle/fuel/efficiency/parking input, calls `schedule.applyDefaults()`, and selects only a non-null current server-resolved origin. `applyRecalculationDraft()` calls `schedule.applyDraft()`, selects only a current `resolvedOrigin`, then calls `destinationPicker.setQueryAndSearch()` with the stored name/address; it returns `{originResolved, destinationSearchStarted}` and leaves submit disabled until an explicit destination result is selected.

Synchronization rules are exact:

1. Changing duration sets `endsAt = startsAt + duration`.
2. Changing start preserves the current duration and moves the end.
3. Changing end recomputes duration in minutes.
4. Crossing midnight increments the end date.
5. Valid duration is 2 through 1,440 minutes.
6. Quick choices are 1 h, 2 h, 4 h, 5 h, and 8 h; direct hour/minute input remains possible.

Pattern copy and route-leg display:

```text
ROUND_TRIP: 일반 왕복 / 가는 길 + 돌아오는 길
OUTBOUND_ONLY_END_AFTER_SCHEDULE: 일정 후 퇴근 / 가는 길만
RETURN_ONLY_DIRECT_TO_DESTINATION: 출장지로 바로 출근 후 근무지 복귀 / 돌아오는 길만
```

For a round trip, each leg owns its own route cards and selected map polyline. The map keys route lines by the composite `(direction, routeId)`, because providers may reuse a route ID across legs. `setActiveRoute(direction, routeId)` highlights only that composite route. The map draws the selected outbound and return polylines together; replacing a selection clears only that leg’s old polyline. `route-results.js` renders provider fields as inert text and computes no policy or cost server-side values itself.

Distance evidence copy is exact and never calls a one-way value “왕복”: `ROUND_TRIP_EXACT` renders `왕복 확인 거리`, `ONE_WAY_LOWER_BOUND` renders `편도 확인 거리(하한)`, and null basis renders `거리 근거 없음 · 지급액 검토 필요`. The allowance warning remains visible beside that text.

Remove the editable “적용 규정” select. Replace it with a read-only badge “서울특별시교육청 공무원 여비 기준” and text explaining that Kakao login is not employment verification.

- [ ] **Step 1: Add RED unit-in-browser and E2E tests**

```text
five_hour_duration_updates_end_time
duration_rolls_end_into_next_date
manual_end_change_updates_duration
start_change_preserves_duration
one_minute_or_over_twenty_four_hours_blocks_submit
each_trip_pattern_sends_exact_enum_and_no_legacy_fields
round_trip_renders_two_route_leg_sections_and_two_selected_polylines
same_route_id_in_outbound_and_return_remains_independently_selectable
outbound_only_renders_no_return_leg
return_only_renders_no_outbound_leg
fixed_policy_badge_replaces_policy_selector
distance_basis_copy_never_labels_one_way_or_missing_evidence_as_round_trip
```

- [ ] **Step 2: Verify RED**

```sh
cd apps/travel-map
pnpm exec playwright test \
  e2e/trip-patterns.spec.ts \
  e2e/route-preview.spec.ts
```

- [ ] **Step 3: Implement the three ES modules and route-leg map lifecycle**

Keep all DOM IDs centralized in `app.js` composition. No module may fetch by constructing URLs directly; use `api.js`.

- [ ] **Step 4: Verify GREEN on desktop and a named 375×812 full-flow test**

Run the Step 2 command. Add `mobile_375_search_candidate_schedule_result_and_map_flow` with an explicit 375×812 viewport. It performs origin search/selection → destination search/selection → duration/pattern → calculation → result → map expand/collapse; asserts DOM order and zero horizontal overflow; and proves origin, destination, and each route direction have visible text/icon labels so status is not conveyed by color alone. Assert no application console errors.

- [ ] **Step 5: Commit Task 6**

```sh
git add apps/travel-map/app/static \
  apps/travel-map/e2e/helpers.ts \
  apps/travel-map/e2e/fixtures/preview.json \
  apps/travel-map/e2e/trip-patterns.spec.ts \
  apps/travel-map/e2e/route-preview.spec.ts
git commit -m "feat: add synchronized directional trip controls"
```

---

### Task 7: Accessible usage and related-policy panels

**Depends on:** Tasks 4 and 6.

**Files:**
- Create: `apps/travel-map/app/static/help.js`
- Modify: `apps/travel-map/app/static/api.js`
- Modify: `apps/travel-map/app/static/app.js`
- Modify: `apps/travel-map/app/static/index.html`
- Modify: `apps/travel-map/app/static/styles.css`
- Create: `apps/travel-map/e2e/fixtures/policy-current.json`
- Modify: `apps/travel-map/e2e/helpers.ts`
- Create: `apps/travel-map/e2e/info-panels.spec.ts`

**Interfaces:**

```javascript
export function createHelpPanels({
  helpButton, policyButton, helpDialog, policyDialog, api,
})
// => { initialize(), openHelp(), openPolicy(), closeAll(), destroy() }
```

Use labelled native `<dialog>` elements with explicit close controls. Opening moves focus to the heading/close control, Escape closes, and closing returns focus to the trigger. The usage guide covers candidate selection, all three patterns, duration synchronization, route/allowance interpretation, anonymous use, optional login, and seven-day retention. Policy amounts, thresholds, version, effective date, and links come only from `/api/v1/policy/current`.

External rule links use `target="_blank" rel="noopener noreferrer"`. All remote text is rendered without `innerHTML`.

- [ ] **Step 1: Add RED accessible panel tests**

```text
usage_help_explains_three_patterns_and_anonymous_use
policy_panel_renders_server_rule_without_html_sink
dialogs_close_on_escape_and_restore_trigger_focus
policy_panel_shows_estimate_and_employment_disclaimers
```

- [ ] **Step 2: Verify RED**

```sh
cd apps/travel-map
pnpm exec playwright test e2e/info-panels.spec.ts
```

- [ ] **Step 3: Implement panels and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 4: Commit Task 7**

```sh
git add apps/travel-map/app/static/help.js \
  apps/travel-map/app/static/api.js \
  apps/travel-map/app/static/app.js \
  apps/travel-map/app/static/index.html \
  apps/travel-map/app/static/styles.css \
  apps/travel-map/e2e/fixtures/policy-current.json \
  apps/travel-map/e2e/helpers.ts \
  apps/travel-map/e2e/info-panels.spec.ts
git commit -m "feat: add usage and policy panels"
```

---

### Task 8: Authentication/storage settings and AES-GCM envelope

**Files:**
- Modify: `apps/travel-map/pyproject.toml`
- Modify: `apps/travel-map/uv.lock`
- Modify: `apps/travel-map/app/settings.py`
- Modify: `apps/travel-map/.env.example`
- Create: `apps/travel-map/app/storage/__init__.py`
- Create: `apps/travel-map/app/storage/crypto.py`
- Create: `apps/travel-map/tests/storage/__init__.py`
- Create: `apps/travel-map/tests/storage/test_crypto.py`
- Modify: `apps/travel-map/tests/providers/test_settings.py`
- Modify: `apps/travel-map/tests/security/test_key_exposure.py`

**Dependencies and settings:**

```toml
"cryptography>=46.0.0"
"PyJWT>=2.10.1"
```

```text
PUBLIC_BASE_URL=https://travel.h19h19.com
USER_DATABASE_PATH=/data/travel-map.sqlite3
KAKAO_OIDC_CLIENT_ID=<login-only Kakao REST client identifier>
KAKAO_OIDC_CLIENT_SECRET=<SecretStr>
SESSION_HMAC_KEY=<base64url 32 bytes>
KAKAO_SUBJECT_HMAC_KEY=<base64url 32 bytes>
DATA_ENCRYPTION_KEY_V1=<base64url 32 bytes>
TRUSTED_PROXY_CIDRS=<JSON list of exact connector /32 or /128 networks>
```

`KAKAO_OIDC_CLIENT_ID` belongs to a login-only Kakao application/key and is the public identifier placed in the authorization URL. It must not equal the server-only `KAKAO_REST_API_KEY` used by Local/routing providers; that provider key remains forbidden from browser payloads, redirect locations, logs, and HTML. In production the auth/storage setting group is mandatory, `PUBLIC_BASE_URL` is an exact path/query-free HTTPS origin included in `ALLOWED_ORIGINS`, and `USER_DATABASE_PATH` is exactly `/data/travel-map.sqlite3`. `KAKAO_OIDC_CLIENT_ID` and `TRUSTED_PROXY_CIDRS` are not secrets, but production must explicitly name only the observed Cloudflare connector/NAT peer as `/32` or `/128`; never trust the whole Docker/private network. Development/test may omit the entire user subsystem; partially supplied groups are invalid in every environment.

Extend `_TupleNormalizingSettingsSource` so blank optional auth/storage values in `.env.example` normalize to `None` in development. A nonblank value must still pass exact type/base64/path/origin validation; production may never fall back to development defaults.

**Interfaces:**

```python
@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    encryption_version: int

class UserDataUnavailableError(RuntimeError):
    """Fixed public boundary for known storage/cipher availability failures."""

class PayloadCipher:
    def __init__(
        self, *, keys: Mapping[int, bytes], active_version: int = 1,
    ) -> None:
        raise NotImplementedError

    def encrypt_json(
        self, *, purpose: str, owner_id: str, payload: dict[str, object]
    ) -> EncryptedPayload:
        raise NotImplementedError

    def decrypt_json(
        self, *, purpose: str, owner_id: str,
        ciphertext: bytes, encryption_version: int,
    ) -> dict[str, object]:
        raise NotImplementedError
```

Use AES-256-GCM with a fresh 12-byte nonce. Persist `nonce || ciphertext || tag`. AAD is UTF-8 `travel-map:{purpose}:v{version}:{owner_id}`. Only exact JSON objects with supported primitives may be encrypted; reject NaN/Infinity and unknown envelope versions. Decryption errors expose only `ENCRYPTED_PAYLOAD_INVALID`.

For history, `owner_id` is `{user_id}:{history_id}` and `purpose` is separately `history-input` or `history-summary`; for settings it is the numeric user ID with purpose `user-settings`. This makes ciphertext swaps across rows, users, or columns fail authentication.

- [ ] **Step 1: Add RED setting and cipher tests**

```text
test_encrypt_json_round_trips_canonical_object
test_encrypt_json_uses_a_unique_nonce_for_equal_plaintext
test_decrypt_rejects_wrong_owner_purpose_version_and_tampering
test_production_requires_exact_auth_storage_settings
test_oidc_client_id_must_not_equal_provider_rest_key
test_partial_auth_settings_are_rejected_in_development
test_auth_secret_values_never_appear_in_repr_errors_or_logs
```

- [ ] **Step 2: Verify RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_crypto.py \
  apps/travel-map/tests/providers/test_settings.py \
  apps/travel-map/tests/security/test_key_exposure.py -q
```

- [ ] **Step 3: Add dependencies, strict settings validation, and the cipher**

Use `uv add --project apps/travel-map 'cryptography>=46.0.0' 'PyJWT>=2.10.1'` so `pyproject.toml` and `uv.lock` remain synchronized. Decode each base64url secret to exactly 32 bytes inside a helper that clears temporary bytearray holders after service construction.

- [ ] **Step 4: Verify GREEN and frozen lock**

```sh
uv sync --project apps/travel-map --frozen --dev
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_crypto.py \
  apps/travel-map/tests/providers/test_settings.py \
  apps/travel-map/tests/security/test_key_exposure.py -q
```

- [ ] **Step 5: Commit Task 8**

```sh
git add apps/travel-map/pyproject.toml apps/travel-map/uv.lock \
  apps/travel-map/app/settings.py apps/travel-map/.env.example \
  apps/travel-map/app/storage apps/travel-map/tests/storage \
  apps/travel-map/tests/providers/test_settings.py \
  apps/travel-map/tests/security/test_key_exposure.py
git commit -m "feat: add encrypted user data boundary"
```

---

### Task 9: SQLite schema, repositories, and retention cleanup

**Depends on:** Tasks 3 and 8.

**Files:**
- Create: `apps/travel-map/app/storage/database.py`
- Create: `apps/travel-map/app/storage/models.py`
- Create: `apps/travel-map/app/storage/migrations.py`
- Create: `apps/travel-map/app/storage/history.py`
- Create: `apps/travel-map/app/storage/users.py`
- Create: `apps/travel-map/app/storage/user_settings.py`
- Create: `apps/travel-map/app/storage/retention.py`
- Create: `apps/travel-map/tests/storage/test_database.py`
- Create: `apps/travel-map/tests/storage/test_history.py`
- Create: `apps/travel-map/tests/storage/test_user_settings.py`
- Create: `apps/travel-map/tests/storage/test_retention.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class UserRecord:
    id: int
    created_at: datetime
    last_login_at: datetime

@dataclass(frozen=True)
class SessionRecord:
    user_id: int
    token_hmac: bytes
    csrf_hmac: bytes
    created_at: datetime
    expires_at: datetime

@dataclass(frozen=True)
class HistoryRecalculationDraft:
    origin_site_id: str
    origin_name: str
    destination_name: str
    destination_address: str
    trip_pattern: TripPattern
    starts_at: datetime
    ends_at: datetime

@dataclass(frozen=True)
class HistoryRouteLegSummary:
    direction: RouteDirection
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None

@dataclass(frozen=True)
class HistorySummary:
    classification: str
    allowance_status: str
    allowance_krw: int | None
    route_legs: tuple[HistoryRouteLegSummary, ...]
    rule_set_id: str | None
    effective_from: str | None

@dataclass(frozen=True)
class HistoryMetadata:
    id: str
    user_id: int
    created_at: datetime
    expires_at: datetime

@dataclass(frozen=True)
class HistoryListItem:
    metadata: HistoryMetadata
    origin_name: str
    destination_name: str
    trip_pattern: TripPattern
    classification: str
    allowance_status: str
    allowance_krw: int | None

@dataclass(frozen=True)
class HistoryDetail:
    metadata: HistoryMetadata
    draft: HistoryRecalculationDraft
    summary: HistorySummary

@dataclass(frozen=True)
class HistoryCursor:
    created_at: datetime
    history_id: str

@dataclass(frozen=True)
class HistoryPage:
    items: tuple[HistoryListItem, ...]
    next_cursor: HistoryCursor | None

@dataclass(frozen=True)
class StoredUserSettings:
    default_origin_site_id: str | None
    default_trip_pattern: TripPattern
    default_duration_minutes: int
    vehicle_use: VehicleUse
    fuel_type: FuelType
    efficiency_km_per_liter: float
    parking_cost_krw: int
    route_sort: Literal["time", "distance", "cost"]

DEFAULT_USER_SETTINGS = StoredUserSettings(
    default_origin_site_id=None,
    default_trip_pattern=TripPattern.ROUND_TRIP,
    default_duration_minutes=300,
    vehicle_use=VehicleUse.NONE,
    fuel_type=FuelType.GASOLINE,
    efficiency_km_per_liter=10.0,
    parking_cost_krw=0,
    route_sort="time",
)

@dataclass(frozen=True)
class CleanupCounts:
    oauth_attempts: int
    sessions: int
    history: int
    users: int

class SqliteDatabase:
    def __init__(self, path: Path) -> None:
        raise NotImplementedError

    def migrate(self) -> int:
        raise NotImplementedError

    def verify_current_schema(self) -> None:
        raise NotImplementedError

    async def read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        raise NotImplementedError

    async def write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        raise NotImplementedError

    async def checkpoint_truncate(self) -> None:
        raise NotImplementedError

class HistoryRepository:
    def __init__(
        self, database: SqliteDatabase, cipher: PayloadCipher,
        clock: Callable[[], datetime],
    ) -> None:
        raise NotImplementedError

    async def create(
        self, *, user_id: int, draft: HistoryRecalculationDraft,
        summary: HistorySummary,
    ) -> HistoryMetadata:
        raise NotImplementedError

    async def list_page(
        self, *, user_id: int, before: HistoryCursor | None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> HistoryPage:
        raise NotImplementedError

    async def get(
        self, *, user_id: int, history_id: str,
    ) -> HistoryDetail | None:
        raise NotImplementedError

    async def delete(self, *, user_id: int, history_id: str) -> bool:
        raise NotImplementedError

    async def delete_all(self, *, user_id: int) -> int:
        raise NotImplementedError

class UserSettingsRepository:
    def __init__(
        self, database: SqliteDatabase, cipher: PayloadCipher,
    ) -> None:
        raise NotImplementedError

    async def get(self, *, user_id: int) -> StoredUserSettings | None:
        raise NotImplementedError

    async def replace(
        self, *, user_id: int, settings: StoredUserSettings,
    ) -> None:
        raise NotImplementedError

class UserSessionRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        raise NotImplementedError

    async def upsert_user_and_insert_session(
        self, *, subject_hmac: bytes, token_hmac: bytes,
        csrf_hmac: bytes, now: datetime, expires_at: datetime,
    ) -> UserRecord:
        raise NotImplementedError

    async def resolve_session(
        self, *, token_hmac: bytes, now: datetime,
    ) -> SessionRecord | None:
        raise NotImplementedError

    async def revoke_session(self, *, token_hmac: bytes) -> bool:
        raise NotImplementedError

    async def revoke_all_sessions(self, *, user_id: int) -> int:
        raise NotImplementedError

    async def delete_user(self, *, user_id: int) -> bool:
        raise NotImplementedError

class RetentionCleaner:
    def __init__(
        self, database: SqliteDatabase, clock: Callable[[], datetime],
    ) -> None:
        raise NotImplementedError

    async def run_once(self, *, now: datetime) -> CleanupCounts:
        raise NotImplementedError

    async def run_forever(self, *, interval_seconds: int = 3600) -> None:
        raise NotImplementedError
```

Schema version 1 contains these exact application columns (plus fixed foreign-key/check constraints, never free-form metadata):

```text
schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)
users(id INTEGER PRIMARY KEY, kakao_subject_hmac BLOB UNIQUE NOT NULL,
      created_at TEXT NOT NULL, last_login_at TEXT NOT NULL)
oauth_login_attempts(attempt_hash BLOB PRIMARY KEY, state_hash BLOB NOT NULL,
      nonce_hash BLOB NOT NULL, created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL, consumed_at TEXT)
sessions(token_hash BLOB PRIMARY KEY, user_id INTEGER NOT NULL,
      csrf_token_hash BLOB NOT NULL, created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)
calculation_history(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
      created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
      encrypted_input BLOB NOT NULL, encrypted_summary BLOB NOT NULL,
      encryption_version INTEGER NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)
user_settings(user_id INTEGER PRIMARY KEY, encrypted_payload BLOB NOT NULL,
      encryption_version INTEGER NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)
```

Digest/check columns are exactly 32 bytes, encryption versions are positive supported integers, and text timestamps use one lexically sortable representation: `value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")`. Every stored timestamp therefore has exactly six fractional digits (`YYYY-MM-DDTHH:MM:SS.ffffffZ`); migration/repository readers reject mixed precision, offsets, or noncanonical input. History IDs are fresh 128-bit URL-safe opaque values and must match the one used in AES AAD. Index OAuth attempt/session/history expiration and `(user_id, created_at DESC, id DESC)`.

Every connection enables:

```text
PRAGMA foreign_keys=ON
PRAGMA journal_mode=WAL
PRAGMA busy_timeout=5000
PRAGMA secure_delete=ON
PRAGMA synchronous=NORMAL
PRAGMA trusted_schema=OFF
```

Connections are created and closed inside each `asyncio.to_thread` operation; no connection crosses threads. Runtime verifies but never auto-migrates. The operator runs:

```sh
uv run --project apps/travel-map python -m app.storage.migrations \
  migrate --database /data/travel-map.sqlite3
uv run --project apps/travel-map python -m app.storage.migrations \
  verify --database /data/travel-map.sqlite3
```

`HistoryRepository` receives a trusted UTC clock in its constructor and derives `created_at` internally; request `startsAt`/`endsAt` can never affect retention. History `expires_at = created_at + timedelta(hours=168)`, and visibility is exactly `expires_at > now`. `list_page()` is keyset-paginated by `(created_at DESC, id DESC)`, enforces an exact integer limit `1..100` again below the HTTP boundary, and returns an opaque API cursor encoded from both fields; callers can continue until `nextCursor=null`, so no still-valid record is silently truncated. Reads/writes delete already-expired history in the same owner-scoped transaction. Hourly cleanup deletes expired attempts/sessions/history, deletes an orphan user only when no settings/session/history remain, commits, then checkpoints WAL with `TRUNCATE`.

`UserSessionRepository.upsert_user_and_insert_session()` is the one storage authority for login issuance. It performs one transaction: insert a user keyed by unique 32-byte subject HMAC or update only `last_login_at`, then insert the supplied session/CSRF HMAC rows for that stable user ID. Any failure rolls back both operations, so hourly orphan cleanup cannot delete a newly created user between login and session issuance. It never accepts the raw Kakao subject or raw session/CSRF token.

The encrypted draft is deliberately smaller than `TripPreviewRequest`: it contains no destination coordinate, vehicle/fuel/efficiency/parking/other-trip inputs, geometry, provider payload, search query, or raw candidate. The future UI can prefill its text fields, but must perform a fresh candidate search and require explicit selection before recalculation.

Repositories serialize only the exact frozen records above and strictly re-validate decrypted JSON with unknown fields forbidden before returning it. Malformed/authentication-failed payloads return a fixed storage-unavailable error; no route/API may partially trust a decoded mapping.

Before the first SQLite connection, create the parent directory as `0700` and pre-create a new database with `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)`. Runtime and migration commands run under `umask 077`; after opening, validate the database, `-wal`, and `-shm` (when present) have no group/world bits. Refuse insecure existing paths instead of silently broadening permissions.

#### 9A — Database, migration, timestamps, and private modes

- [ ] **9A.1 Write the primary RED test** in `tests/storage/test_database.py`:

```python
def test_v1_migration_is_idempotent_private_and_rejects_mixed_timestamps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    assert database.migrate() == 1
    assert database.migrate() == 1
    database.verify_current_schema()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(StorageIntegrityError, match="timestamp"):
        parse_storage_timestamp("2026-08-17T00:00:00Z")
    with pytest.raises(StorageIntegrityError, match="timestamp"):
        parse_storage_timestamp("2026-08-17T09:00:00.000000+09:00")
    with pytest.raises(StorageIntegrityError, match="timezone"):
        format_storage_timestamp(datetime(2026, 8, 17))
```

- [ ] **9A.2 Run only that test and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_database.py::test_v1_migration_is_idempotent_private_and_rejects_mixed_timestamps -q
```

Expected: collection/import failure because `SqliteDatabase`/migration do not exist.

- [ ] **9A.3 Implement the exact schema/migration boundary**

```python
def format_storage_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageIntegrityError("storage timestamp timezone is invalid")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )

def parse_storage_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=UTC,
        )
    except (TypeError, ValueError):
        raise StorageIntegrityError("storage timestamp is invalid") from None
    if format_storage_timestamp(parsed) != value:
        raise StorageIntegrityError("storage timestamp is invalid")
    return parsed
```

Implement the fixed schema shown above, private path creation, PRAGMAs, sync test seam, and CLI; no dynamic SQL identifiers.

- [ ] **9A.4 Run the database/migration GREEN tests**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_database.py -q
```

- [ ] **9A.5 Commit**

```sh
git add apps/travel-map/app/storage/database.py \
  apps/travel-map/app/storage/migrations.py \
  apps/travel-map/app/storage/models.py \
  apps/travel-map/tests/storage/test_database.py
git commit -m "feat: add private user database migration"
```

#### 9B — Atomic user sessions and encrypted settings repositories

- [ ] **9B.1 Write the primary RED test** in `tests/storage/test_user_settings.py`:

```python
@pytest.mark.asyncio
async def test_atomic_login_reuses_user_and_settings_are_ciphertext(tmp_path: Path) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    cipher = PayloadCipher(keys={1: b"e" * 32})
    users = UserSessionRepository(database)
    settings = UserSettingsRepository(database, cipher)
    first = await users.upsert_user_and_insert_session(
        subject_hmac=b"s" * 32, token_hmac=b"1" * 32,
        csrf_hmac=b"a" * 32, now=UTC_NOW, expires_at=UTC_NOW + timedelta(days=7),
    )
    second = await users.upsert_user_and_insert_session(
        subject_hmac=b"s" * 32, token_hmac=b"2" * 32,
        csrf_hmac=b"b" * 32, now=UTC_NOW + timedelta(seconds=1),
        expires_at=UTC_NOW + timedelta(days=7, seconds=1),
    )
    assert first.id == second.id
    await settings.replace(user_id=first.id, settings=DEFAULT_USER_SETTINGS)
    assert await settings.get(user_id=first.id) == DEFAULT_USER_SETTINGS
    await database.checkpoint_truncate()
    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            assert b"ROUND_TRIP" not in artifact.read_bytes()
```

- [ ] **9B.2 Run that node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_user_settings.py::test_atomic_login_reuses_user_and_settings_are_ciphertext -q
```

- [ ] **9B.3 Implement one transactional user/session authority and settings encryption**

```python
async def upsert_user_and_insert_session(self, *, subject_hmac, token_hmac,
                                         csrf_hmac, now, expires_at):
    def operation(connection: sqlite3.Connection) -> UserRecord:
        now_text = format_storage_timestamp(now)
        expires_text = format_storage_timestamp(expires_at)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO users(kakao_subject_hmac, created_at, last_login_at) "
                "VALUES (?, ?, ?) ON CONFLICT(kakao_subject_hmac) DO UPDATE "
                "SET last_login_at=excluded.last_login_at",
                (subject_hmac, now_text, now_text),
            )
            row = connection.execute(
                "SELECT id, created_at, last_login_at FROM users "
                "WHERE kakao_subject_hmac=?",
                (subject_hmac,),
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("user upsert did not return a row")
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf_token_hash, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token_hmac, row[0], csrf_hmac, now_text, expires_text),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return UserRecord(
            id=row[0],
            created_at=parse_storage_timestamp(row[1]),
            last_login_at=parse_storage_timestamp(row[2]),
        )
    return await self._database.write(operation)
```

Use the exact repository signatures and `PayloadCipher` AAD from Tasks 8/9; rollback both user and session on any error.

- [ ] **9B.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_user_settings.py \
  apps/travel-map/tests/storage/test_database.py -q
```

- [ ] **9B.5 Commit**

```sh
git add apps/travel-map/app/storage/users.py \
  apps/travel-map/app/storage/user_settings.py \
  apps/travel-map/tests/storage/test_user_settings.py
git commit -m "feat: add encrypted user settings repository"
```

#### 9C — Minimal history, keyset pagination, and physical retention

- [ ] **9C.1 Write the primary RED test** in `tests/storage/test_history.py`:

```python
@pytest.mark.asyncio
async def test_history_expires_exactly_at_168_hours_and_pages_all_rows(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    user = await UserSessionRepository(database).upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"t" * 32,
        csrf_hmac=b"c" * 32,
        now=clock[0],
        expires_at=clock[0] + timedelta(days=7),
    )
    history_repository = HistoryRepository(
        database,
        PayloadCipher(keys={1: b"e" * 32}),
        clock=lambda: clock[0],
    )
    draft = HistoryRecalculationDraft(
        origin_site_id="neis:B10:7010057:main",
        origin_name="서울샘물초등학교",
        destination_name="서울시청",
        destination_address="서울특별시 중구 세종대로 110",
        trip_pattern=TripPattern.ROUND_TRIP,
        starts_at=clock[0] + timedelta(hours=1),
        ends_at=clock[0] + timedelta(hours=6),
    )
    summary = HistorySummary(
        classification="LOCAL",
        allowance_status="ESTIMATED",
        allowance_krw=20_000,
        route_legs=(),
        rule_set_id="2025-local-travel",
        effective_from="2025-01-01",
    )
    created = await history_repository.create(
        user_id=user.id, draft=draft, summary=summary,
    )
    clock[0] = created.expires_at - timedelta(microseconds=1)
    assert await history_repository.get(user_id=user.id, history_id=created.id) is not None
    clock[0] = created.expires_at
    assert await history_repository.get(user_id=user.id, history_id=created.id) is None
    for index in range(101):
        clock[0] += timedelta(microseconds=1)
        await history_repository.create(user_id=user.id, draft=draft, summary=summary)
    first = await history_repository.list_page(user_id=user.id, before=None, limit=100)
    second = await history_repository.list_page(
        user_id=user.id, before=first.next_cursor, limit=100,
    )
    assert len(first.items) == 100 and len(second.items) == 1
```

- [ ] **9C.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_history.py::test_history_expires_exactly_at_168_hours_and_pages_all_rows -q
```

- [ ] **9C.3 Implement history projection, encryption, pagination, and cleaner**

```python
expires_at = created_at + timedelta(hours=168)
rows = connection.execute(
    HISTORY_PAGE_SQL,
    (user_id, now_text, before_created_at, before_id, limit + 1),
).fetchall()
next_cursor = _cursor_from(rows[limit - 1]) if len(rows) > limit else None
```

Bind owner/history ID into both cipher AAD purposes, enforce `1..100`, delete expired rows before visibility, and checkpoint after hourly physical cleanup.

- [ ] **9C.4 Run full storage GREEN and plaintext scan**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage -q
```

- [ ] **9C.5 Commit**

```sh
git add apps/travel-map/app/storage/history.py \
  apps/travel-map/app/storage/retention.py \
  apps/travel-map/tests/storage/test_history.py \
  apps/travel-map/tests/storage/test_retention.py
git commit -m "feat: add seven-day encrypted history retention"
```

**Exhaustive Task 9 acceptance matrix (assign each case to 9A–9C):**

```text
test_v1_migration_is_transactional_and_idempotent
test_runtime_rejects_missing_or_future_schema
test_database_enables_required_pragmas_on_every_connection
test_user_delete_cascades_sessions_history_and_settings
test_atomic_session_issue_reuses_subject_user_updates_last_login_and_prevents_orphan_race
test_history_is_visible_one_microsecond_before_168_hours
test_history_is_hidden_and_physically_deleted_at_exactly_168_hours
test_trip_start_and_end_times_cannot_change_history_retention
test_history_keyset_pagination_exposes_every_valid_row_beyond_one_hundred
test_history_repository_rejects_bool_zero_and_limits_over_one_hundred
test_mixed_precision_or_offset_timestamps_are_rejected_before_lexical_queries
test_history_draft_excludes_coordinate_vehicle_fuel_parking_and_other_trip_inputs
test_history_payload_and_settings_are_ciphertext_at_rest
test_retention_cleanup_checkpoints_and_truncates_wal
test_database_directory_db_wal_and_shm_have_private_modes
test_migration_cli_prints_only_schema_version_and_status
```

- [ ] **Task 9 combined verification after 9C**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/storage/test_database.py \
  apps/travel-map/tests/storage/test_history.py \
  apps/travel-map/tests/storage/test_user_settings.py \
  apps/travel-map/tests/storage/test_retention.py -q
```

- [ ] **Inspect plaintext absence after the combined GREEN**

In the test, insert a sentinel destination/address/site ID through repositories, then read raw DB bytes and query columns. The sentinel may occur only after decrypting the BLOB; it must not occur in raw database/WAL bytes.

---

### Task 10: Kakao OIDC, opaque sessions, Origin, and CSRF

**Depends on:** Tasks 1–4, 8, and 9.

**Files:**
- Create: `apps/travel-map/app/auth/__init__.py`
- Create: `apps/travel-map/app/auth/models.py`
- Create: `apps/travel-map/app/auth/oauth.py`
- Create: `apps/travel-map/app/auth/session.py`
- Create: `apps/travel-map/app/api/auth.py`
- Create: `apps/travel-map/app/api/me.py`
- Modify: `apps/travel-map/app/api/places.py`
- Modify: `apps/travel-map/app/api/trips.py`
- Modify: `apps/travel-map/app/api/__init__.py`
- Modify: `apps/travel-map/app/contracts.py`
- Modify: `apps/travel-map/app/dependencies.py`
- Modify: `apps/travel-map/app/settings.py`
- Modify: `apps/travel-map/app/api/common.py`
- Modify: `apps/travel-map/app/main.py`
- Modify: `apps/travel-map/Dockerfile`
- Create: `apps/travel-map/tests/auth/__init__.py`
- Create: `apps/travel-map/tests/auth/test_oauth.py`
- Create: `apps/travel-map/tests/auth/test_sessions.py`
- Create: `apps/travel-map/tests/api/test_auth.py`
- Modify: `apps/travel-map/tests/api/test_places.py`
- Modify: `apps/travel-map/tests/api/test_trips.py`
- Modify: `apps/travel-map/tests/providers/test_settings.py`
- Modify: `apps/travel-map/tests/security/test_rate_limit.py`
- Create: `apps/travel-map/tests/security/test_proxy_boundary.py`
- Create: `apps/travel-map/tests/security/test_user_data_safety.py`
- Modify: `apps/travel-map/tests/security/test_public_safety.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class IssuedOAuthAttempt:
    attempt_token: str
    state: str
    nonce: str
    expires_at: datetime

@dataclass(frozen=True)
class SessionPrincipal:
    user_id: int
    token_hmac: bytes
    csrf_hmac: bytes
    expires_at: datetime

@dataclass(frozen=True)
class IssuedSession:
    user_id: int
    raw_token: str
    raw_csrf: str
    expires_at: datetime

@dataclass(frozen=True)
class VerifiedSubject:
    subject_hmac: bytes

@dataclass(frozen=True)
class UserServices:
    oauth_attempts: OAuthAttemptRepository
    sessions: SessionService
    history: HistoryRepository
    settings: UserSettingsRepository
    retention_cleaner: RetentionCleaner
    oidc_client: KakaoOidcClient

class KakaoOidcClient:
    def authorization_url(self, *, state: str, nonce: str) -> str:
        raise NotImplementedError

    async def exchange_and_verify(
        self, *, code: str, expected_nonce_hash: bytes
    ) -> VerifiedSubject:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

class OAuthAttemptRepository:
    async def create(self, *, now: datetime) -> IssuedOAuthAttempt:
        raise NotImplementedError

    async def consume(
        self, *, attempt_token: str, state: str, now: datetime
    ) -> bytes:
        raise NotImplementedError

class SessionService:
    async def issue_for_subject(
        self, *, subject_hmac: bytes, now: datetime
    ) -> IssuedSession:
        raise NotImplementedError

    async def resolve(
        self, *, raw_token: str, now: datetime,
    ) -> SessionPrincipal | None:
        raise NotImplementedError

    async def verify_csrf(
        self, *, principal: SessionPrincipal, raw_csrf: str,
    ) -> bool:
        raise NotImplementedError

    async def revoke(self, *, raw_token: str) -> None:
        raise NotImplementedError

    async def revoke_all(self, *, user_id: int) -> None:
        raise NotImplementedError

    async def delete_user(self, *, principal: SessionPrincipal) -> bool:
        raise NotImplementedError
```

OIDC authorization is a `GET https://kauth.kakao.com/oauth/authorize` with exactly `response_type=code`, login-only `KAKAO_OIDC_CLIENT_ID` as `client_id`, redirect URI `https://travel.h19h19.com/auth/kakao/callback`, `scope=openid`, and cryptographic `state`/`nonce`. Token exchange is `POST https://kauth.kakao.com/oauth/token` with `Content-Type: application/x-www-form-urlencoded;charset=utf-8` and exactly `grant_type=authorization_code`, that same `client_id`, the same `redirect_uri`, one-use `code`, and `client_secret`; Kakao's advertised auth method is `client_secret_post`, so do not send HTTP Basic auth. Fetch signing keys only from `GET https://kauth.kakao.com/.well-known/jwks.json`. Pin these endpoints instead of following discovery-provided arbitrary hosts; recheck them against the [official Kakao Login REST/OIDC document](https://developers.kakao.com/docs/en/kakaologin/rest-api) when implementing.

The attempt cookie is `__Host-travel_oauth`, HttpOnly, Secure, SameSite=Lax, Path=/, Max-Age=600. Store only HMAC-SHA-256 digests of attempt/state and SHA-256 nonce digest. Reuse `SESSION_HMAC_KEY` with explicit `travel-map:oauth-attempt\0` and `travel-map:oauth-state\0` domain separators; session token inputs use a distinct `travel-map:session\0` separator. `consume()` atomically validates unexpired/unused state and marks the attempt consumed before token exchange.

The callback clears `__Host-travel_oauth` on every terminal success or failure. A provider-supplied `error` response follows the same fixed-error/no-log path and never reaches token exchange.

Every success/error response from `/auth/kakao/start` and `/auth/kakao/callback`, including 302 redirects, sets `Cache-Control: no-store` and `Pragma: no-cache` so state-bearing locations and cookie responses cannot be replayed by a browser/intermediary cache.

Token exchange uses the login-only client ID and its client secret. Verify ID token `alg=RS256`, signature against Kakao JWKS, `iss=https://kauth.kakao.com`, exact `aud`, required `sub`/`iat`/`exp`, and SHA-256 of token nonce against the consumed attempt. `KakaoOidcClient` holds `KAKAO_SUBJECT_HMAC_KEY`, HMACs the verified `sub` inside the same awaited worker that parsed it, clears raw tokens/code/sub holders, and returns only `VerifiedSubject(subject_hmac=computed_subject_hmac)`; no caller frame ever receives raw `sub`. Cache validated JWKS only for a bounded duration and refresh once on an unknown `kid`.

The callback passes `verified_subject.subject_hmac` to `SessionService.issue_for_subject()`. This is the only issuance service: it generates fresh raw session/CSRF tokens, HMACs them, and delegates once to `UserSessionRepository.upsert_user_and_insert_session()` so user upsert, `last_login_at`, and session insertion commit atomically. There is no second repository issuance API. `resolve()` copies the stored `csrf_hmac` into `SessionPrincipal`; `verify_csrf()` HMACs the raw header token and constant-time compares it to that principal field without a second owner lookup. `SessionService.delete_user(principal)` delegates to the same repository's owner-scoped cascading delete and cannot accept an arbitrary user ID from HTTP input. Raw tokens remain only in `IssuedSession` until cookie construction. Repeated login for one HMAC reuses one user while issuing an independent fresh session for that browser; “내 데이터 삭제” revokes every session through the user cascade.

Token and JWKS responses use fixed HTTPS hosts, no redirects, the configured provider timeout, and a 256 KiB response cap. Token exchange must follow the same awaitable-factory/task-consumption isolation pattern as `BoundedHttpClient`: validate non-secret fields first, extract code/client secret only inside the awaited worker, clear form/response/token holders in `finally`, and convert transport/schema/JWT failures to a fixed `OIDC_LOGIN_FAILED` error with traceback context removed. Neither callback frames nor task-creation/cancellation failures may retain plaintext code, client secret, ID token, access token, refresh token, or `sub`.

Session and CSRF token digests use HMAC-SHA-256 with `SESSION_HMAC_KEY`. Every `__Host-` cookie omits `Domain`:

```text
__Host-travel_session: Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800
__Host-travel_csrf:    Secure;           SameSite=Lax; Path=/; Max-Age=604800
```

The CSRF cookie contains only a random token; JavaScript copies it to `X-CSRF-Token`. Authenticated mutations require `Origin == PUBLIC_BASE_URL` plus matching token. `GET /api/v1/me` returns `{authenticated, sessionExpiresAt}` and `Cache-Control: no-store`; it returns `{authenticated:false}` for no/expired session. Logout and data deletion clear all three cookies.

`AppDependencies` gains an optional `user_services`. Storage open/schema failure creates an unavailable user-service boundary rather than preventing the existing public dependencies from serving calculations.

When user services are available, the FastAPI lifespan starts one `RetentionCleaner.run_forever()` task and cancels/awaits it during shutdown before closing the OIDC HTTP client. Extend `AppDependencies.aclose()` so this owned client participates in the existing close-once/failure-isolated cleanup. A cleanup database error records only a fixed safe code and retries at the next interval; cancellation propagates immediately and never becomes a storage error.

Add `client_ip(request, trusted_proxy_cidrs)` with a strict boundary: ignore `CF-Connecting-IP` unless the raw ASGI socket peer is inside one explicitly configured `/32` or `/128`; for a trusted peer require exactly one canonical global IP value, otherwise fall back to a single safe `trusted-proxy-invalid` bucket. Untrusted peers always use their socket IP even if they spoof the header. Run Uvicorn with `--no-proxy-headers` in the fixed Docker command so `X-Forwarded-For`/`Forwarded` cannot rewrite `scope["client"]` before this check. Apply the existing `FixedWindowRateLimiter` before either unauthenticated route writes or performs provider work: `auth-start` allows 10 requests per 60 seconds and `auth-callback` allows 20 per 60 seconds, keyed by that derived IP. A rejected start creates no OAuth row; a rejected callback neither consumes an attempt nor contacts token/JWKS endpoints. `OAuthAttemptRepository.create()` also rejects once 10,000 unexpired attempts exist, bounding DB growth under distributed abuse. Rate-limit keys/logs contain no state, code, nonce, cookie, or subject.

#### 10A — OAuth attempts and Kakao OIDC verification

- [ ] **10A.1 Write the primary RED test** in `tests/auth/test_oauth.py`:

```python
@pytest.mark.asyncio
async def test_login_attempt_is_single_use_hmac_only_and_expires_at_ten_minutes(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    attempts = OAuthAttemptRepository(database, hmac_key=b"a" * 32)
    issued = await attempts.create(now=now)
    nonce_hash = await attempts.consume(
        attempt_token=issued.attempt_token, state=issued.state, now=now,
    )
    assert nonce_hash == hashlib.sha256(issued.nonce.encode()).digest()
    with pytest.raises(AuthRejected, match="INVALID_OAUTH_ATTEMPT"):
        await attempts.consume(
            attempt_token=issued.attempt_token, state=issued.state, now=now,
        )
    await database.checkpoint_truncate()
    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            raw = artifact.read_bytes()
            assert issued.attempt_token.encode() not in raw
            assert issued.state.encode() not in raw
            assert issued.nonce.encode() not in raw
```

- [ ] **10A.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/auth/test_oauth.py::test_login_attempt_is_single_use_hmac_only_and_expires_at_ten_minutes -q
```

- [ ] **10A.3 Implement attempts and the OIDC verifier**

```python
async def exchange_and_verify(
    self, *, code: str, expected_nonce_hash: bytes,
) -> VerifiedSubject:
    code_holder = code
    worker: Coroutine[object, object, _OidcWorkerOutcome] | None = None
    task: asyncio.Task[_OidcWorkerOutcome] | None = None
    outcome: _OidcWorkerOutcome | None = None
    internal_failed = False
    try:
        worker = self._exchange_verify_worker(
            code=code_holder, expected_nonce_hash=expected_nonce_hash,
        )
        code = ""
        code_holder = ""
        try:
            task = asyncio.create_task(worker)
        except Exception:
            worker.close()
            worker = None
            raise
        worker = None
        outcome = await task
    except asyncio.CancelledError:
        raise
    except Exception:
        internal_failed = True
    finally:
        code = ""
        code_holder = ""
        if worker is not None:
            worker.close()
        task = None
    if internal_failed:
        raise OidcInternalError("OIDC_INTERNAL_ERROR").with_traceback(None) from None
    if outcome is None or outcome.subject is None:
        raise OidcLoginFailed("OIDC_LOGIN_FAILED").with_traceback(None) from None
    return outcome.subject

async def _exchange_verify_worker(
    self, *, code: str, expected_nonce_hash: bytes,
) -> _OidcWorkerOutcome:
    form: dict[str, str] = {}
    token_response: dict[str, object] = {}
    claims: dict[str, object] = {}
    raw_id_token: object = None
    raw_value: object = None
    client_secret = ""
    id_token = ""
    raw_subject = ""
    try:
        client_secret = self._client_secret.get_secret_value()
        form = self._token_form(code=code, client_secret=client_secret)
        token_response = await self._exchange_code(form)
        raw_id_token = token_response.pop("id_token", None)
        if not isinstance(raw_id_token, str) or not raw_id_token:
            raise OidcSchemaFailure("OIDC_SCHEMA_FAILURE")
        id_token = raw_id_token
        claims = await self._verify_rs256_id_token(
            id_token,
            issuer="https://kauth.kakao.com",
            audience=self._client_id,
            expected_nonce_hash=expected_nonce_hash,
        )
        raw_value = claims.pop("sub", None)
        if not isinstance(raw_value, str) or not raw_value:
            raise OidcSchemaFailure("OIDC_SCHEMA_FAILURE")
        raw_subject = raw_value
        subject_hmac = hmac.digest(
            self._subject_hmac_key, raw_subject.encode("utf-8"), "sha256",
        )
        return _OidcWorkerOutcome(
            subject=VerifiedSubject(subject_hmac=subject_hmac), failed=False,
        )
    except asyncio.CancelledError:
        raise
    except (OidcTransportFailure, OidcSchemaFailure, OidcJwtFailure):
        return _OidcWorkerOutcome(subject=None, failed=True)
    finally:
        code = ""
        client_secret = ""
        form.clear()
        raw_id_token = None
        raw_value = None
        raw_subject = ""
        id_token = ""
        claims.clear()
        token_response.clear()
```

`_OidcWorkerOutcome` is a private frozen record containing only `subject: VerifiedSubject | None` and `failed: bool`; it is the only normal value that crosses the task boundary. `OidcTransportFailure`, `OidcSchemaFailure`, and `OidcJwtFailure` are private fixed exception types produced at the HTTP/parse/JWT seams; only that exact family becomes `OIDC_LOGIN_FAILED`. Persist only 32-byte attempt/state/nonce digests, consume with `BEGIN IMMEDIATE`, enforce the 600-second boundary and 10,000-row active cap, and implement bounded JWKS caching with one unknown-`kid` refresh. The sensitive worker performs token HTTP, JWT parsing, nonce verification, raw-subject HMAC, and cleanup in one awaited task; it initializes every code/client-secret/form/response/token/claims/alias holder before transport work and clears all of them in `finally`, including schema, missing-`sub`, signature, issuer/audience/nonce, cancellation, and transport failures. Immediately after creating the unstarted private coroutine, the public consumer clears both of its code references before `create_task`; on creation failure it closes that coroutine to release the only remaining code-holding frame. Expected failures are raised only after their context is gone; an unexpected programming/task failure is not misreported as a login rejection but crosses after cleanup only as fixed `OIDC_INTERNAL_ERROR` for a 500/fixed-code alert. Cancellation propagates only after the same cleanup. `test_oidc_failure_clears_sensitive_response_holders_and_uses_fixed_error` supplies mutable fake response/claims mappings, suspends the worker to inspect that the public consumer is already clean, forces task-creation, transport, schema, missing-sub, JWT, unexpected, and cancellation failures, walks every retained traceback frame local, and asserts the mappings are empty, no code/client secret/token/raw subject appears in traceback locals, captured logs, or exception text, and only the appropriate fixed safe error crosses non-cancellation boundaries.

- [ ] **10A.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/auth/test_oauth.py -q
```

- [ ] **10A.5 Commit**

```sh
git add apps/travel-map/app/auth/__init__.py \
  apps/travel-map/app/auth/models.py \
  apps/travel-map/app/auth/oauth.py \
  apps/travel-map/app/settings.py \
  apps/travel-map/tests/auth/__init__.py \
  apps/travel-map/tests/auth/test_oauth.py
git commit -m "feat: verify Kakao OIDC attempts"
```

#### 10B — Opaque sessions, cookies, CSRF, and account APIs

- [ ] **10B.1 Write the primary RED test** in `tests/auth/test_sessions.py`:

```python
@pytest.mark.asyncio
async def test_session_issue_reuses_user_and_csrf_is_bound_to_that_session(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    service = SessionService(
        UserSessionRepository(database), hmac_key=b"h" * 32,
    )
    first = await service.issue_for_subject(subject_hmac=b"s" * 32, now=now)
    second = await service.issue_for_subject(
        subject_hmac=b"s" * 32, now=now + timedelta(seconds=1),
    )
    principal = await service.resolve(raw_token=first.raw_token, now=now)
    assert principal is not None
    assert first.user_id == second.user_id == principal.user_id
    assert await service.verify_csrf(
        principal=principal, raw_csrf=first.raw_csrf,
    )
    assert not await service.verify_csrf(
        principal=principal, raw_csrf=second.raw_csrf,
    )
    assert first.expires_at == now + timedelta(days=7)
```

- [ ] **10B.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/auth/test_sessions.py::test_session_issue_reuses_user_and_csrf_is_bound_to_that_session -q
```

- [ ] **10B.3 Implement the single issuance authority and routes**

```python
async def issue_for_subject(self, *, subject_hmac: bytes, now: datetime) -> IssuedSession:
    raw_token = secrets.token_urlsafe(32)
    raw_csrf = secrets.token_urlsafe(32)
    expires_at = now + timedelta(days=7)
    user = await self._repository.upsert_user_and_insert_session(
        subject_hmac=subject_hmac,
        token_hmac=self._digest(raw_token),
        csrf_hmac=self._digest(raw_csrf),
        now=now,
        expires_at=expires_at,
    )
    return IssuedSession(user.id, raw_token, raw_csrf, expires_at)

async def verify_csrf(self, *, principal: SessionPrincipal, raw_csrf: str) -> bool:
    return hmac.compare_digest(principal.csrf_hmac, self._digest(raw_csrf))
```

Add the exact top-level OAuth router, versioned session/me routers, no-store redirects, exact `__Host-` cookies, Origin+CSRF enforcement, owner-scoped data deletion, and cookie clearing. Include the OAuth router directly in `create_app()` before the static mount.

- [ ] **10B.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/auth/test_sessions.py \
  apps/travel-map/tests/api/test_auth.py -q
```

- [ ] **10B.5 Commit**

```sh
git add apps/travel-map/app/auth/session.py \
  apps/travel-map/app/api/auth.py apps/travel-map/app/api/me.py \
  apps/travel-map/app/api/__init__.py apps/travel-map/app/contracts.py \
  apps/travel-map/app/dependencies.py apps/travel-map/app/main.py \
  apps/travel-map/tests/auth/test_sessions.py \
  apps/travel-map/tests/api/test_auth.py
git commit -m "feat: add opaque same-origin sessions"
```

#### 10C — Trusted proxy, rate limits, lifespan, and public degradation

- [ ] **10C.1 Write the primary RED test** in `tests/security/test_proxy_boundary.py`:

```python
def test_only_exact_trusted_socket_peer_can_supply_cf_connecting_ip() -> None:
    forged = Request({
        "type": "http", "client": ("203.0.113.9", 40123),
        "headers": [(b"cf-connecting-ip", b"1.1.1.1")],
    })
    trusted = Request({
        "type": "http", "client": ("127.0.0.1", 40124),
        "headers": [(b"cf-connecting-ip", b"1.1.1.1")],
    })
    cidrs = (ip_network("127.0.0.1/32"),)
    assert client_ip(forged, cidrs) == "203.0.113.9"
    assert client_ip(trusted, cidrs) == "1.1.1.1"
```

- [ ] **10C.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/security/test_proxy_boundary.py::test_only_exact_trusted_socket_peer_can_supply_cf_connecting_ip -q
```

- [ ] **10C.3 Implement the availability/security envelope**

```python
def client_ip(request: Request, trusted_proxy_cidrs: tuple[IPv4Network | IPv6Network, ...]) -> str:
    peer = ip_address(request.scope["client"][0])
    if not any(peer in network for network in trusted_proxy_cidrs):
        return peer.compressed
    values = request.headers.getlist("cf-connecting-ip")
    if len(values) != 1:
        return "trusted-proxy-invalid"
    try:
        candidate = ip_address(values[0])
    except ValueError:
        return "trusted-proxy-invalid"
    return candidate.compressed if candidate.is_global else "trusted-proxy-invalid"
```

Apply auth rate limits before DB/provider work, start/cancel/await the retention task in lifespan, retry only fixed cleanup failures, make `user_services` optional, and continue public preview with `HISTORY_NOT_SAVED` when a presented session cannot be resolved because storage is unavailable. Pin Docker Uvicorn to `--no-proxy-headers`.

- [ ] **10C.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/security/test_proxy_boundary.py \
  apps/travel-map/tests/security/test_rate_limit.py \
  apps/travel-map/tests/security/test_user_data_safety.py \
  apps/travel-map/tests/security/test_public_safety.py \
  apps/travel-map/tests/providers/test_settings.py -q
```

- [ ] **10C.5 Commit**

```sh
git add apps/travel-map/app/api/common.py apps/travel-map/app/api/places.py \
  apps/travel-map/app/api/trips.py apps/travel-map/app/main.py \
  apps/travel-map/app/dependencies.py apps/travel-map/app/settings.py \
  apps/travel-map/Dockerfile apps/travel-map/tests/security \
  apps/travel-map/tests/providers/test_settings.py \
  apps/travel-map/tests/api/test_places.py apps/travel-map/tests/api/test_trips.py
git commit -m "feat: harden optional user services"
```

**Exhaustive Task 10 acceptance matrix (assign every case to 10A–10C):**

```text
test_authorization_url_has_exact_redirect_scope_state_and_nonce
test_token_exchange_uses_exact_endpoint_form_fields_and_client_secret_post
test_auth_location_uses_login_client_id_and_excludes_provider_rest_key
test_login_attempt_is_single_use_and_expires_at_ten_minutes
test_callback_rejects_bad_signature_alg_issuer_audience_expiry_iat_and_nonce
test_callback_hmacs_subject_and_never_persists_tokens_or_profile
test_oidc_client_returns_only_subject_hmac_never_raw_subject
test_oidc_failure_clears_sensitive_response_holders_and_uses_fixed_error
test_oidc_task_creation_and_unexpected_failures_use_sanitized_internal_error
test_same_verified_subject_reuses_one_user_and_updates_last_login_atomically
test_login_sets_exact_session_csrf_and_attempt_cookie_attributes
test_callback_clears_attempt_cookie_on_success_and_failure
test_auth_start_and_callback_success_and_failure_responses_are_no_store
test_session_expires_at_exactly_seven_days_without_sliding
test_mutation_rejects_missing_wrong_origin_or_csrf
test_logout_revokes_server_session_and_clears_all_cookies
test_delete_my_data_cascades_only_the_authenticated_user_and_clears_cookies
test_same_origin_session_does_not_enable_credentialed_cors
test_query_logs_worker_tracebacks_and_cancellation_exclude_code_state_nonce_token_and_sub
test_auth_start_rate_limit_returns_429_without_creating_an_attempt
test_auth_callback_rate_limit_returns_429_without_consuming_or_contacting_kakao
test_untrusted_peer_cannot_spoof_cf_connecting_ip
test_trusted_connector_uses_one_canonical_cf_connecting_ip
test_real_uvicorn_process_does_not_rewrite_socket_peer_from_forwarded_headers
test_oauth_attempt_hard_cap_bounds_unexpired_database_rows
test_user_storage_outage_keeps_anonymous_health_search_and_preview_available
test_lifespan_starts_retention_retries_after_fixed_code_error_and_cancels_on_shutdown
```

Use a test RSA key/JWKS in memory; never contact Kakao in tests.

- [ ] **Task 10 combined verification after 10C**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/auth \
  apps/travel-map/tests/api/test_auth.py \
  apps/travel-map/tests/api/test_places.py \
  apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/providers/test_settings.py \
  apps/travel-map/tests/security/test_rate_limit.py \
  apps/travel-map/tests/security/test_proxy_boundary.py \
  apps/travel-map/tests/security/test_user_data_safety.py \
  apps/travel-map/tests/security/test_public_safety.py -q
```

Route behavior is exactly:

```text
GET  /auth/kakao/start       -> 302 Kakao authorization URL
GET  /auth/kakao/callback    -> validate, issue session, 302 /
GET  /api/v1/me              -> 200 auth state, no-store
POST /api/v1/auth/logout     -> 204
DELETE /api/v1/me/data       -> 204 cascade + cookie clearing
```

`app/api/auth.py` exports two routers so prefixes cannot drift: `oauth_router = APIRouter(prefix="/auth")` owns Kakao start/callback and is included directly by `create_app()` before the static root mount; `session_router = APIRouter(prefix="/auth")` owns logout and is included inside the existing `/api/v1` router. `app/api/me.py` owns `GET /me` and `DELETE /me/data` inside the existing versioned router; Task 11 extends the same router with settings/history endpoints.

---

### Task 11: Authenticated settings, default workplace, and seven-day history APIs

**Depends on:** Tasks 1, 3, 9, and 10.

**Files:**
- Modify: `apps/travel-map/app/api/me.py`
- Modify: `apps/travel-map/app/api/__init__.py`
- Modify: `apps/travel-map/app/api/trips.py`
- Modify: `apps/travel-map/app/contracts.py`
- Modify: `apps/travel-map/app/dependencies.py`
- Modify: `apps/travel-map/app/institutions/store.py`
- Modify: `apps/travel-map/app/services/trip_preview.py`
- Modify: `apps/travel-map/tests/api/conftest.py`
- Create: `apps/travel-map/tests/api/test_me.py`
- Create: `apps/travel-map/tests/api/test_history.py`
- Create: `apps/travel-map/tests/api/test_user_settings.py`
- Modify: `apps/travel-map/tests/api/test_trips.py`
- Modify: `apps/travel-map/tests/security/test_user_data_safety.py`

**Settings contract:**

```python
class UserSettingsInput(ApiRequestModel):
    default_origin_site_id: str | None
    default_trip_pattern: TripPattern
    default_duration_minutes: Annotated[int, Field(ge=2, le=1440)]
    vehicle_use: VehicleUse
    fuel_type: FuelType
    efficiency_km_per_liter: Annotated[float, Field(ge=3.0, le=30.0)]
    parking_cost_krw: Annotated[int, Field(ge=0, le=100_000)]
    route_sort: Literal["time", "distance", "cost"]

class UserSettingsResponse(ApiModel):
    settings: UserSettingsInput
    source: Literal["DEFAULT", "SAVED"]
    resolved_default_origin: InstitutionSearchItemResponse | None
    warnings: tuple[str, ...]
```

`PUT /api/v1/me/settings` is full replacement. A non-null workplace must resolve to an active site before encryption. When no row exists, `GET` returns the exact `DEFAULT_USER_SETTINGS` from Task 9 with `source="DEFAULT"`; it never returns `settings=null`. A saved row returns `source="SAVED"`. `GET` resolves the stored ID against the current snapshot on every request; a missing/inactive site returns no `resolvedDefaultOrigin`, preserves the encrypted setting for explicit user correction, and emits `DEFAULT_ORIGIN_UNAVAILABLE`. The schema has no destination or concrete date/time fields.

**History contract:**

```python
class HistoryListItemResponse(ApiModel):
    id: str
    calculated_at: datetime
    expires_at: datetime
    origin_name: str
    destination_name: str
    trip_pattern: TripPattern
    classification: str
    allowance_status: str
    allowance_krw: int | None

class HistoryRecalculationDraftResponse(ApiModel):
    origin_site_id: str
    origin_name: str
    destination_name: str
    destination_address: str
    trip_pattern: TripPattern
    starts_at: datetime
    ends_at: datetime

class HistoryDetailResponse(ApiModel):
    item: HistoryListItemResponse
    recalculation_draft: HistoryRecalculationDraftResponse
    resolved_origin: InstitutionSearchItemResponse | None
    route_summary: tuple[HistoryRouteLegSummary, ...]
    rule_set_id: str | None
    effective_from: str | None
    warnings: tuple[str, ...]

class HistoryPageResponse(ApiModel):
    items: tuple[HistoryListItemResponse, ...]
    next_cursor: str | None
```

The encrypted draft is the exact minimal Task 9 record. It contains no destination latitude/longitude, vehicle/fuel/efficiency/parking/other-trip input, home data, route geometry, provider payload, token, profile, or raw candidate. Detail resolves `originSiteId` against the current active snapshot; a missing site returns `resolvedOrigin=null` plus `HISTORY_ORIGIN_UNAVAILABLE` and cannot authorize recalculation. “다시 계산” may restore the stored labels, pattern, and schedule, but must run a current place search and require the user to select an authoritative destination candidate before it sends a preview. Current saved settings provide vehicle assumptions; history never silently overrides them. The encrypted summary chooses each leg's representative deterministically from the route whose ID equals that leg's `best.fastestRouteId`; if no fastest route exists, it stores no representative for that leg. Provider return order or a later browser card click cannot change stored history.

API:

```text
GET    /api/v1/me/history?cursor=<opaque>&limit=50  # HTTP Field(ge=1, le=100)
GET    /api/v1/me/history/{history_id}
DELETE /api/v1/me/history/{history_id}
DELETE /api/v1/me/history
GET    /api/v1/me/settings
PUT    /api/v1/me/settings
```

All require a valid session; mutations also require Origin+CSRF. All responses set `Cache-Control: no-store`.

Authenticated preview order is exact:

1. Resolve an optional session from the opaque cookie.
2. If resolution succeeds with a valid session, require Origin+CSRF before calculation because success creates history.
3. If a session cookie exists but optional user storage fails during resolution, treat the request as unauthenticated, calculate publicly, append fixed `HISTORY_NOT_SAVED`, and expose no user data.
4. Calculate the public preview.
5. Return immediately for anonymous requests.
6. For authenticated requests, project the request/response into the approved minimal draft/summary, encrypt, and save it.
7. Convert only known storage/cipher availability failures to `HISTORY_NOT_SAVED`; do not swallow programming errors.

#### 11A — Authenticated settings and default workplace API

- [ ] **11A.1 Write the primary RED test** in `tests/api/test_user_settings.py`:

```python
def test_first_login_without_settings_returns_exact_non_null_defaults(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=41,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    settings = AsyncMock()
    settings.get.return_value = None
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(), sessions=sessions,
        history=AsyncMock(), settings=settings,
        retention_cleaner=AsyncMock(), oidc_client=AsyncMock(),
    )
    response = client.get(
        "/api/v1/me/settings",
        headers={"Cookie": "__Host-travel_session=session-token"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "settings": {
            "defaultOriginSiteId": None,
            "defaultTripPattern": "ROUND_TRIP",
            "defaultDurationMinutes": 300,
            "vehicleUse": "NONE",
            "fuelType": "GASOLINE",
            "efficiencyKmPerLiter": 10.0,
            "parkingCostKrw": 0,
            "routeSort": "time",
        },
        "source": "DEFAULT",
        "resolvedDefaultOrigin": None,
        "warnings": [],
    }
```

- [ ] **11A.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_user_settings.py::test_first_login_without_settings_returns_exact_non_null_defaults -q
```

- [ ] **11A.3 Implement exact default resolution and full replacement**

```python
@router.get("/me/settings", response_model=UserSettingsResponse)
async def get_settings(request: Request, response: Response) -> UserSettingsResponse:
    dependencies = dependencies_for(request)
    services = require_user_services(dependencies)
    principal = await require_session_principal(request, services)
    stored = await services.settings.get(
        user_id=principal.user_id,
    )
    value = stored or DEFAULT_USER_SETTINGS
    resolved = (
        dependencies.institutions.get_search_item(value.default_origin_site_id)
        if value.default_origin_site_id is not None else None
    )
    warnings = (
        ("DEFAULT_ORIGIN_UNAVAILABLE",)
        if value.default_origin_site_id is not None and resolved is None else ()
    )
    response.headers["Cache-Control"] = "no-store"
    return UserSettingsResponse(
        settings=UserSettingsInput.from_stored(value),
        source="SAVED" if stored is not None else "DEFAULT",
        resolved_default_origin=(
            InstitutionSearchItemResponse.from_domain(resolved)
            if resolved is not None else None
        ),
        warnings=warnings,
    )
```

`require_user_services(dependencies) -> UserServices` is the sole typed narrowing seam: it returns the concrete service bundle or raises the fixed storage-unavailable 503 without exposing an optional attribute. `require_session_principal(request, services)` accepts that concrete bundle. Every settings/history/account route captures `services` once and uses it for the full request; no code dereferences `dependencies.user_services` after a separate optional check. Add `InstitutionStore.get_search_item(site_id)` over the immutable active-site index. `PUT` performs full replacement: authenticate, require Origin+CSRF, resolve any non-null site ID from that index, map to the exact stored record, encrypt, and return the saved no-store response.

- [ ] **11A.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_user_settings.py \
  apps/travel-map/tests/api/test_me.py \
  apps/travel-map/tests/institutions/test_store.py -q
```

- [ ] **11A.5 Commit**

```sh
git add apps/travel-map/app/api/me.py apps/travel-map/app/contracts.py \
  apps/travel-map/app/institutions/store.py \
  apps/travel-map/tests/api/conftest.py apps/travel-map/tests/api/test_me.py \
  apps/travel-map/tests/api/test_user_settings.py \
  apps/travel-map/tests/institutions/test_store.py
git commit -m "feat: add encrypted workplace settings API"
```

#### 11B — Seven-day history list, detail, and deletion APIs

- [ ] **11B.1 Write the primary RED test** in `tests/api/test_history.py`:

```python
def test_history_list_is_scoped_to_authenticated_user(client: TestClient) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    history = AsyncMock()
    history.list_page.return_value = HistoryPage(items=(), next_cursor=None)
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(), sessions=sessions,
        history=history, settings=AsyncMock(),
        retention_cleaner=AsyncMock(), oidc_client=AsyncMock(),
    )
    response = client.get(
        "/api/v1/me/history",
        headers={"Cookie": "__Host-travel_session=session-token"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"items": [], "nextCursor": None}
    history.list_page.assert_awaited_once_with(
        user_id=73, before=None, limit=50,
    )
```

- [ ] **11B.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_history.py::test_history_list_is_scoped_to_authenticated_user -q
```

- [ ] **11B.3 Implement canonical cursors and owner-scoped routes**

```python
def encode_history_cursor(cursor: HistoryCursor) -> str:
    raw = json.dumps(
        {
            "createdAt": format_storage_timestamp(cursor.created_at),
            "historyId": cursor.history_id,
        },
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

def decode_history_cursor(value: str) -> HistoryCursor:
    if not value or "=" in value:
        raise ValueError("history cursor is invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        cursor = HistoryCursor(
            created_at=parse_storage_timestamp(payload["createdAt"]),
            history_id=payload["historyId"],
        )
    except (
        KeyError, TypeError, ValueError, UnicodeDecodeError,
        json.JSONDecodeError, StorageIntegrityError,
    ):
        raise ValueError("history cursor is invalid") from None
    if set(payload) != {"createdAt", "historyId"} or encode_history_cursor(cursor) != value:
        raise ValueError("history cursor is invalid")
    return cursor
```

Require the 22-character history-ID pattern, implement all four history routes, pass only `principal.user_id` to repository methods, re-resolve the stored origin on detail, require Origin+CSRF for deletes, and set no-store on every response. Invalid cursor or `limit` outside integer `1..100` returns 422 before repository access.

- [ ] **11B.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_history.py \
  apps/travel-map/tests/security/test_user_data_safety.py -q
```

- [ ] **11B.5 Commit**

```sh
git add apps/travel-map/app/api/me.py apps/travel-map/app/contracts.py \
  apps/travel-map/tests/api/test_history.py \
  apps/travel-map/tests/security/test_user_data_safety.py
git commit -m "feat: add owner-scoped seven-day history API"
```

#### 11C — Optional authenticated preview persistence

- [ ] **11C.1 Write the primary RED test** in `tests/api/test_trips.py`:

```python
def test_authenticated_preview_writes_only_minimal_history_draft(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=hmac.digest(b"h" * 32, b"csrf-token", "sha256"),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    sessions.verify_csrf.return_value = True
    history = AsyncMock()
    history.create.return_value = HistoryMetadata(
        id="A" * 22, user_id=73,
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
        expires_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(), sessions=sessions,
        history=history, settings=AsyncMock(),
        retention_cleaner=AsyncMock(), oidc_client=AsyncMock(),
    )
    response = client.post(
        "/api/v1/trips/preview", json=trip_payload(),
        headers={
            "Cookie": "__Host-travel_session=session-token; __Host-travel_csrf=csrf-token",
            "Origin": "https://travel.example.test",
            "X-CSRF-Token": "csrf-token",
        },
    )
    assert response.status_code == 200
    draft = history.create.await_args.kwargs["draft"]
    assert history.create.await_args.kwargs["user_id"] == 73
    assert set(type(draft).__dataclass_fields__) == {
        "origin_site_id", "origin_name", "destination_name",
        "destination_address", "trip_pattern", "starts_at", "ends_at",
    }
```

- [ ] **11C.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_trips.py::test_authenticated_preview_writes_only_minimal_history_draft -q
```

- [ ] **11C.3 Implement deterministic projection and optional saving**

```python
def project_history_records(
    request: TripPreviewRequest, response: TripPreviewResponse,
) -> tuple[HistoryRecalculationDraft, HistorySummary]:
    representatives = []
    for leg in response.route_legs:
        fastest = next(
            (route for route in leg.routes if route.id == leg.best.fastest_route_id),
            None,
        )
        if fastest is not None:
            representatives.append(HistoryRouteLegSummary(
                direction=leg.direction, mode=fastest.mode,
                duration_seconds=fastest.duration_seconds,
                distance_meters=fastest.distance_meters,
                mobility_cost_krw=fastest.mobility_cost_krw,
            ))
    return HistoryRecalculationDraft(
        origin_site_id=request.origin_site_id,
        origin_name=response.origin.name,
        destination_name=request.destination.name,
        destination_address=request.destination.address,
        trip_pattern=request.trip_pattern,
        starts_at=request.starts_at,
        ends_at=request.ends_at,
    ), HistorySummary(
        classification=response.classification.value,
        allowance_status=response.allowance.status,
        allowance_krw=response.allowance.amount_krw,
        route_legs=tuple(representatives),
        rule_set_id=response.rule_set_id,
        effective_from=response.effective_from,
    )
```

Resolve an optional session before calculation; if valid require Origin+CSRF. Calculate publicly next. Return immediately when anonymous. On a valid session, project only this minimal draft/summary and save with the principal's user ID. Convert only `UserDataUnavailableError` during resolution/save to fixed `HISTORY_NOT_SAVED`; never catch broad `Exception` or expose user data after resolution failure.

- [ ] **11C.4 Run focused GREEN and degradation coverage**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/api/test_history.py \
  apps/travel-map/tests/api/test_user_settings.py \
  apps/travel-map/tests/security/test_user_data_safety.py -q
```

- [ ] **11C.5 Commit**

```sh
git add apps/travel-map/app/api/trips.py \
  apps/travel-map/app/services/trip_preview.py \
  apps/travel-map/app/dependencies.py \
  apps/travel-map/tests/api/conftest.py apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/security/test_user_data_safety.py
git commit -m "feat: persist optional encrypted preview history"
```

**Exhaustive Task 11 acceptance matrix (assign every case to 11A–11C):**

```text
test_settings_round_trip_and_database_contains_only_ciphertext
test_first_login_without_settings_returns_exact_non_null_defaults
test_default_origin_requires_an_active_site
test_stale_default_origin_is_not_auto_applied
test_default_origin_can_be_changed_and_cleared
test_settings_reject_destination_and_concrete_dates
test_anonymous_preview_never_writes_history
test_authenticated_preview_writes_minimal_encrypted_history
test_authenticated_preview_requires_origin_and_csrf
test_history_save_failure_returns_preview_with_warning
test_history_list_detail_delete_one_and_delete_all_are_user_scoped
test_history_api_paginates_every_record_within_the_full_168_hour_window
test_history_api_rejects_zero_bool_and_limits_over_one_hundred
test_history_api_rejects_noncanonical_invalid_utf8_and_bad_timestamp_cursors
test_history_detail_contains_no_geometry_provider_payload_or_profile
test_history_summary_uses_each_leg_fastest_route_independent_of_provider_order
test_history_recalculation_draft_excludes_coordinates_and_vehicle_assumptions
test_history_detail_revalidates_origin_and_blocks_stale_site
test_session_cookie_with_resolve_failure_still_calculates_without_user_data
```

- [ ] **Task 11 combined verification after 11C**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/api/test_me.py \
  apps/travel-map/tests/api/test_history.py \
  apps/travel-map/tests/api/test_user_settings.py \
  apps/travel-map/tests/api/test_trips.py \
  apps/travel-map/tests/security/test_user_data_safety.py -q
```

The test fixture exposes `client.app.state.dependencies` only in test mode; production code continues using the existing request dependency accessor. Use Task 8's fixed `UserDataUnavailableError` solely for known storage/cipher availability and integrity failures.

---

### Task 12: Login, history, and settings panels in the anonymous-first UI

**Depends on:** Tasks 5–7, 10, and 11.

**Files:**
- Create: `apps/travel-map/app/static/auth.js`
- Create: `apps/travel-map/app/static/history.js`
- Create: `apps/travel-map/app/static/settings.js`
- Modify: `apps/travel-map/app/static/api.js`
- Modify: `apps/travel-map/app/static/app.js`
- Modify: `apps/travel-map/app/static/index.html`
- Modify: `apps/travel-map/app/static/styles.css`
- Modify: `apps/travel-map/e2e/helpers.ts`
- Create: `apps/travel-map/e2e/fixtures/me-anonymous.json`
- Create: `apps/travel-map/e2e/fixtures/me-authenticated.json`
- Create: `apps/travel-map/e2e/fixtures/history.json`
- Create: `apps/travel-map/e2e/fixtures/history-detail.json`
- Create: `apps/travel-map/e2e/fixtures/settings.json`
- Create: `apps/travel-map/e2e/auth-history-settings.spec.ts`
- Modify: `apps/travel-map/e2e/route-preview.spec.ts`

**Interfaces:**

```javascript
export function createAuthController({ api, elements, onSessionChange })
// => { initialize(), authenticated(), login(), logout(), deleteMyData(), destroy() }

export function createHistoryPanel({
  api, dialog, tripForm, destinationPicker, onDraftApplied,
})
// => {
//   initialize(), refresh(), loadNext(), openDetail(id), applyDraft(id),
//   deleteOne(id), deleteAll()
// }

export function createSettingsPanel({
  api, dialog, institutionPicker, tripForm, schedule, routeResults,
})
// => { initialize(), load(), save(), applyResolvedDefaults(), destroy() }
```

`api.js` uses `credentials: "same-origin"` and reads only the random `__Host-travel_csrf` cookie. For every same-origin POST/PUT/DELETE, it attaches `X-CSRF-Token` whenever that readable cookie exists, even before `/me` resolves; it never reads the HttpOnly session cookie. This prevents a returning user's automatic session cookie from racing public-form initialization and causing a missing-CSRF 403.

Initialization order:

1. Load public bootstrap/facets/policy.
2. Initialize anonymous search/map/form unconditionally.
3. Fetch `/api/v1/me`.
4. If authenticated, fetch settings and apply only a server-resolved active workplace plus generic defaults.
5. Never wait for auth/storage before enabling anonymous calculation.

Logged-out history/settings panels show a Kakao login explanation without disabling the main form. Logged-in history automatically refreshes after a successfully saved preview. `refresh()` resets pagination and `loadNext()` follows `nextCursor` until all still-valid records can be reached; the panel never stops at an arbitrary first 100 rows.

“다시 계산” decrypts/fetches the minimal draft and restores origin/pattern/schedule labels. It places the stored destination name/address into the destination query, runs current search, clears any authoritative destination state, and announces “출장지를 다시 선택하세요.” It must not call preview until the user explicitly selects a current candidate. It never restores a coordinate, vehicle assumption, or route geometry from history.

Settings apply every approved default: active workplace, trip pattern, duration, vehicle use, fuel type, efficiency, parking cost, and route sort. `routeResults.setSort(settings.routeSort)` controls the rendered card order for both newly calculated and already visible results. A no-row `DEFAULT` response applies the exact server defaults rather than browser-local guesses.

Replace the old unconditional “입력값을 저장하지 않습니다” footer. The new copy states: anonymous calculations are not saved; authenticated calculations save only the documented minimal encrypted history for exactly 168 hours; settings persist until “내 데이터 삭제”; and search queries, coordinates, and route geometry are never stored in history. Keep this user-facing statement free of NAS paths, key names, or administrator operations.

“내 데이터 삭제” requires an explicit confirmation dialog and then clears history/settings/session UI. It does not remove public browser assets or provider caches.

#### 12A — Optional authentication and non-blocking public bootstrap

- [ ] **12A.1 Write the primary RED Playwright test** in `e2e/auth-history-settings.spec.ts`:

```typescript
test("anonymous form works while me remains unresolved", async ({ page }) => {
  await installMockApi(page);
  let releaseMe!: () => void;
  let markMeSeen!: () => void;
  const heldMe = new Promise<void>((resolve) => { releaseMe = resolve; });
  const meSeen = new Promise<void>((resolve) => { markMeSeen = resolve; });
  await page.route("**/api/v1/me", async (route) => {
    markMeSeen();
    await heldMe;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(readFixture("me-anonymous.json")),
    });
  });
  await page.goto("/");
  await meSeen;
  try {
    await expect(page.getByLabel("출발 기관")).toBeEnabled();
    await expect(page.getByLabel("출장지")).toBeEnabled();
    await completePublicOfficialTrip(page);
    await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  } finally {
    releaseMe();
  }
});
```

- [ ] **12A.2 Run the node and record RED**

```sh
pnpm --dir apps/travel-map exec playwright test \
  e2e/auth-history-settings.spec.ts \
  -g "anonymous form works while me remains unresolved"
```

- [ ] **12A.3 Implement request/auth boundaries and startup order**

```javascript
async function request(path, options = {}) {
  const url = new URL(`/api/v1${path}`, window.location.origin);
  const method = String(options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers ?? {});
  if (url.origin === window.location.origin && ["POST", "PUT", "DELETE"].includes(method)) {
    const csrf = readCookie("__Host-travel_csrf");
    if (csrf !== null) headers.set("X-CSRF-Token", csrf);
  }
  return decodeApiResponse(await fetch(url, {
    ...options, method, headers, credentials: "same-origin",
  }));
}

async function initializeApplication() {
  await initializePublicBootstrapFacetsAndPolicy();
  initializePublicMapSearchAndForm();
  enablePublicCalculation();
  void authController.initialize();
}
```

`readCookie` decodes only the readable random CSRF cookie and returns `null` on malformed encoding. `createAuthController` renders login/account controls, redirects login to `/auth/kakao/start`, and handles logout/data deletion without ever blocking public initialization.

- [ ] **12A.4 Run focused GREEN**

```sh
pnpm --dir apps/travel-map exec playwright test \
  e2e/auth-history-settings.spec.ts \
  -g "anonymous|returning session|logged out|login state|inactive default|logout"
```

- [ ] **12A.5 Commit**

```sh
git add apps/travel-map/app/static/api.js apps/travel-map/app/static/auth.js \
  apps/travel-map/app/static/app.js apps/travel-map/app/static/index.html \
  apps/travel-map/app/static/styles.css apps/travel-map/e2e/helpers.ts \
  apps/travel-map/e2e/fixtures/me-anonymous.json \
  apps/travel-map/e2e/fixtures/me-authenticated.json \
  apps/travel-map/e2e/auth-history-settings.spec.ts
git commit -m "feat: add optional Kakao login UI"
```

#### 12B — Seven-day history and fresh-destination recalculation

- [ ] **12B.1 Write the primary RED Playwright test**:

```typescript
test("history draft requires a current destination selection", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  let previewPosts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/trips/preview") {
      previewPosts += 1;
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "다시 계산" }).first().click();
  await expect(page.getByText("출장지를 다시 선택하세요.")).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  expect(previewPosts).toBe(0);
  await page.getByRole("option", { name: /서울특별시청.*세종대로 110/ }).click();
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect.poll(() => previewPosts).toBe(1);
});

test("history detail renders the stored calculation and rule summary", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "상세 보기" }).first().click();
  const detail = page.getByRole("dialog", { name: "저장 당시 계산 상세" });
  await expect(detail.getByText("관내 출장")).toBeVisible();
  await expect(detail.getByText("20,000원")).toBeVisible();
  await expect(detail.getByText(/가는 길.*자동차.*35분.*12\.4km.*8,500원/)).toBeVisible();
  await expect(detail.getByText("2025-local-travel")).toBeVisible();
  await expect(detail.getByText("2025-01-01부터 적용")).toBeVisible();
});
```

`installAuthenticatedHistoryApi()` is added to `e2e/helpers.ts` in this unit and serves only the five named fixtures over intercepted same-origin requests.

- [ ] **12B.2 Run the node and record RED**

```sh
pnpm --dir apps/travel-map exec playwright test \
  e2e/auth-history-settings.spec.ts \
  -g "history draft requires|history detail renders"
```

- [ ] **12B.3 Implement paging and safe draft application**

```javascript
async function loadNext() {
  if (loading || nextCursor === null && loadedOnce) return;
  loading = true;
  try {
    const page = await api.history({ cursor: nextCursor, limit: 50 });
    for (const item of page.items) appendHistoryRow(rows, item);
    nextCursor = page.nextCursor;
    loadedOnce = true;
    loadMore.hidden = nextCursor === null;
  } finally {
    loading = false;
  }
}

async function applyDraft(id) {
  const detail = await api.historyDetail(id);
  const applied = await tripForm.applyRecalculationDraft(
    detail.recalculationDraft, detail.resolvedOrigin,
  );
  status.textContent = "출장지를 다시 선택하세요.";
  onDraftApplied({ ...applied, canPreview: false });
}
```

`refresh()` clears rows/cursor then calls `loadNext`; delete-one/all refresh it. `openDetail()` renders the immutable stored `item`, each representative `routeSummary` leg, `ruleSetId`, and `effectiveFrom` from `HistoryDetailResponse` in a labelled dialog before offering “다시 계산”; it never substitutes a fresh preview for the saved summary. Draft application uses the already-defined `destinationPicker.setQueryAndSearch()` path, restores no coordinates/vehicle/geometry, and cannot submit until an explicit current result is selected. Render all stored labels via `textContent`.

- [ ] **12B.4 Run focused GREEN**

```sh
pnpm --dir apps/travel-map exec playwright test \
  e2e/auth-history-settings.spec.ts -g "history|calculation refreshes"
pnpm --dir apps/travel-map exec playwright test e2e/route-preview.spec.ts
```

- [ ] **12B.5 Commit**

```sh
git add apps/travel-map/app/static/api.js apps/travel-map/app/static/history.js \
  apps/travel-map/app/static/app.js apps/travel-map/app/static/index.html \
  apps/travel-map/app/static/styles.css apps/travel-map/e2e/helpers.ts \
  apps/travel-map/e2e/fixtures/history.json \
  apps/travel-map/e2e/fixtures/history-detail.json \
  apps/travel-map/e2e/auth-history-settings.spec.ts
git commit -m "feat: add seven-day calculation history UI"
```

#### 12C — Saved settings, retention disclosure, and panel integration

- [ ] **12C.1 Write the primary RED Playwright test**:

```typescript
test("all saved settings restore and keep cost sorting active", async ({ page }) => {
  await installAuthenticatedSettingsApi(page);
  await page.goto("/");
  await expect(page.locator("#origin-selection")).toContainText("서울샘물초등학교");
  await expect(page.locator("#trip-pattern")).toHaveValue("ROUND_TRIP");
  await expect(page.locator("#duration-minutes")).toHaveValue("300");
  await expect(page.locator("#vehicle-use")).toHaveValue("PERSONAL_CAR");
  await expect(page.locator("#fuel-type")).toHaveValue("GASOLINE");
  await expect(page.locator("#efficiency")).toHaveValue("10");
  await expect(page.locator("#parking-cost")).toHaveValue("5000");
  await expect(page.getByRole("tab", { name: "비용순" })).toHaveAttribute("aria-selected", "true");
  await completePublicOfficialTrip(page);
  await expect(page.getByRole("tab", { name: "비용순" })).toHaveAttribute("aria-selected", "true");
});
```

- [ ] **12C.2 Run the node and record RED**

```sh
pnpm --dir apps/travel-map exec playwright test \
  e2e/auth-history-settings.spec.ts -g "all saved settings restore"
```

- [ ] **12C.3 Implement exact settings application**

```javascript
async function load() {
  current = await api.settings();
  tripForm.applySettings(current.settings, current.resolvedDefaultOrigin);
  routeResults.setSort(current.settings.routeSort);
  populateSettingsDialog(dialog, current);
  return current;
}

async function save() {
  current = await api.replaceSettings(settingsPayloadFrom(dialog));
  return load();
}
```

`settingsPayloadFrom` enumerates exactly the eight approved fields and converts numbers explicitly. Use labelled native dialogs, Escape/focus restoration, explicit confirmation for data deletion, and the exact public retention copy; use no HTML sink.

- [ ] **12C.4 Run the complete map-first GREEN suite**

```sh
pnpm --dir apps/travel-map exec playwright test \
  e2e/institution-picker.spec.ts e2e/destination-picker.spec.ts \
  e2e/trip-patterns.spec.ts e2e/info-panels.spec.ts \
  e2e/auth-history-settings.spec.ts e2e/route-preview.spec.ts
```

- [ ] **12C.5 Commit**

```sh
git add apps/travel-map/app/static/api.js apps/travel-map/app/static/settings.js \
  apps/travel-map/app/static/app.js apps/travel-map/app/static/index.html \
  apps/travel-map/app/static/styles.css apps/travel-map/e2e/helpers.ts \
  apps/travel-map/e2e/fixtures/settings.json \
  apps/travel-map/e2e/auth-history-settings.spec.ts \
  apps/travel-map/e2e/route-preview.spec.ts
git commit -m "feat: add persistent travel settings UI"
```

**Exhaustive Task 12 acceptance matrix (assign every case to 12A–12C):**

```text
anonymous_user_can_search_and_calculate_with_login_panels_closed
returning_session_can_calculate_before_me_resolves_without_csrf_race
logged_out_history_and_settings_offer_optional_kakao_login
login_state_restores_an_active_default_workplace
inactive_default_workplace_is_not_auto_selected
settings_change_and_clear_default_workplace
calculation_refreshes_seven_day_history
history_load_more_reaches_records_beyond_the_first_page
history_detail_renders_stored_calculation_route_and_rule_summary
history_draft_requires_fresh_destination_selection_before_preview
history_delete_one_and_delete_all_update_the_panel
all_saved_settings_restore_and_affect_form_and_route_sort
footer_distinguishes_anonymous_and_authenticated_retention_without_admin_details
logout_keeps_anonymous_calculation_available
delete_my_data_clears_history_settings_and_session_ui
panels_support_escape_focus_return_and_mobile_full_screen
```

Mock cookies and API state in `helpers.ts`; never make live Kakao requests in E2E.

- [ ] **Task 12 combined verification after 12C**

```sh
cd apps/travel-map
pnpm exec playwright test \
  e2e/auth-history-settings.spec.ts \
  e2e/route-preview.spec.ts
```

History/settings/user-supplied labels use DOM nodes and `textContent`. Do not add an inline login callback script; the callback is server-side 302 back to `/`.

---

### Task 13: NAS `/volume2` deployment, administrator documentation, and final release gates

**Depends on:** Tasks 1–12.

**Files:**
- Modify: `apps/travel-map/Dockerfile`
- Create: `apps/travel-map/deploy/nas/compose.example.yml`
- Create: `apps/travel-map/deploy/nas/backup-excludes.txt`
- Create: `apps/travel-map/deploy/nas/migrate-user-database.sh`
- Create: `apps/travel-map/deploy/nas/publish-reviewed-image.sh`
- Create: `apps/travel-map/deploy/nas/deploy-reviewed-image.sh`
- Create: `apps/travel-map/deploy/nas/verify-backup-exclusion.sh`
- Modify: `apps/travel-map/scripts/release-gate.sh`
- Modify: `.gitignore`
- Modify: `apps/travel-map/README.md`
- Modify: `apps/travel-map/tests/test_release.py`
- Modify: `apps/travel-map/tests/security/test_user_data_safety.py`
- Modify: `.github/workflows/ci.yml`
- External admin-only update: existing Notion NAS operations page, no secrets.

The Dockerfile creates `/data` as `0700` owned by UID/GID `10001` but does not declare an anonymous `VOLUME`. Its fixed runtime command enters `umask 077` before `exec uvicorn`; it contains no interpolated environment value. The runtime root remains read-only; only the explicit bind mount is writable. Migration uses the same umask. The application refuses an existing directory/database with group/world bits and validates SQLite/WAL/SHM modes after open.

Tracked Compose example:

```yaml
services:
  travel-map:
    image: ghcr.io/h19h29-design/seoul-education-travel-map@sha256:${TRAVEL_MAP_MANIFEST_DIGEST:?set the reviewed 64-hex manifest digest}
    init: true
    restart: unless-stopped
    read_only: true
    user: "10001:10001"
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    env_file:
      - /volume1/docker/seoul-education-travel-map/runtime.env
    volumes:
      - /volume2/docker-1/seoul-education-travel-map/data:/data:rw
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001
    ports:
      - "127.0.0.1:18080:8080"
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "5"
```

Do not mount `/data` into cloudflared. Preserve the existing `init`, capability drop, `no-new-privileges`, read-only root, restart, and bounded log controls while adding only the `/volume2:/data` writable exception. The registry/repository and `@sha256:` syntax are fixed in YAML, so `/volume1/docker/seoul-education-travel-map/image.env` contains only `TRAVEL_MAP_MANIFEST_DIGEST=<64 lowercase hex>` and cannot switch to a tag or another repository. `deploy-reviewed-image.sh` is the supported mutation path: it validates the full GHCR reference, pulls and platform-checks it, preserves the previous non-secret digest, atomically rewrites only `image.env`, runs migration, then invokes the fixed Compose file with `--env-file image.env`. Docker also rejects malformed digest references. A local image/config digest is not accepted as a registry manifest digest.

Before the release build, inspect the NAS Docker daemon read-only and record its exact `linux/amd64` or `linux/arm64` platform. `publish-reviewed-image.sh` creates its own private attestation path, invokes `release-gate.sh` once for that platform, and receives the gated local tag/image ID only through that path; it accepts no arbitrary local image argument and never rebuilds. It tags/pushes that exact image to `ghcr.io/h19h29-design/seoul-education-travel-map:<git-sha>`, reads its GHCR `RepoDigest`, and requires the remote manifest `config.digest` to equal the gated local image ID before printing the immutable reference. The NAS pulls that exact reference and verifies its platform before Compose changes.

Publisher authentication and NAS authentication are deliberately different: the release operator uses a short-lived publisher credential with `write:packages` supplied only through `docker login ghcr.io --username h19h29-design --password-stdin`; the NAS credential store uses a separate token with only `read:packages`. Neither token appears in Compose, script arguments, or logs. Architecture, push, config-digest equality, remote inspection, or NAS pull mismatch blocks deployment.

`backup-excludes.txt` excludes the exact data directory plus `*.sqlite3`, `*.sqlite3-wal`, and `*.sqlite3-shm` from the active NAS backup job whose absolute destination is supplied to the verifier. Root `.gitignore` rejects those three SQLite filename forms anywhere in a checkout.

`migrate-user-database.sh` accepts the immutable GHCR digest reference as its only positional argument, validates the exact host directory, and starts a one-shot container as UID 10001 with `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`, a private `noexec,nosuid,nodev` `/tmp` tmpfs, and only `/data` writable. It enters `umask 077`, runs `migrate` then `verify`, and prints only safe status/version. It never sources or echoes `runtime.env`; Compose provides secrets to the normal app, while migration needs only the DB path.

`verify-backup-exclusion.sh --job-config <absolute-file> --backup-root <absolute-dir>` is read-only. It requires the active NAS backup job configuration to reference this exact checked-out `backup-excludes.txt`, requires the exclusion file to name the exact `/volume2/docker-1/seoul-education-travel-map/data/` directory and SQLite/WAL/SHM patterns, and fails if the inspected backup destination contains `travel-map.sqlite3`, `travel-map.sqlite3-wal`, or `travel-map.sqlite3-shm`. A tracked exclusion file alone is not acceptance evidence. If the active Synology job cannot expose a readable config/dry-run, deployment remains blocked until the administrator supplies an equivalent secret-free UI export and backup-destination file listing; record only paths/status in the private task report.

Administrator README/Notion content must include:

- data location `/volume2/docker-1/seoul-education-travel-map/data/travel-map.sqlite3`;
- image/Compose on `/volume1` and DB-only bind mount on `/volume2`;
- directory `0700`, DB `0600`, owner `10001:10001`;
- migration-before-container-swap and rollback procedure;
- SHA-256-checked atomic installation of the five tracked NAS assets, plus the hard block on deployment until a valid immutable current-image rollback digest exists;
- login-only Kakao application, `KAKAO_OIDC_CLIENT_ID` distinct from the provider REST key, OIDC/client-secret, and redirect URI configuration;
- observed Cloudflare connector socket peer and the exact `/32` or `/128` `TRUSTED_PROXY_CIDRS` value, with a spoofed-header rejection check;
- secret variable names, never values;
- key lifetime rules: generate `KAKAO_SUBJECT_HMAC_KEY` and `DATA_ENCRYPTION_KEY_V1` once, preserve them across routine deploy/rollback, and never replace either without an explicit reviewed identity/re-encryption migration; losing/changing the subject key disconnects existing users, while losing/changing the data key makes settings/history undecryptable. Rotating `SESSION_HMAC_KEY` is allowed only as an announced all-session logout;
- exact 168-hour history and settings-until-delete behavior;
- backup exclusion and the consequence that settings/history are intentionally not disaster-restored;
- anonymous calculation and storage-degradation smoke checks.

`travel.h19h19.com` is the only public origin and Kakao redirect/domain. Remove the legacy Synology hostname from production `ALLOWED_HOSTS`, `ALLOWED_ORIGINS`, user-facing text, and rollback instructions. Rollback swaps the previous app image behind the same Cloudflare route; it never publishes a Synology URL.

Do not put these administrator internals in the public “사용안내” panel.

#### 13A — Docker, Compose, private migration, and file modes

- [ ] **13A.1 Write the primary RED test** in `tests/test_release.py`:

```python
def test_nas_runtime_has_one_writable_mount_and_hardened_migration() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/nas/compose.example.yml").read_text(encoding="utf-8")
    migration = (ROOT / "deploy/nas/migrate-user-database.sh").read_text(encoding="utf-8")
    assert "install -d -m 0700 -o appuser -g appuser /data" in dockerfile
    assert "VOLUME" not in dockerfile
    assert "umask 077; exec uvicorn" in dockerfile
    assert "--no-proxy-headers" in dockerfile
    assert "image: ghcr.io/h19h29-design/seoul-education-travel-map@sha256:${TRAVEL_MAP_MANIFEST_DIGEST:" in compose
    assert "/volume2/docker-1/seoul-education-travel-map/data:/data:rw" in compose
    assert "read_only: true" in compose
    assert 'user: "10001:10001"' in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges"):
        assert flag in migration
    assert "runtime.env" not in migration
    assert os.access(ROOT / "deploy/nas/migrate-user-database.sh", os.X_OK)
```

- [ ] **13A.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py::test_nas_runtime_has_one_writable_mount_and_hardened_migration -q
```

- [ ] **13A.3 Implement the runtime boundary**

```dockerfile
RUN install -d -m 0700 -o appuser -g appuser /data
USER appuser
CMD ["/bin/sh", "-c", "umask 077; exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log --no-proxy-headers"]
```

```sh
#!/bin/sh
set -eu

[ "$#" -eq 1 ] || { printf '%s\n' 'usage: migrate-user-database.sh IMMUTABLE_GHCR_REFERENCE' >&2; exit 64; }
image=$1
data_dir=/volume2/docker-1/seoul-education-travel-map/data
prefix=${image%@sha256:*}
digest=${image##*@sha256:}
case "$image" in
  *@sha256:*@sha256:*|-*) printf '%s\n' 'BLOCKED_INVALID_IMAGE_DIGEST' >&2; exit 2 ;;
esac
case "$prefix" in
  ""|*[!A-Za-z0-9./:_-]*) printf '%s\n' 'BLOCKED_INVALID_IMAGE_DIGEST' >&2; exit 2 ;;
esac
[ "$image" = "$prefix@sha256:$digest" ] && [ "${#digest}" -eq 64 ] || {
    printf '%s\n' 'BLOCKED_INVALID_IMAGE_DIGEST' >&2
    exit 2
}
case "$digest" in *[!0-9a-f]*) printf '%s\n' 'BLOCKED_INVALID_IMAGE_DIGEST' >&2; exit 2 ;; esac
[ "$prefix" = "ghcr.io/h19h29-design/seoul-education-travel-map" ] || {
    printf '%s\n' 'BLOCKED_INVALID_IMAGE_REPOSITORY' >&2; exit 2;
}
[ -d "$data_dir" ] && [ "$(CDPATH= cd -- "$data_dir" && pwd -P)" = "$data_dir" ] || {
    printf '%s\n' 'BLOCKED_UNEXPECTED_DATA_DIRECTORY' >&2; exit 2;
}

docker run --rm --user 10001:10001 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
  --mount "type=bind,src=$data_dir,dst=/data" "$image" \
  /bin/sh -eu -c 'umask 077
    python -m app.storage.migrations migrate --database /data/travel-map.sqlite3
    exec python -m app.storage.migrations verify --database /data/travel-map.sqlite3'
```

Create the exact hardened Compose file shown above, validate the physical data path and owner/mode before `docker run`, mark the script executable in Git, and add the three SQLite filename patterns to root `.gitignore`.

- [ ] **13A.4 Run focused GREEN**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py \
  apps/travel-map/tests/security/test_user_data_safety.py \
  -k "image_prepares or nas_compose or cloudflared or migration_script or private_umask or container_db or gitignore" -q
sh -n apps/travel-map/deploy/nas/migrate-user-database.sh
```

- [ ] **13A.5 Commit**

```sh
git add apps/travel-map/Dockerfile \
  apps/travel-map/deploy/nas/compose.example.yml \
  apps/travel-map/deploy/nas/migrate-user-database.sh \
  apps/travel-map/tests/test_release.py \
  apps/travel-map/tests/security/test_user_data_safety.py .gitignore
git commit -m "ops: harden NAS user database runtime"
```

#### 13B — Active backup exclusion and administrator-only runbook

- [ ] **13B.1 Write the primary RED test** in `tests/test_release.py`:

```python
def test_backup_assets_and_admin_runbook_define_private_data_boundary() -> None:
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").read_text(encoding="utf-8").splitlines()
    verifier = (ROOT / "deploy/nas/verify-backup-exclusion.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert excludes == [
        "/volume2/docker-1/seoul-education-travel-map/data/",
        "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm",
    ]
    assert "--job-config" in verifier and "--backup-root" in verifier
    assert os.access(ROOT / "deploy/nas/verify-backup-exclusion.sh", os.X_OK)
    assert "/volume2/docker-1/seoul-education-travel-map/data/travel-map.sqlite3" in readme
    assert all(name in readme for name in (
        "KAKAO_SUBJECT_HMAC_KEY", "DATA_ENCRYPTION_KEY_V1", "SESSION_HMAC_KEY",
    ))
    assert "168시간" in readme and "travel.h19h19.com" in readme
    assert "synology.me" not in readme.lower()
```

- [ ] **13B.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py::test_backup_assets_and_admin_runbook_define_private_data_boundary -q
```

- [ ] **13B.3 Implement the exact exclusion assets and runbook**

```text
/volume2/docker-1/seoul-education-travel-map/data/
*.sqlite3
*.sqlite3-wal
*.sqlite3-shm
```

`verify-backup-exclusion.sh` parses only required absolute `--job-config` and `--backup-root` values, resolves them physically, refuses `/`, verifies the active job export contains the exact checked-out exclusion-file path, verifies all four lines above, then uses a bounded `find` to reject DB/WAL/SHM files in the backup destination. It prints only `BACKUP_EXCLUSION_OK` or one fixed `BLOCKED_*` code and is committed executable. Finalize the tracked README first, then use the Notion skill to append the same secret-free administrator section to the existing NAS page; never copy environment values.

- [ ] **13B.4 Run focused GREEN and the real read-only check**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py -k "backup or admin_docs" -q
sh -n apps/travel-map/deploy/nas/verify-backup-exclusion.sh
: "${NAS_BACKUP_JOB_EXPORT:?set the approved absolute secret-free job export}"
: "${NAS_BACKUP_DESTINATION:?set the configured absolute backup destination}"
apps/travel-map/deploy/nas/verify-backup-exclusion.sh \
  --job-config "$NAS_BACKUP_JOB_EXPORT" \
  --backup-root "$NAS_BACKUP_DESTINATION"
```

If the two real read-only artifacts are unavailable, record `BLOCKED_BACKUP_CONFIGURATION_UNVERIFIED` and do not claim deploy readiness.

- [ ] **13B.5 Commit**

```sh
git add apps/travel-map/deploy/nas/backup-excludes.txt \
  apps/travel-map/deploy/nas/verify-backup-exclusion.sh \
  apps/travel-map/README.md apps/travel-map/tests/test_release.py
git commit -m "docs: add NAS user data runbook"
```

**Exhaustive Task 13 acceptance matrix (assign every case to 13A–13D):**

```text
test_image_prepares_data_directory_for_nonroot_runtime
test_nas_compose_mounts_only_user_data_on_volume2
test_nas_compose_preserves_existing_nonroot_hardening_and_log_bounds
test_nas_compose_requires_an_immutable_reviewed_image_digest
test_reviewed_image_publish_uses_ghcr_manifest_digest_without_rebuild
test_deploy_wrapper_rejects_tag_other_registry_and_malformed_digest
test_cloudflared_has_no_user_database_mount
test_backup_excludes_database_wal_and_shm
test_backup_verifier_requires_active_job_reference_and_clean_backup_destination
test_migration_script_is_shell_safe_and_never_echoes_environment
test_migration_script_rejects_option_like_and_multi_digest_image_refs
test_migration_script_uses_networkless_readonly_capability_dropped_envelope
test_runtime_and_migration_enter_private_umask_before_sqlite_creation
test_container_db_wal_and_shm_are_uid_10001_and_not_group_or_world_readable
test_release_context_contains_no_database_runtime_env_or_user_secret
test_release_gate_is_warning_strict_and_checks_full_app_test_script_format
test_release_gate_uses_bounded_helpers_and_real_encrypted_storage
test_release_gate_builds_gated_image_for_recorded_nas_platform
test_release_gate_emits_attestation_only_after_all_checks_pass
test_gitignore_rejects_sqlite_database_wal_and_shm
test_admin_docs_name_volume2_retention_backup_and_rollback_boundaries
test_admin_docs_define_subject_encryption_and_session_key_lifetimes
test_admin_docs_require_hashed_atomic_nas_asset_install_and_existing_rollback_digest
test_admin_docs_and_runtime_example_use_only_travel_h19h19_com_publicly
test_ci_runs_every_warning_strict_release_check
```

#### 13C — Encrypted-storage sentinel and bounded image release gate

- [ ] **13C.1 Write the primary RED test** in `tests/test_release.py`:

```python
def test_release_gate_uses_bounded_helpers_and_real_encrypted_storage() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")
    assert "--cap-drop ALL --cap-add CHOWN --cap-add FOWNER" in gate
    assert "--user 10001:10001" in gate
    assert "--network none" in gate and "--read-only" in gate
    assert all(name in gate for name in (
        "PayloadCipher", "UserSettingsRepository", "HistoryRepository",
        "ENCRYPTED_STORAGE_SMOKE_OK", "BLOCKED_PLAINTEXT_IN_STORAGE",
        "RELEASE_GATE_IMAGE_RECORD", "imageId=", "gitSha=",
    ))
    assert "sudo" not in gate
```

- [ ] **13C.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py::test_release_gate_uses_bounded_helpers_and_real_encrypted_storage -q
```

- [ ] **13C.3 Implement and run release/privacy gates using only approved local artifacts**

```sh
: "${NAS_PLATFORM:?set from the read-only NAS docker architecture check}"
NAS_PLATFORM="$NAS_PLATFORM" apps/travel-map/scripts/release-gate.sh
```

The gate's setup/cleanup skeleton is fixed and contains no user-controlled command text:

```sh
gate_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-image-gate.XXXXXX")
chmod 0700 "$gate_parent"
gate_data=$gate_parent/data
(umask 077 && mkdir "$gate_data")
host_uid=$(id -u); host_gid=$(id -g)
case "$host_uid:$host_gid" in
  *[!0-9:]*) printf '%s\n' 'BLOCKED_INVALID_HOST_ID' >&2; exit 2 ;;
esac
[ "$gate_data" = "$gate_parent/data" ] || exit 2
case "$gate_data" in *,*) printf '%s\n' 'BLOCKED_UNSAFE_GATE_PATH' >&2; exit 2 ;; esac

docker run --rm --user 0:0 --network none --read-only \
  --cap-drop ALL --cap-add CHOWN --cap-add FOWNER \
  --security-opt no-new-privileges \
  --mount "type=bind,src=$gate_data,dst=/data" \
  --entrypoint /bin/sh "$gate_image" \
  -eu -c 'chown 10001:10001 /data; chmod 0700 /data'
```

The fixed in-container Python heredoc imports `PayloadCipher`, `UserSessionRepository`, `UserSettingsRepository`, and `HistoryRepository`; it inserts a generated sentinel through those repositories, decrypts the exact settings/history back, checkpoints WAL, and prints only `ENCRYPTED_STORAGE_SMOKE_OK`. Cleanup first runs the reviewed image as UID 10001 with all capabilities dropped to delete its own `/data` children, then the root chown-only helper restores the now-empty directory to `host_uid:host_gid`, after which the host removes only `gate_parent`. A failed cleanup is a gate failure, never a reason to widen capabilities or use `sudo`.

The gate may emit an image attestation only when `RELEASE_GATE_IMAGE_RECORD` is set and every preceding check has succeeded. It requires an absolute, not-yet-existing path whose basename is exactly `gated-image.record`, whose real parent is a caller-owned non-symlink directory with mode `0700`, and rejects any other target. It writes a mode-`0600` sibling temporary file, fsyncs it, and atomically renames it to the requested path. The content is exactly these four newline-terminated fields, with no extra whitespace or keys:

```text
imageTag=seoul-education-travel-map:release-gate-<40 lowercase Git SHA>
imageId=sha256:<64 lowercase hex>
platform=linux/amd64|linux/arm64
gitSha=<40 lowercase hex from git rev-parse HEAD>
```

Without `RELEASE_GATE_IMAGE_RECORD`, a successful standalone gate removes its temporary local tag before exit. With the record enabled, the publisher owns that tag only until its EXIT cleanup and removes both the release-gate tag and its temporary GHCR tag on success or failure. A failed or interrupted gate creates no record.

Modify `release-gate.sh` so its own Python invocation sets `PYTHONWARNINGS=error` and it runs both `ruff check` and `ruff format --check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts` before build. It requires `NAS_PLATFORM` to equal the read-only observed `linux/amd64` or `linux/arm64` and builds exactly once with `docker buildx build --platform "$NAS_PLATFORM" --build-arg SNAPSHOT_ID="$snapshot_id" --load --tag "$gate_image" "$context_root"`; every following check and 13D publish consumes that same local tag/image ID. After its existing exact release-context validation/tests/build, it performs these bounded image checks when Docker and the approved snapshot exist:

1. Create one private temporary host data directory and record the exact numeric host UID/GID. Reuse the just-reviewed app image as a bounded chown-only helper (no third-party/helper image): override it to UID 0 and fixed `/bin/sh`, pass `--cap-drop ALL --cap-add CHOWN --cap-add FOWNER`, keep `--read-only --network none --security-opt no-new-privileges`, and mount only the validated empty temp directory at `/data`. Its fixed command changes `/data` to exactly `10001:10001`/`0700` and exits; it never starts application code. The EXIT trap first runs a UID `10001:10001`, `--cap-drop ALL` helper to delete only its own `/data` children, then runs the root chown-only helper against the now-empty directory to restore the validated host UID/GID; the host finally removes the private parent. Reject non-numeric IDs or a temp path outside the release-gate-created parent. Do not use `sudo` or any broad host path.
2. Run the image as UID `10001:10001` with `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`, a private `noexec,nosuid,nodev` `/tmp` tmpfs, and only that directory mounted at `/data`; enter `umask 077`; execute `python -m app.storage.migrations migrate` and `verify`.
3. Under the same one-shot isolation, execute a fixed, non-user-controlled Python heredoc from `release-gate.sh` that imports the shipped cipher/repositories, inserts one test user plus minimal settings/history containing a generated test-only plaintext sentinel, reads/decrypts both records back, asserts equality, checkpoints/closes SQLite, and prints only `ENCRYPTED_STORAGE_SMOKE_OK`. It must not use raw SQL or bypass the production repository/cipher boundary.
4. Start the image detached with that same security envelope, the temporary `/data`, exact localhost/HTTPS allowlists, and freshly generated in-process non-production test credentials that are never echoed or written. No real provider/OIDC secret is used and no outbound request is possible.
5. Use `docker exec` to query `http://127.0.0.1:8080/healthz` inside the container, then inspect UID/GID/modes for the DB/WAL/SHM when present. Stop/remove the container in the trap.
6. Scan captured container logs and raw temporary DB/WAL/SHM bytes for the inserted plaintext sentinel and all generated test credential values; print only fixed PASS/BLOCKED codes. This assertion is valid only after Step 3's decrypt round-trip succeeds.

Do not claim anonymous route preview in this network-isolated image gate; the warning-strict API/Playwright suites own mocked public-flow coverage. If Docker or an approved snapshot is unavailable, record the exact fail-closed status and do not claim a deploy. Do not run live Kakao login in CI.

- [ ] **13C.4 Run focused GREEN and the release gate**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py \
  apps/travel-map/tests/security/test_user_data_safety.py \
  -k "release_context or release_gate or plaintext or container_db" -q
sh -n apps/travel-map/scripts/release-gate.sh
: "${NAS_PLATFORM:?set from the read-only NAS docker architecture check}"
NAS_PLATFORM="$NAS_PLATFORM" apps/travel-map/scripts/release-gate.sh
```

- [ ] **13C.5 Commit**

```sh
git add apps/travel-map/scripts/release-gate.sh \
  apps/travel-map/tests/test_release.py \
  apps/travel-map/tests/security/test_user_data_safety.py
git commit -m "test: add encrypted image release gate"
```

#### 13D — CI enforcement, final review, publish, mirror, and deploy

- [ ] **13D.1 Write the primary RED test** in `tests/test_release.py`:

```python
def test_ci_runs_every_warning_strict_release_check() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    publish = Path("apps/travel-map/deploy/nas/publish-reviewed-image.sh").read_text(encoding="utf-8")
    deploy = Path("apps/travel-map/deploy/nas/deploy-reviewed-image.sh").read_text(encoding="utf-8")
    normalized = " ".join(workflow.split())
    assert "PYTHONWARNINGS: error" in workflow
    assert "pytest apps/travel-map/tests -q" in normalized
    assert "ruff check apps/travel-map" in normalized
    assert "ruff format --check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts" in normalized
    assert "mypy apps/travel-map/app apps/travel-map/scripts" in normalized
    assert "pnpm --dir apps/travel-map test:e2e" in normalized
    assert "ghcr.io/h19h29-design/seoul-education-travel-map" in publish
    assert "docker build" not in publish
    assert "RepoDigests" in publish and "imagetools inspect" in publish
    assert "release-gate.sh" in publish and "RELEASE_GATE_IMAGE_RECORD" in publish
    assert "TRAVEL_MAP_MANIFEST_DIGEST" in deploy
    assert "docker pull" in deploy and "migrate-user-database.sh" in deploy
    assert "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" in deploy
    assert "docker build" not in deploy
    assert os.access(Path("apps/travel-map/deploy/nas/publish-reviewed-image.sh"), os.X_OK)
    assert os.access(Path("apps/travel-map/deploy/nas/deploy-reviewed-image.sh"), os.X_OK)
```

- [ ] **13D.2 Run the node and record RED**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/test_release.py::test_ci_runs_every_warning_strict_release_check -q
```

- [ ] **13D.3 Add the exact CI jobs**

```yaml
- name: Test with warnings as errors
  env:
    PYTHONWARNINGS: error
  run: uv run --project apps/travel-map pytest apps/travel-map/tests -q
- name: Lint and format
  run: |
    uv run --project apps/travel-map ruff check apps/travel-map
    uv run --project apps/travel-map ruff format --check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
- name: Type check
  env:
    MYPYPATH: apps/travel-map
  run: uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
- name: Browser tests
  run: pnpm --dir apps/travel-map test:e2e
```

Create `publish-reviewed-image.sh` with this single-artifact handoff after the caller has authenticated to GHCR and supplied the NAS platform discovered read-only:

```sh
#!/bin/sh
set -eu

[ "$#" -eq 2 ] || { printf '%s\n' 'usage: publish-reviewed-image.sh GIT_SHA NAS_PLATFORM' >&2; exit 64; }
git_sha=$1
nas_platform=$2
registry=ghcr.io/h19h29-design/seoul-education-travel-map
[ "${#git_sha}" -eq 40 ] || { printf '%s\n' 'BLOCKED_INVALID_PUBLISH_INPUT' >&2; exit 2; }
case "$git_sha" in *[!0-9a-f]*) printf '%s\n' 'BLOCKED_INVALID_PUBLISH_INPUT' >&2; exit 2 ;; esac
case "$nas_platform" in linux/amd64|linux/arm64) ;; *) printf '%s\n' 'BLOCKED_INVALID_PUBLISH_INPUT' >&2; exit 2 ;; esac

script_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)
travel_root=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)
record_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-publish.XXXXXX")
chmod 0700 "$record_parent"
record=$record_parent/gated-image.record
manifest=$record_parent/manifest.json
image_tag=
tagged=
cleanup_publish() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$tagged" ]; then docker image rm "$tagged" >/dev/null 2>&1 || :; fi
  if [ -n "$image_tag" ]; then docker image rm "$image_tag" >/dev/null 2>&1 || :; fi
  rm -f "$record" "$manifest"
  rmdir "$record_parent"
  exit "$status"
}
trap cleanup_publish EXIT HUP INT TERM

NAS_PLATFORM=$nas_platform RELEASE_GATE_IMAGE_RECORD=$record \
  "$travel_root/scripts/release-gate.sh" >&2
[ -f "$record" ] && [ ! -L "$record" ] || exit 2
[ "$(wc -l < "$record" | tr -d ' ')" -eq 4 ] || exit 2
image_tag=$(sed -n 's/^imageTag=//p' "$record")
image_id=$(sed -n 's/^imageId=//p' "$record")
recorded_platform=$(sed -n 's/^platform=//p' "$record")
recorded_sha=$(sed -n 's/^gitSha=//p' "$record")
[ "$recorded_platform" = "$nas_platform" ] && [ "$recorded_sha" = "$git_sha" ] || exit 2
inspected=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_tag")
[ "$inspected" = "$image_id $nas_platform" ] || exit 2

tagged=$registry:$git_sha
docker tag "$image_tag" "$tagged"
docker push "$tagged" >/dev/null
repo_digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tagged" |
  awk -v prefix="$registry@sha256:" 'index($0,prefix)==1 {print; exit}')
manifest_hex=${repo_digest#"$registry@sha256:"}
[ "$repo_digest" = "$registry@sha256:$manifest_hex" ] && [ "${#manifest_hex}" -eq 64 ] || exit 2
case "$manifest_hex" in *[!0-9a-f]*) exit 2 ;; esac
docker buildx imagetools inspect --raw "$repo_digest" > "$manifest"
remote_config=$(python3 - "$manifest" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
digest = value.get("config", {}).get("digest")
if not isinstance(digest, str):
    raise SystemExit(2)
print(digest)
PY
)
[ "$remote_config" = "$image_id" ] || { printf '%s\n' 'BLOCKED_REMOTE_IMAGE_MISMATCH' >&2; exit 2; }
docker buildx imagetools inspect "$repo_digest" >/dev/null
printf '%s\n' "$repo_digest"
```

`release-gate.sh` writes the four-line record atomically with mode `0600` only after every 13C check succeeds; the record's image tag, image ID, platform, and current Git SHA come from the just-gated process. The publish script accepts no image tag/ID argument and contains no `docker build` command.

Create `deploy-reviewed-image.sh` as the only supported Compose mutation path:

```sh
#!/bin/sh
set -eu

[ "$#" -eq 1 ] || { printf '%s\n' 'usage: deploy-reviewed-image.sh IMMUTABLE_GHCR_REFERENCE' >&2; exit 64; }
reference=$1
registry=ghcr.io/h19h29-design/seoul-education-travel-map
digest=${reference#"$registry@sha256:"}
[ "$reference" = "$registry@sha256:$digest" ] && [ "${#digest}" -eq 64 ] || exit 2
case "$digest" in *[!0-9a-f]*) exit 2 ;; esac

base=/volume1/docker/seoul-education-travel-map
compose=$base/compose.yml
migration=$base/migrate-user-database.sh
image_env=$base/image.env
previous_env=$base/previous-image.env
[ -d "$base" ] && [ "$(CDPATH= cd -- "$base" && pwd -P)" = "$base" ] || exit 2
[ -f "$compose" ] && [ ! -L "$compose" ] || exit 2
[ -f "$migration" ] && [ ! -L "$migration" ] && [ -x "$migration" ] || exit 2

validate_image_env() {
  path=$1
  [ -f "$path" ] && [ ! -L "$path" ] && [ "$(stat -c '%a' "$path")" = 600 ] || exit 2
  [ "$(wc -l < "$path" | tr -d ' ')" -eq 1 ] || exit 2
  line=$(cat "$path")
  value=${line#TRAVEL_MAP_MANIFEST_DIGEST=}
  [ "$line" = "TRAVEL_MAP_MANIFEST_DIGEST=$value" ] && [ "${#value}" -eq 64 ] || exit 2
  case "$value" in *[!0-9a-f]*) exit 2 ;; esac
}
validate_image_env "$image_env"

docker pull "$reference" >/dev/null
nas_arch=$(docker info --format '{{.Architecture}}')
case "$nas_arch" in amd64) nas_platform=linux/amd64 ;; arm64) nas_platform=linux/arm64 ;; *) exit 2 ;; esac
actual_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$reference")
[ "$actual_platform" = "$nas_platform" ] || exit 2

cp -p "$image_env" "$previous_env"
chmod 0600 "$previous_env"
validate_image_env "$previous_env"
"$migration" "$reference"
tmp=$(mktemp "$base/.image.env.XXXXXX")
trap 'rm -f "$tmp"' EXIT HUP INT TERM
chmod 0600 "$tmp"
printf 'TRAVEL_MAP_MANIFEST_DIGEST=%s\n' "$digest" > "$tmp"
mv -f "$tmp" "$image_env"
trap - EXIT HUP INT TERM
docker compose --env-file "$image_env" -f "$compose" up -d
printf '%s\n' 'DEPLOYED_REVIEWED_IMAGE'
```

Mark all four NAS scripts (`migrate`, `verify-backup-exclusion`, `publish`, and `deploy`) executable in Git. The publisher redirects the complete release-gate transcript to stderr, so its stdout contains exactly one immutable GHCR reference and can be captured without parsing test/build output. The deployment script requires a valid mode-`0600` current `image.env`; it refuses a first deploy with no immutable rollback baseline, copies and revalidates that digest as `previous-image.env` before migration, and never starts Compose without the rollback copy.

- [ ] **13D.4 Run the complete local matrix**

```sh
uv sync --project apps/travel-map --frozen --dev
PYTHONWARNINGS=error uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map
uv run --project apps/travel-map ruff format --check \
  apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
sh -n apps/travel-map/scripts/release-gate.sh
sh -n apps/travel-map/deploy/nas/publish-reviewed-image.sh
sh -n apps/travel-map/deploy/nas/deploy-reviewed-image.sh
: "${NAS_PLATFORM:?set from the read-only NAS docker architecture check}"
NAS_PLATFORM="$NAS_PLATFORM" apps/travel-map/scripts/release-gate.sh
git diff --check
```

- [ ] **13D.5 Commit**

```sh
git add .github/workflows/ci.yml \
  apps/travel-map/deploy/nas/publish-reviewed-image.sh \
  apps/travel-map/deploy/nas/deploy-reviewed-image.sh \
  apps/travel-map/tests/test_release.py
git commit -m "ci: enforce complete travel map release checks"
```

- [ ] **13D.6 Request final code and security review**

Use `superpowers:requesting-code-review` for the full branch and include at minimum these adversarial cases:

```text
late search response overwrites selected candidate
free text or stale map click authorizes calculation
unused trip direction contacts a provider or contributes cost
caller injects policyProfile or legacy returnsAt
OIDC state/nonce replay, wrong kid/issuer/audience/alg
session fixation, CSRF without exact Origin, cross-user object access
history visible at/after 168 hours
ciphertext swap across user/purpose/history ID
storage outage does not block anonymous calculation
valid session cookie plus session-store read failure still returns public calculation only
auth start/callback flooding is rejected before extra rows or token/JWKS work
DB/WAL/log/backup contains a plaintext sentinel or token
```

Fix every Critical/Important finding with a new RED test, rerun the complete matrix, and obtain a READY verdict.

- [ ] **13D.7 Merge, push, mirror, deploy, and smoke-test**

After the READY verdict and clean worktree:

1. Merge the implementation branch into `main` without rewriting published history.
2. Push `main` to the `github` remote.
3. Verify GitHub CI succeeds.
4. Verify `.github/workflows/mirror-to-gitlab.yml` mirrors the same `main` SHA to GitLab.
5. Read NAS `docker info --format '{{.Architecture}}'` without mutation and map only `amd64`→`linux/amd64` or `arm64`→`linux/arm64`. Require the existing `/volume1/docker/seoul-education-travel-map/image.env` to be a regular non-symlink mode-`0600` file containing the immutable GHCR manifest digest of the currently deployed rollback image. If this one-time rollback baseline is absent or does not resolve to the running platform, block this release and establish it in a separate READY-reviewed publication of the currently deployed source revision; never invent a digest or approve a no-rollback first deploy.
6. Supply a short-lived `write:packages` publisher credential only through `docker login --password-stdin`, then run `publish-reviewed-image.sh <40-char-main-sha> <NAS_PLATFORM>` exactly once. The script itself invokes 13C with `NAS_PLATFORM`, receives the gated attestation, pushes that exact image without rebuilding, and prints exactly one immutable GHCR reference on stdout. Capture that line and independently verify its remote manifest config digest and platform.
7. Through authenticated NAS admin SSH, create a new mode-`0700` non-symlink staging directory below `/volume1/docker/seoul-education-travel-map`; copy exactly the tracked `compose.example.yml`, `migrate-user-database.sh`, `deploy-reviewed-image.sh`, `backup-excludes.txt`, and `verify-backup-exclusion.sh` from the reviewed `main` SHA. Compare SHA-256 for every local/staged pair, reject missing/extra/symlink files, run `sh -n` on all staged scripts, then atomically install `compose.example.yml` as `compose.yml` mode `0600`, scripts mode `0700`, and the exclusion file mode `0600`. Remove only that staging directory after rehashing the installed files; do not copy or overwrite `runtime.env`, `image.env`, or `previous-image.env` in this asset step.
8. Before starting the new image, authenticate on NAS with the separate read-only packages token, pull the captured immutable reference, and verify its platform without changing the running service. Register the exact redirect URI on the login-only Kakao app. Require current `runtime.env` to be a regular non-symlink mode-`0600` file; byte-copy the entire file—including every existing provider API secret and non-auth setting—to a same-directory mode-`0600` temporary file, then add or replace only the exact Task 8 auth/storage keys through a no-echo secret input and make exactly two reviewed legacy-host changes: `ALLOWED_HOSTS=["travel.h19h19.com","127.0.0.1","localhost"]` (the latter two exist solely for internal health probes) and `ALLOWED_ORIGINS=["https://travel.h19h19.com"]`. Reject duplicate/unknown changes, reject every Synology hostname, and prove every other key/value is byte-for-byte unchanged. Validate the complete temporary environment with the already-pulled new image's settings parser under `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`; preserve the prior complete file as mode-`0600` `previous-runtime.env`, revalidate that rollback copy, and atomically rename the validated temporary file to `runtime.env`. Preserve established subject/data keys exactly; never reuse the route/local REST key or proceed without the runtime rollback copy.
9. With that read-only NAS registry session still active, invoke `/volume1/docker/seoul-education-travel-map/deploy-reviewed-image.sh <captured-immutable-reference>`. The wrapper independently pull/platform-checks that exact reference, copies and validates current `image.env` as `previous-image.env`, runs the 13A migration with the same reference, atomically writes only `TRAVEL_MAP_MANIFEST_DIGEST`, and starts the fixed Compose file. Verify health, confirm the active container digest equals the captured reference, and confirm both image/runtime rollback files still name the prior valid state.
10. Smoke-test `https://travel.h19h19.com`: anonymous institution/address search, each trip pattern, Kakao login, default workplace restoration, history create/detail/delete, logout, and anonymous calculation after logout.
11. Inspect the active NAS backup job/dry-run and confirm it consumes the exclusion file and that its destination contains no DB/WAL/SHM copy.
12. Simulate an expired history record in a disposable test DB; confirm API invisibility, physical deletion, and WAL checkpoint before considering retention complete.

Do not deploy if the migration verification, approved snapshot gate, image health check, Cloudflare route, or rollback copy is missing.

---

## Dependency graph and parallel execution

```text
Task 0 ───────────────────────────────────────────────────────────► all tasks
   │
   ▼
Task 1 ─► Task 2 ─► Task 3 ─► Task 4
   │         │         │         │
   └─────────┴────────► Task 5 ─► Task 6 ─► Task 7 ───────────────┐
                         ▲          ▲                              │
                         └──────────┴──────────────────────────────┤
                                                                  ▼
Task 8 ───────────────► 9A ─► 9B ─► 9C ──────────────────────► 10A ─► 10B ─► 10C
                          ▲                                         │
                          └──────────── Task 3                       │
                                                                    ▼
Task 1 + Task 3 + 9C + 10C ──────────────────────────────────► 11A ─► 11B ─► 11C
Task 5 + Task 6 + Task 7 + 10C + 11C ────────────────────────► 12A ─► 12B ─► 12C
Task 1–12C ──────────────────────────────────────────────────► 13A ─► 13B ─► 13C ─► 13D
```

Tasks 1→2→3→4 are serial because they share `contracts.py`, API dependencies, policy/service boundaries, and fixtures. Tasks 5→6→7 are serial because they share `app.js`, `index.html`, `styles.css`, `kakao-map.js`, and E2E helpers. Task 8 may run in parallel with Tasks 1→4; every lettered chain then executes strictly left-to-right. Task 11 waits for storage/session, Task 12 is the single UI integration barrier, and Task 13 is the sole release chain. Do not merge concurrent edits to these shared files mechanically.

## Completion evidence

The implementing task report must include:

- commit SHA for every task above;
- RED and GREEN command/output summary for every task;
- final Python/Ruff/format/mypy/Playwright/release results;
- a schema version and migration/rollback result without secret values;
- a raw DB/WAL/log/backup plaintext-sentinel scan result;
- the observed NAS platform, release-gated local image ID, matching GHCR manifest digest, and deployed NAS Compose digest;
- GitHub main SHA and matching GitLab mirror SHA;
- public URL smoke results for anonymous and authenticated flows;
- the Notion NAS runbook page URL;
- any external blocker such as Kakao console approval, missing Docker, or unavailable provider, stated without fabricating success.

## Execution handoff

Recommended: open a new Codex task from this committed branch and use `superpowers:subagent-driven-development`. The implementing task reads the approved design and this plan completely, creates an isolated `codex/map-search-login-history-implementation` worktree/branch, executes one numbered task at a time, requests review after each commit, and updates the checkboxes/evidence without placing secrets or live user data in the repository. Deployment and external NAS/Cloudflare/Notion mutations remain Task 13 and require their stated gates/authorizations.

If subagent-driven execution is unavailable, use `superpowers:executing-plans` in the new task with the same task order and review gates; do not implement the plan ad hoc in this planning task.
