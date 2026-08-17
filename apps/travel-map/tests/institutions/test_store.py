import hashlib
import json
import shutil
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from app.institutions.store import InstitutionStore, UnknownSiteError

SNAPSHOT_ROOT = Path("apps/travel-map/tests/fixtures/institutions/snapshot")


def load_store_with_verified_unclassified_school(tmp_path: Path) -> InstitutionStore:
    snapshot_root = tmp_path / "verified-unclassified"
    shutil.copytree(SNAPSHOT_ROOT, snapshot_root)
    snapshot = snapshot_root / "fixture-001"
    institution_path = snapshot / "institutions.jsonl"
    institutions = [
        json.loads(line)
        for line in institution_path.read_text(encoding="utf-8").splitlines()
    ]
    quarantined = next(
        item
        for item in institutions
        if item["institutionId"] == "test-neis:B10:REVIEW-PARENT"
    )
    quarantined.update(
        {
            "officialName": "공개 제외 평생학교",
            "institutionType": "UNCLASSIFIED_SCHOOL",
            "statusSource": "OFFICIAL_CLASSIFICATION_PENDING",
        }
    )
    institution_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in institutions
        )
        + "\n"
    ).encode()
    institution_path.write_bytes(institution_bytes)
    site_path = snapshot / "sites.jsonl"
    sites = [
        json.loads(line) for line in site_path.read_text(encoding="utf-8").splitlines()
    ]
    next(
        item for item in sites if item["siteId"] == "test-neis:B10:REVIEW-PARENT:main"
    )["status"] = "REVIEW_REQUIRED"
    site_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in sites
        )
        + "\n"
    ).encode()
    site_path.write_bytes(site_bytes)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(institution_bytes).hexdigest()
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest["countsByType"]["ELEMENTARY_SCHOOL"] -= 1
    manifest["countsByType"]["UNCLASSIFIED_SCHOOL"] = 1
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return InstitutionStore.load(snapshot_root)


def load_store_with_equal_ranked_active_sites(tmp_path: Path) -> InstitutionStore:
    snapshot_root = tmp_path / "equal-ranked-sites"
    shutil.copytree(SNAPSHOT_ROOT, snapshot_root)
    snapshot = snapshot_root / "fixture-001"
    institution_path = snapshot / "institutions.jsonl"
    site_path = snapshot / "sites.jsonl"
    institutions = [
        json.loads(line)
        for line in institution_path.read_text(encoding="utf-8").splitlines()
    ]
    sites = [
        json.loads(line) for line in site_path.read_text(encoding="utf-8").splitlines()
    ]
    base_institution = next(
        item
        for item in institutions
        if item["institutionId"] == "test-neis:B10:SEMWATER-ES"
    )
    base_site = next(
        item for item in sites if item["siteId"] == "test-neis:B10:SEMWATER-ES:main"
    )
    for number in range(21):
        suffix = f"PAGED-{number:02d}"
        institution = dict(base_institution)
        institution.update(
            {
                "institutionId": f"test-neis:B10:{suffix}",
                "officialName": f"동률학교{number:02d}",
                "aliases": [],
            }
        )
        site = dict(base_site)
        site.update(
            {
                "siteId": f"test-neis:B10:{suffix}:main",
                "institutionId": institution["institutionId"],
                "roadAddress": f"서울특별시 샘물구 동률로 {number + 1}",
            }
        )
        institutions.append(institution)
        sites.append(site)
    institution_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in institutions
        )
        + "\n"
    ).encode()
    site_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in sites
        )
        + "\n"
    ).encode()
    institution_path.write_bytes(institution_bytes)
    site_path.write_bytes(site_bytes)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(institution_bytes).hexdigest()
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest["institutionCount"] += 21
    manifest["siteCount"] += 21
    manifest["countsByType"]["ELEMENTARY_SCHOOL"] += 21
    manifest["countsByFoundation"]["PUBLIC"] += 21
    manifest["countsByStatus"]["ACTIVE"] += 21
    manifest["coordinateQualityCounts"]["ROOFTOP"] += 21
    source = manifest["sources"][0]
    source["fetchedRowCount"] += 21
    source["rowCount"] += 21
    source["normalizedRowCount"] += 21
    source["normalizedObservationDateCounts"]["2026-08-01"] += 21
    source["sourceObservationDateCounts"]["2026-08-01"] += 21
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return InstitutionStore.load(snapshot_root)


def load_store_with_main_site_name(tmp_path: Path) -> InstitutionStore:
    snapshot_root = tmp_path / "main-site-name"
    shutil.copytree(SNAPSHOT_ROOT, snapshot_root)
    snapshot = snapshot_root / "fixture-001"
    site_path = snapshot / "sites.jsonl"
    sites = [
        json.loads(line) for line in site_path.read_text(encoding="utf-8").splitlines()
    ]
    next(item for item in sites if item["siteId"] == "test-neis:B10:SEMWATER-ES:main")[
        "siteName"
    ] = "main"
    site_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in sites
        )
        + "\n"
    ).encode()
    site_path.write_bytes(site_bytes)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return InstitutionStore.load(snapshot_root)


# Production break caught: collapsing two institutions because they share an address.
def test_search_keeps_co_located_school_and_kindergarten_separate() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="샘물", limit=20)

    assert [
        (item.institution_type, item.official_name, item.site_id) for item in results
    ] == [
        (
            "KINDERGARTEN",
            "샘물초등학교병설유치원",
            "test-neis:B10:SEMWATER-KG:main",
        ),
        (
            "ELEMENTARY_SCHOOL",
            "샘물초등학교",
            "test-neis:B10:SEMWATER-ES:main",
        ),
    ]


# Production break caught: merging same-name institutions in different districts.
def test_search_keeps_same_official_name_in_two_districts_separate() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="한빛학교", limit=20)

    assert [(item.district, item.institution_id) for item in results] == [
        ("은평구", "test-neis:B10:HANBIT-EUNPYEONG"),
        ("강남구", "test-neis:B10:HANBIT-GANGNAM"),
    ]
    assert [
        item.district for item in store.search(query="한빛", district="강남구")
    ] == ["강남구"]


# Production break caught: returning one origin for a multi-site institution or
# omitting the physical-site label that lets the user distinguish the origins.
def test_search_returns_each_physical_site_with_its_site_name() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="새봄학교", limit=20)

    assert [(item.site_id, item.site_name) for item in results] == [
        ("test-neis:B10:SAEBOM:branch", "분교장"),
        ("test-neis:B10:SAEBOM:main", "본교"),
    ]


# Production mutation caught: rendering the source site token instead of the
# authoritative institution name for the sole selectable physical site.
def test_single_site_search_uses_official_name(tmp_path: Path) -> None:
    store = load_store_with_main_site_name(tmp_path)

    result = store.search(query="샘물초등학교", limit=20)

    assert result[0].official_name == "샘물초등학교"
    assert result[0].site_name == "main"
    assert result[0].display_name == "샘물초등학교"
    assert store.display_name_for_site(result[0].site_id) == "샘물초등학교"


# Production mutation caught: dropping a meaningful active branch label and
# making two selectable physical sites indistinguishable in the origin picker.
def test_multisite_headquarters_and_branch_have_distinct_display_names() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="새봄학교", limit=20)

    assert [(item.site_name, item.display_name) for item in results] == [
        ("분교장", "새봄학교 · 분교장"),
        ("본교", "새봄학교 · 본교"),
    ]


# Production mutation caught: returning browser/address coordinates instead of
# the verified active-site routing anchor attached to the selected site.
def test_search_item_exposes_only_the_verified_routing_anchor_coordinate() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    item = store.search(query="샘물초등학교", limit=20)[0]

    assert item.coordinate.latitude == 37.5501
    assert item.coordinate.longitude == 126.9801
    assert (item.coordinate.latitude, item.coordinate.longitude) != (37.55, 126.98)


def test_active_site_lookup_returns_only_current_search_item() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    active = store.search(query="샘물초등학교", limit=20)[0]

    assert store.get_search_item(active.site_id) == active
    assert store.get_search_item("test-neis:B10:CLOSED:main") is None
    assert store.get_search_item("test-neis:B10:MISSING:main") is None


# Production mutation caught: slicing before stable sorting or failing to return
# an exact next offset, which duplicates or omits equal-rank search results.
def test_search_page_has_stable_nonoverlapping_offsets_and_total(
    tmp_path: Path,
) -> None:
    store = load_store_with_equal_ranked_active_sites(tmp_path)

    first = store.search_page(query="동률학교", limit=20, offset=0)
    second = store.search_page(
        query="동률학교", limit=20, offset=first.next_offset or 0
    )

    combined = first.items + second.items
    assert first.total == 21
    assert first.next_offset == 20
    assert second.next_offset is None
    assert len(combined) == 21
    assert len({item.site_id for item in combined}) == 21
    assert [item.site_id for item in combined] == [
        item.site_id
        for item in store.search_page(query="동률학교", limit=50, offset=0).items
    ]


# Production break caught: exposing a closed or review-required origin to routing.
@pytest.mark.parametrize(
    "site_id",
    [
        "test-neis:B10:CLOSED:main",
        "test-neis:B10:MISSING:main",
        "test-neis:B10:TEMP-PARENT:main",
        "test-neis:B10:REVIEW-PARENT:main",
        "test-neis:B10:NONACTIVE-SITES:temporary",
        "test-neis:B10:NONACTIVE-SITES:review",
        "unknown:site",
    ],
)
def test_inactive_or_unknown_site_cannot_be_route_origin(site_id: str) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(
        UnknownSiteError,
        match=f"unknown or inactive institution site: {site_id}",
    ):
        store.require_site(site_id)


# Production break caught: showing an inactive site or a site whose parent is inactive.
def test_search_lists_only_active_sites_of_active_institutions() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="", limit=50)

    assert {item.site_id for item in results} == {
        "test-neis:B10:SEMWATER-KG:main",
        "test-neis:B10:SEMWATER-ES:main",
        "test-neis:B10:HANBIT-GANGNAM:main",
        "test-neis:B10:HANBIT-EUNPYEONG:main",
        "test-neis:B10:SAEBOM:main",
        "test-neis:B10:SAEBOM:branch",
    }
    assert store.search(query="폐교", limit=20) == ()
    assert store.search(query="누락", limit=20) == ()
    assert store.search(query="휴교", limit=20) == ()
    assert store.search(query="검토", limit=20) == ()
    assert store.search(query="비활성", limit=20) == ()


# Production break caught: allowing a review-required school with valid Seoul
# coordinates to re-enter the public store index by name or direct site lookup.
def test_review_required_school_is_excluded_from_every_public_store_boundary() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    quarantined_site_id = "test-neis:B10:REVIEW-PARENT:main"

    assert store.search(query="검토학교", limit=20) == ()
    assert all(
        item.site_id != quarantined_site_id
        for item in store.search(query="", institution_type="ELEMENTARY_SCHOOL")
    )
    with pytest.raises(UnknownSiteError, match="unknown or inactive institution site"):
        store.require_site(quarantined_site_id)


# The quarantine type must remain excluded even when its rows form a valid,
# signed NEIS snapshot with complete policy provenance and valid coordinates.
def test_verified_unclassified_school_is_excluded_from_every_public_store_boundary(
    tmp_path: Path,
) -> None:
    store = load_store_with_verified_unclassified_school(tmp_path)
    quarantined_site_id = "test-neis:B10:REVIEW-PARENT:main"

    assert store.search(query="공개 제외", limit=20) == ()
    assert (
        store.search(
            query="",
            institution_type="UNCLASSIFIED_SCHOOL",
            limit=20,
        )
        == ()
    )
    with pytest.raises(UnknownSiteError, match="unknown or inactive institution site"):
        store.require_site(quarantined_site_id)


# Production break caught: treating canonically equivalent Hangul, whitespace, or
# parentheses as different search text.
def test_search_normalizes_unicode_nfc_whitespace_and_parentheses() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    decomposed_query = unicodedata.normalize("NFD", " ( 새 봄 학교 ) ")

    results = store.search(query=decomposed_query, limit=20)

    assert [item.site_id for item in results] == [
        "test-neis:B10:SAEBOM:branch",
        "test-neis:B10:SAEBOM:main",
    ]


# Production break caught: indexing only the official name and dropping approved aliases.
def test_search_indexes_aliases_without_merging_institutions() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="샘물부설유치원", limit=20)

    assert [item.institution_id for item in results] == ["test-neis:B10:SEMWATER-KG"]


# Production break caught: failing Korean initial-consonant lookup.
def test_search_indexes_korean_initial_consonants() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="ㅎㅂㅎㄱ", limit=20)

    assert [(item.official_name, item.district) for item in results] == [
        ("한빛학교", "은평구"),
        ("한빛학교", "강남구"),
    ]


# Production break caught: fuzzy matching a typo into a server-approved origin.
def test_search_does_not_fuzzy_auto_match() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    assert store.search(query="샘믈", limit=20) == ()


# Production break caught: making exact site ID lookup depend on name tokenization.
def test_search_matches_exact_site_id() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="test-neis:B10:SAEBOM:branch", limit=20)

    assert [(item.site_id, item.site_name) for item in results] == [
        ("test-neis:B10:SAEBOM:branch", "분교장")
    ]


# Production break caught: returning a different ordering across repeated empty queries.
def test_empty_search_is_deterministic_by_name_then_site_id() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    expected_site_ids = [
        "test-neis:B10:SAEBOM:branch",
        "test-neis:B10:SAEBOM:main",
        "test-neis:B10:SEMWATER-ES:main",
        "test-neis:B10:SEMWATER-KG:main",
        "test-neis:B10:HANBIT-EUNPYEONG:main",
        "test-neis:B10:HANBIT-GANGNAM:main",
    ]

    assert [item.site_id for item in store.search(query="", limit=50)] == (
        expected_site_ids
    )
    assert [item.site_id for item in store.search(query="", limit=50)] == (
        expected_site_ids
    )


# Production break caught: ignoring one of the supported exact metadata filters.
@pytest.mark.parametrize(
    ("filters", "expected_site_ids"),
    [
        (
            {"institution_type": "KINDERGARTEN"},
            ["test-neis:B10:SEMWATER-KG:main"],
        ),
        (
            {"foundation_type": "PUBLIC", "district": "성동구"},
            ["test-neis:B10:SAEBOM:branch"],
        ),
        (
            {"education_office": "강남서초교육지원청"},
            ["test-neis:B10:HANBIT-GANGNAM:main"],
        ),
        (
            {"district": "샘물구", "institution_type": "ELEMENTARY_SCHOOL"},
            ["test-neis:B10:SEMWATER-ES:main"],
        ),
    ],
)
def test_search_applies_supported_filters(
    filters: dict[str, Any],
    expected_site_ids: list[str],
) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="", limit=50, **filters)

    assert [item.site_id for item in results] == expected_site_ids


# Production break caught: accepting an unbounded, zero, fractional, or boolean limit.
@pytest.mark.parametrize("limit", [0, 51, -1, True, 1.5])
def test_search_rejects_limit_outside_strict_integer_range(limit: Any) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(ValueError, match="limit must be an integer from 1 to 50"):
        store.search(query="", limit=limit)


# Production break caught: silently treating a bool/int/list filter as an ordinary
# unmatched value instead of rejecting a malformed request.
@pytest.mark.parametrize(
    "filter_name",
    ["institution_type", "foundation_type", "education_office", "district"],
)
@pytest.mark.parametrize("invalid_value", [True, 1, ["강남구"]])
def test_search_rejects_non_string_filter(
    filter_name: str,
    invalid_value: Any,
) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(
        TypeError,
        match=f"{filter_name} must be a string or None",
    ):
        store.search(query="", **{filter_name: invalid_value})


# Production break caught: leaking a raw unhashable-dict-key error from require_site
# for malformed request input.
@pytest.mark.parametrize("site_id", [None, True, 1, ["site"]])
def test_require_site_rejects_non_string_input(site_id: Any) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(TypeError, match="site_id must be a string"):
        store.require_site(site_id)


# Production break caught: require_site returning a reconstructed or user-supplied site.
def test_require_site_returns_only_the_loaded_server_approved_site() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    site = store.require_site("test-neis:B10:SAEBOM:branch")

    assert site.site_id == "test-neis:B10:SAEBOM:branch"
    assert site.site_name == "분교장"
    assert site.institution_id == "test-neis:B10:SAEBOM"
