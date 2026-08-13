import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import app.institutions.sources.kindergarten as kindergarten_module
import app.institutions.sources.neis as neis_module
import app.institutions.sources.standard_school as standard_school_module
import app.institutions.sync as sync_module
import app.providers.kakao_local as kakao_module
import httpx
import pytest
from app.institutions.models import InstitutionStatus
from app.institutions.snapshot import VerifiedSnapshot, verify_snapshot
from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    SourceFetchResult,
    SourceInstitutionRecord,
    SourceInstitutionSiteRecord,
    SourceProvenance,
    get_json_with_retry,
    normalized_records_sha256,
)
from app.institutions.sources.kindergarten import (
    KindergartenSource,
    parse_kindergarten_region_codes,
    parse_kindergarten_rows,
)
from app.institutions.sources.neis import NeisSource, parse_neis_rows
from app.institutions.sources.sen import SenCsvSource, parse_sen_csv
from app.institutions.sources.sen_counts import (
    ReportedSchoolTotal,
    ReviewedSchoolCounts,
    SchoolCountEvidence,
    load_reviewed_school_counts,
)
from app.institutions.sources.standard_school import (
    StandardSchoolLocationSource,
    enrich_neis_coordinates,
    parse_standard_school_locations,
)
from app.institutions.store import InstitutionStore
from app.institutions.sync import (
    SnapshotBuildResult,
    SnapshotQualityError,
    approve_candidate_snapshot,
    build_candidate_review_packet,
    build_candidate_snapshot,
    build_sync_preflight_audit,
    emit_sync_preflight_audit,
    enrichment_records_sha256,
    geocode_missing_records,
    reconcile_selectable_school_counts,
)
from app.policy.coverage import CoverageService
from app.providers.kakao_local import KakaoLocalClient

SOURCE_FIXTURES = Path("apps/travel-map/tests/fixtures/institutions/sources")
SOURCE_RESOURCES = Path("apps/travel-map/resources/institution-sources")
TEST_COVERAGE = CoverageService.from_geojson(
    seoul_path="apps/travel-map/resources/geodata/seoul.geojson",
    buffer_distance_m=12_000,
)


def load_json(name: str) -> dict[str, object]:
    path = SOURCE_FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8"))


def assert_secret_absent_from_app_traceback(
    error: BaseException,
    traceback_value: TracebackType | None,
    secret: str,
) -> None:
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    current = traceback_value
    while current is not None:
        frame = current.tb_frame
        if "/apps/travel-map/app/" in frame.f_code.co_filename:
            assert not _contains_secret(frame.f_locals, secret)
        current = current.tb_next


def _contains_secret(
    value: object,
    secret: str,
    *,
    seen: set[int] | None = None,
    depth: int = 0,
) -> bool:
    if seen is None:
        seen = set()
    if depth > 6 or id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, str):
        return secret in value
    if isinstance(value, bytes):
        return secret.encode() in value
    if isinstance(value, Mapping):
        return any(
            _contains_secret(item, secret, seen=seen, depth=depth + 1)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _contains_secret(item, secret, seen=seen, depth=depth + 1)
            for item in value
        )
    if isinstance(value, httpx.Request):
        return _contains_secret(
            (str(value.url), dict(value.headers)),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    if isinstance(value, httpx.Response):
        request = value.request if value.has_request else None
        return _contains_secret(
            (value.content, request),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    if type(value).__module__.startswith("app.") and hasattr(value, "__dict__"):
        return _contains_secret(
            vars(value),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    return False


# Production break caught: merging private schools or co-located kindergartens into
# another source's identity instead of preserving the official namespace.
def test_source_ids_are_namespaced_and_private_schools_are_kept() -> None:
    neis = parse_neis_rows(load_json("neis-school-info.json"))
    kinder = parse_kindergarten_rows(load_json("kindergarten-info.json"))
    sen = parse_sen_csv(SOURCE_FIXTURES / "sen-institutions.csv")

    assert {row.institution_id for row in neis} == {
        "neis:B10:7010001",
        "neis:B10:7010002",
    }
    assert {row.foundation_type for row in neis} == {"PUBLIC", "PRIVATE"}
    assert kinder[0].institution_id == "kinder:K12345678"
    assert sen[0].institution_id == "sen:headquarters"
    assert not hasattr(kinder[0], "telephone")
    assert not hasattr(kinder[0], "representative")


# Production break caught: the live NEIS type labels being dropped because the
# importer recognizes only the broad labels shown in an older implementation plan.
@pytest.mark.parametrize(
    ("source_type", "expected_type"),
    [
        ("\uc678\uad6d\uc778\ud559\uad50", "FOREIGN_SCHOOL"),
        ("\ubc29\uc1a1\ud1b5\uc2e0\uc911\ud559\uad50", "BROADCAST_SCHOOL"),
        ("\ubc29\uc1a1\ud1b5\uc2e0\uace0\ub4f1\ud559\uad50", "BROADCAST_SCHOOL"),
        ("\uac01\uc885\ud559\uad50(\ucd08)", "MISC_SCHOOL"),
        ("\uac01\uc885\ud559\uad50(\uc911)", "MISC_SCHOOL"),
        ("\uac01\uc885\ud559\uad50(\uace0)", "MISC_SCHOOL"),
        ("\uace0\ub4f1\uae30\uc220\ud559\uad50", "MISC_SCHOOL"),
        ("\ud3c9\uc0dd\ud559\uad50(\ucd08)-3\ub1446\ud559\uae30", "LIFELONG_EDUCATION_FACILITY"),
        ("\ud3c9\uc0dd\ud559\uad50(\uc911)-2\ub1446\ud559\uae30", "LIFELONG_EDUCATION_FACILITY"),
        ("\ud3c9\uc0dd\ud559\uad50(\uace0)-2\ub1446\ud559\uae30", "LIFELONG_EDUCATION_FACILITY"),
        ("\ud3c9\uc0dd\ud559\uad50(\uace0)-3\ub1446\ud559\uae30", "LIFELONG_EDUCATION_FACILITY"),
    ],
)
def test_neis_maps_every_verified_selectable_school_type(
    source_type: str,
    expected_type: str,
) -> None:
    payload = neis_payload(source_type=source_type)

    assert parse_neis_rows(payload)[0].institution_type == expected_type


@pytest.mark.parametrize(
    ("school_name", "source_type", "expected_type"),
    [
        ("꿈타래학교", "각종학교(고)", "ALTERNATIVE_EDUCATION_CENTER"),
        ("여명학교(중)", "각종학교(중)", "MISC_SCHOOL_PROGRAM"),
        ("지구촌학교 중학교", "각종학교(중)", "MISC_SCHOOL_PROGRAM"),
        ("지구촌학교 고등학교", "각종학교(고)", "MISC_SCHOOL_PROGRAM"),
    ],
)
def test_neis_preserves_noncounted_misc_programs_as_selectable_types(
    school_name: str,
    source_type: str,
    expected_type: str,
) -> None:
    payload = neis_payload(source_type=source_type)
    row = payload["schoolInfo"][1]["row"][0]  # type: ignore[index]
    row["SCHUL_NM"] = school_name

    assert parse_neis_rows(payload)[0].institution_type == expected_type


def test_neis_does_not_reclassify_other_misc_schools_by_level() -> None:
    payload = neis_payload(source_type="각종학교(중)")
    row = payload["schoolInfo"][1]["row"][0]  # type: ignore[index]
    row["SCHUL_NM"] = "국립국악중학교"

    assert parse_neis_rows(payload)[0].institution_type == "MISC_SCHOOL"


# Production break caught: publishing a training facility as a route-selectable school.
def test_neis_explicitly_excludes_nonselectable_joint_training_center() -> None:
    payload = neis_payload(source_type="\uacf5\ub3d9\uc2e4\uc2b5\uc18c")

    assert parse_neis_rows(payload) == ()


# Production break caught: assigning every parsed NEIS row the newest page vintage
# instead of the record's own raw LOAD_DTM value.
def test_neis_parse_preserves_mixed_raw_load_dates_within_one_page() -> None:
    payload = load_json("neis-school-info.json")
    rows = payload["schoolInfo"][1]["row"]  # type: ignore[index]
    rows[0]["LOAD_DTM"] = "20260809"
    rows[1]["LOAD_DTM"] = "20260810"

    assert {record.source_as_of for record in parse_neis_rows(payload)} == {
        "2026-08-09",
        "2026-08-10",
    }


# Production break caught: treating a filtered-out row as if its vintage cannot
# invalidate the raw source page.
def test_neis_validates_load_date_on_nonselectable_raw_row() -> None:
    payload = neis_payload(source_type="공동실습소")
    row = payload["schoolInfo"][1]["row"][0]  # type: ignore[index]
    row["LOAD_DTM"] = "invalid"

    with pytest.raises(SourceDataError, match="unsupported"):
        parse_neis_rows(payload)


# Production break caught: coercing a newly introduced establishment category to
# PRIVATE and silently skewing public/private totals.
def test_neis_rejects_unknown_foundation() -> None:
    payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
    row = payload["schoolInfo"][1]["row"][0]  # type: ignore[index]
    row["FOND_SC_NM"] = "\ubbf8\ud655\uc778"

    with pytest.raises(SourceDataError, match="unsupported"):
        parse_neis_rows(payload)


# Production break caught: losing live kindergarten rows because the documentation
# and documentation UI use two different exact aliases for the identifier.
def test_kindergarten_accepts_observed_lowercase_aliases() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["kindercode"] = row.pop("kinderCode")
    row["rpst_yn"] = row.pop("rpstYn")

    assert parse_kindergarten_rows(payload)[0].institution_id == "kinder:K12345678"


# Production break caught: accepting an ambiguous record whose documented and
# observed identifier aliases disagree.
def test_kindergarten_rejects_conflicting_identifier_aliases() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["kindercode"] = "DIFFERENT"

    with pytest.raises(SourceDataError, match="conflicting"):
        parse_kindergarten_rows(payload)


# Production break caught: aborting an otherwise complete disclosure round instead
# of quarantining the single live row with no coordinates.
def test_kindergarten_preserves_missing_coordinate_for_quarantine() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["lttdcdnt"] = ""
    row["lngtcdnt"] = ""

    parsed = parse_kindergarten_rows(payload)

    assert (parsed[0].latitude, parsed[0].longitude) == (None, None)
    assert parsed[0].coordinate_quality == "MISSING"


# Production break caught: silently mixing disclosure rounds when timing is omitted
# or a row contains a different official disclosure period.
def test_kindergarten_requires_one_pinned_disclosure_timing() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["pbnttmng"] = "20252"

    with pytest.raises(SourceDataError, match="timing"):
        parse_kindergarten_rows(payload, expected_timing="20261")


def test_kindergarten_region_codes_require_pinned_official_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "regions.csv"
    body = (
        "# source_url=https://e-childschoolinfo.moe.go.kr/openApi/"
        "sidoSigunguCode.do\n"
        "# source_as_of=2026-08-10\n"
        "# source_sha256="
        "94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925\n"
        "# normalized_sha256="
        "04e31dd3a83f8d58397ae24aabc894dd17530c5102826f603317a3ae8a3122c5\n"
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Source: Ministry of Education Kindergarten Info\n"
        "sido_code,sgg_code,district\n"
        "11,11110,Jongno-gu\n"
    )
    path.write_text(body, encoding="utf-8")

    regions = parse_kindergarten_region_codes(path, expected_count=1)

    assert regions == (("11", "11110", "Jongno-gu"),)


def test_kindergarten_region_codes_reject_hash_drift(tmp_path: Path) -> None:
    path = tmp_path / "regions.csv"
    path.write_text(
        "# source_url=https://e-childschoolinfo.moe.go.kr/openApi/"
        "sidoSigunguCode.do\n"
        "# source_as_of=2026-08-10\n"
        "# source_sha256=not-a-hash\n"
        "# normalized_sha256="
        "04e31dd3a83f8d58397ae24aabc894dd17530c5102826f603317a3ae8a3122c5\n"
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Source: Ministry of Education Kindergarten Info\n"
        "sido_code,sgg_code,district\n"
        "11,11110,Jongno-gu\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceDataError, match="SHA-256"):
        parse_kindergarten_region_codes(path, expected_count=1)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925",
            "f" * 64,
        ),
        ("11,11740,Gangdong-gu", "11,11999,Gangdong-gu"),
    ],
)
def test_kindergarten_region_resource_is_bound_to_reviewed_content(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source_text = (
        SOURCE_RESOURCES / "kindergarten-region-codes.csv"
    ).read_text(encoding="utf-8")
    path = tmp_path / "tampered-regions.csv"
    path.write_text(source_text.replace(old, new), encoding="utf-8")

    with pytest.raises(SourceDataError, match="reviewed|SHA-256|normalized"):
        parse_kindergarten_region_codes(path)


def test_kindergarten_region_codes_must_match_requested_timing(
    tmp_path: Path,
) -> None:
    path = write_region_fixture(tmp_path)

    with pytest.raises(SourceDataError, match="timing"):
        parse_kindergarten_region_codes(
            path,
            expected_timing="20252",
        )


def test_reviewed_sen_resource_matches_official_organization_totals() -> None:
    source = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    )

    result = source.load()

    assert len(result.records) == 41
    assert result.provenance.row_count == 41
    assert result.provenance.fetched_row_count == 42
    assert result.provenance.source_observation_date_counts == (
        ("2026-08-10", 42),
    )
    assert all(record.foundation_type == "PUBLIC" for record in result.records)
    assert all(record.source_region_code == "SEOUL" for record in result.records)
    assert all(not hasattr(record, "telephone") for record in result.records)
    gangseo = next(
        record
        for record in result.records
        if record.institution_id == "sen:gangseo-library"
    )
    assert gangseo.official_name == "강서도서관"
    assert gangseo.site_name == "본관"
    assert gangseo.road_address == "서울특별시 강서구 등촌로51나길 29"
    assert gangseo.additional_sites == (
        SourceInstitutionSiteRecord(
            site_code="gayang",
            site_name="가양관",
            road_address="서울특별시 강서구 양천로55길 46",
            district="강서구",
            latitude=None,
            longitude=None,
            coordinate_quality="MISSING",
        ),
    )
    assert not hasattr(gangseo.additional_sites[0], "telephone")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (",gayang,\\uac00\\uc591\\uad00,false", ",main,\\uac00\\uc591\\uad00,false"),
        (",gayang,\\uac00\\uc591\\uad00,false", ",gayang,\\uac00\\uc591\\uad00,true"),
    ],
)
def test_sen_multisite_parser_rejects_duplicate_or_second_default(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source = SOURCE_RESOURCES / "sen-institutions.csv"
    text = source.read_text(encoding="utf-8")
    assert old in text
    changed = text.replace(old, new, 1)
    data_lines = [
        line
        for line in changed.splitlines()
        if line.strip() and not line.startswith("# ")
    ]
    digest = hashlib.sha256(
        ("\n".join(data_lines) + "\n").encode("utf-8")
    ).hexdigest()
    changed = changed.replace(
        "c2b7e84c476175586b9f3764f54ee008fc35cb7831b4a8a0186ded9b608aac50",
        digest,
        1,
    )
    path = tmp_path / "invalid-sites.csv"
    path.write_text(changed, encoding="utf-8")

    with pytest.raises(SourceDataError, match="defaults|identifiers"):
        parse_sen_csv(path)


def test_unresolved_sen_main_and_branch_are_both_persisted_for_review(
    tmp_path: Path,
) -> None:
    result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()

    candidate = build_candidate_snapshot(
        records=result.records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="unresolved-sen-multisite",
        coverage=TEST_COVERAGE,
        source_provenance={result.provenance.source: result.provenance},
    )

    assert candidate.issues == (
        "coordinate validation success rate is below 98 percent",
    )
    institutions = [
        json.loads(line)
        for line in (candidate.candidate_path / "institutions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    sites = [
        json.loads(line)
        for line in (candidate.candidate_path / "sites.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    gangseo = next(
        item
        for item in institutions
        if item["institutionId"] == "sen:gangseo-library"
    )
    gangseo_sites = [
        item
        for item in sites
        if item["institutionId"] == "sen:gangseo-library"
    ]
    assert gangseo["status"] == "REVIEW_REQUIRED"
    assert {item["siteId"] for item in gangseo_sites} == {
        "sen:gangseo-library:main",
        "sen:gangseo-library:gayang",
    }
    assert {item["status"] for item in gangseo_sites} == {"REVIEW_REQUIRED"}
    assert all(item["latitude"] is None for item in gangseo_sites)


def test_reviewed_sen_multisite_survives_snapshot_and_store(
    tmp_path: Path,
) -> None:
    sen_result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()
    geocoded_sen = tuple(
        replace(
            record,
            latitude=37.56,
            longitude=126.97,
            coordinate_quality="GEOCODED",
            additional_sites=tuple(
                replace(
                    site,
                    latitude=37.57,
                    longitude=126.84,
                    coordinate_quality="GEOCODED",
                )
                for site in record.additional_sites
            ),
        )
        for record in sen_result.records
    )
    neis_records = tuple(
        source_record(institution_id=f"neis:B10:7011{index:03d}")
        for index in range(9)
    )
    records = geocoded_sen + neis_records
    geocoded_count = sum(
        record.coordinate_quality == "GEOCODED"
        for record in records
    ) + sum(
        site.coordinate_quality == "GEOCODED"
        for record in records
        for site in record.additional_sites
    )
    kakao = EnrichmentProvenance(
        source="KAKAO_LOCAL_GEOCODING",
        endpoint="https://dapi.kakao.com/v2/local/search/address.json",
        license_name="KAKAO_LOCAL_API_TERMS",
        attribution="Kakao Local API",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-08-10",
        raw_sha256="c" * 64,
        normalized_sha256=sync_module._geocoded_records_sha256(records),
        request_region_code="SEOUL_ADDRESS_BATCH",
        request_timing=None,
        page_count=geocoded_count,
        fetched_row_count=geocoded_count,
        matched_row_count=geocoded_count,
        matched_normalized_sha256=enrichment_records_sha256(
            records,
            "GEOCODED",
        ),
    )
    neis_provenance = source_provenance_for(neis_records)["NEIS"]
    candidate = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="reviewed-sen-multisite",
        coverage=TEST_COVERAGE,
        source_provenance={
            sen_result.provenance.source: sen_result.provenance,
            neis_provenance.source: neis_provenance,
        },
        enrichment_provenance=(kakao,),
    )

    assert candidate.issues == ()
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    district_counts = cast(dict[str, int], packet["districtCounts"])
    assert len(district_counts) == 25
    assert "가평군" not in district_counts
    assert "가평군" not in json.dumps(packet, ensure_ascii=False, sort_keys=True)
    assert "sen:agency-student" in packet["quarantinedInstitutionIds"]
    assert "sen:agency-student:main" in packet["quarantinedSiteIds"]
    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)
    store = InstitutionStore.load(tmp_path)

    matches = store.search("강서도서관")
    assert {item.site_id for item in matches} == {
        "sen:gangseo-library:main",
        "sen:gangseo-library:gayang",
    }
    assert sum(item.site_name == "본관" for item in matches) == 1
    assert store.require_site("sen:gangseo-library:gayang").site_name == "가양관"
    student = next(
        institution
        for institution in verified.institutions
        if institution.institution_id == "sen:agency-student"
    )
    student_site = next(
        site
        for site in verified.sites
        if site.site_id == "sen:agency-student:main"
    )
    assert student.status is InstitutionStatus.REVIEW_REQUIRED
    assert student_site.status is InstitutionStatus.REVIEW_REQUIRED
    assert store.search("학생교육원") == ()
    with pytest.raises(LookupError, match="unknown or inactive"):
        store.require_site(student_site.site_id)


def test_reviewed_sen_provenance_is_accepted_by_candidate_builder(
    tmp_path: Path,
) -> None:
    result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()

    candidate = build_candidate_snapshot(
        records=result.records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="sen-provenance-contract",
        coverage=TEST_COVERAGE,
        source_provenance={result.provenance.source: result.provenance},
    )

    assert candidate.issues == (
        "coordinate validation success rate is below 98 percent",
    )


def test_reviewed_sen_provenance_rejects_valid_looking_wrong_raw_digest(
    tmp_path: Path,
) -> None:
    result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()
    forged = replace(result.provenance, raw_sha256="f" * 64)

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        build_candidate_snapshot(
            records=result.records,
            previous=None,
            output_root=tmp_path,
            snapshot_id="sen-wrong-raw-digest",
            coverage=TEST_COVERAGE,
            source_provenance={forged.source: forged},
        )


def test_reviewed_school_count_resource_is_official_and_pinned() -> None:
    benchmark = load_reviewed_school_counts(
        SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    )
    assert benchmark.counts == {
        "KINDERGARTEN": 724,
        "ELEMENTARY_SCHOOL": 609,
        "MIDDLE_SCHOOL": 390,
        "HIGH_SCHOOL": 319,
        "SPECIAL_SCHOOL": 32,
        "MISC_SCHOOL": 18,
    }
    assert benchmark.category_evidence["KINDERGARTEN"].source_as_of == (
        "2026-03-10"
    )
    assert (
        benchmark.category_evidence["KINDERGARTEN"].status
        == "PRELIMINARY_2026"
    )
    assert benchmark.category_evidence["ELEMENTARY_SCHOOL"].source_as_of == (
        "2026-03-10"
    )
    assert (
        benchmark.category_evidence["ELEMENTARY_SCHOOL"].status
        == "PRELIMINARY_2026"
    )
    assert benchmark.category_composition["MISC_SCHOOL"] == (
        "각종학교17+고등기술학교1"
    )
    assert benchmark.reported_totals == (
        ReportedSchoolTotal(
            expected_count=2_092,
            population=(
                "KINDERGARTEN+ELEMENTARY_SCHOOL+MIDDLE_SCHOOL+"
                "HIGH_SCHOOL+SPECIAL_SCHOOL+MISC_SCHOOL"
            ),
            used_for_gate=False,
            evidence=SchoolCountEvidence(
                source_url=(
                    "https://enews.sen.go.kr/uploads/img_smart//"
                    "2026-06-08/20260608075519432.png"
                ),
                source_as_of="2026-03-10",
                source_sha256=(
                    "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a170"
                    "5db508afeee15e70"
                ),
                status="PRELIMINARY_2026",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("KINDERGARTEN,724,", "KINDERGARTEN,725,"),
        (
            "https://enews.sen.go.kr/uploads/img_smart//",
            "https://attacker.invalid/",
        ),
        ("PRELIMINARY_2026", "FINAL_2026"),
        (
            "36158d45a3b8c7e8a083e6d78f63fee706618f69eb49d8624877aef07e3a9332",
            "f" * 64,
        ),
    ],
)
def test_reviewed_school_count_resource_rejects_mutation(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source = SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    text = source.read_text(encoding="utf-8")
    assert old in text
    tampered = tmp_path / "sen-counts.csv"
    tampered.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(SourceDataError, match="reviewed|provenance"):
        load_reviewed_school_counts(tampered)


@pytest.mark.parametrize(
    ("institution_type", "expected", "passing_actual", "failing_actual"),
    [
        ("ELEMENTARY_SCHOOL", 609, 603, 602),
        ("MIDDLE_SCHOOL", 390, 387, 386),
        ("HIGH_SCHOOL", 318, 315, 314),
    ],
)
def test_school_reconciliation_checks_one_percent_per_category(
    institution_type: str,
    expected: int,
    passing_actual: int,
    failing_actual: int,
) -> None:
    benchmark = reviewed_counts_fixture({institution_type: expected})
    passing = reconcile_selectable_school_counts(
        records_for_type_counts({institution_type: passing_actual}),
        benchmark=benchmark,
    )
    failing = reconcile_selectable_school_counts(
        records_for_type_counts({institution_type: failing_actual}),
        benchmark=benchmark,
    )

    assert passing["categories"][institution_type]["passed"] is True
    assert passing["passed"] is True
    assert failing["categories"][institution_type]["passed"] is False
    assert failing["passed"] is False


def test_school_reconciliation_cannot_hide_swapped_category_losses() -> None:
    benchmark = reviewed_counts_fixture(
        {"ELEMENTARY_SCHOOL": 609, "MIDDLE_SCHOOL": 390}
    )
    audit = reconcile_selectable_school_counts(
        records_for_type_counts(
            {"ELEMENTARY_SCHOOL": 599, "MIDDLE_SCHOOL": 400}
        ),
        benchmark=benchmark,
    )

    assert audit["categories"]["ELEMENTARY_SCHOOL"]["passed"] is False
    assert audit["categories"]["MIDDLE_SCHOOL"]["passed"] is False
    assert audit["passed"] is False


def test_school_reconciliation_passes_reviewed_real_count_fixture() -> None:
    benchmark = load_reviewed_school_counts(
        SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    )
    audit = reconcile_selectable_school_counts(
        records_for_type_counts(benchmark.counts),
        benchmark=benchmark,
    )

    assert audit["passed"] is True
    assert audit["reportedTotals"] == [
        {
            "expectedCount": 2_092,
            "actualCount": 2_092,
            "population": (
                "KINDERGARTEN+ELEMENTARY_SCHOOL+MIDDLE_SCHOOL+"
                "HIGH_SCHOOL+SPECIAL_SCHOOL+MISC_SCHOOL"
            ),
            "usedForGate": False,
            "passed": None,
            "sourceUrl": (
                "https://enews.sen.go.kr/uploads/img_smart//"
                "2026-06-08/20260608075519432.png"
            ),
            "sourceAsOf": "2026-03-10",
            "sourceSha256": (
                "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a170"
                "5db508afeee15e70"
            ),
            "evidenceStatus": "PRELIMINARY_2026",
        }
    ]
    assert (
        audit["categories"]["KINDERGARTEN"]["sourceAsOf"]
        == "2026-03-10"
    )
    assert (
        audit["categories"]["ELEMENTARY_SCHOOL"]["sourceAsOf"]
        == "2026-03-10"
    )
    assert audit["categories"]["MISC_SCHOOL"]["composition"] == (
        "각종학교17+고등기술학교1"
    )
    assert all(
        category["deltaCount"] == 0
        for category in audit["categories"].values()
    )


# Production break caught: folding school-form lifelong-education facilities into
# the ordinary elementary/middle/high or misc school count gates.
def test_school_reconciliation_keeps_lifelong_facilities_outside_school_counts() -> None:
    benchmark = reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 1})
    records = records_for_type_counts(
        {"ELEMENTARY_SCHOOL": 1, "LIFELONG_EDUCATION_FACILITY": 18}
    )

    audit = reconcile_selectable_school_counts(records, benchmark=benchmark)

    assert audit["categories"]["ELEMENTARY_SCHOOL"]["actualCount"] == 1
    assert audit["reportedTotals"][0]["actualCount"] == 1
    assert audit["passed"] is True


def test_school_reconciliation_keeps_parallel_school_systems_outside_counts() -> None:
    benchmark = reviewed_counts_fixture(
        {"MIDDLE_SCHOOL": 1, "HIGH_SCHOOL": 1, "MISC_SCHOOL": 1}
    )
    records = records_for_type_counts(
        {
            "MIDDLE_SCHOOL": 1,
            "HIGH_SCHOOL": 1,
            "MISC_SCHOOL": 1,
            "BROADCAST_SCHOOL": 6,
            "FOREIGN_SCHOOL": 17,
        }
    )

    audit = reconcile_selectable_school_counts(records, benchmark=benchmark)

    assert audit["reportedTotals"][0]["actualCount"] == 3
    assert audit["passed"] is True


def test_school_reconciliation_rejects_actual_source_contamination() -> None:
    benchmark = reviewed_counts_fixture({"KINDERGARTEN": 1})
    contaminated = (
        replace(
            source_record(),
            institution_type="KINDERGARTEN",
            source="NEIS",
            institution_id="neis:B10:7010001",
        ),
    )

    audit = reconcile_selectable_school_counts(
        contaminated,
        benchmark=benchmark,
    )

    assert audit["categories"]["KINDERGARTEN"]["sourceValidationPassed"] is False
    assert audit["categories"]["KINDERGARTEN"]["passed"] is False
    assert audit["passed"] is False


def test_failed_reconciliation_emits_privacy_safe_audit_before_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = records_for_type_counts({"ELEMENTARY_SCHOOL": 602})
    reconciliation = reconcile_selectable_school_counts(
        records,
        benchmark=reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 609}),
    )
    provenance = source_provenance_for(records)["NEIS"]
    audit = build_sync_preflight_audit(
        records,
        source_provenance={provenance.source: provenance},
        reconciliation=reconciliation,
    )

    with pytest.raises(
        SnapshotQualityError,
        match="official school count reconciliation failed",
    ):
        emit_sync_preflight_audit(audit)

    output = capsys.readouterr()
    parsed = json.loads(output.out)
    assert parsed["auditStage"] == "PRE_PROMOTION_RECONCILIATION"
    assert parsed["passed"] is False
    assert parsed["reconciliation"]["passed"] is False
    assert parsed["typeCounts"] == {"ELEMENTARY_SCHOOL": 602}
    assert parsed["sourceCounts"]["NEIS"]["normalized"] == 602
    assert len(parsed["districtCounts"]) == 25
    assert "statusCounts" in parsed
    assert "quarantinedInstitutionIds" in parsed
    assert "quarantinedSiteIds" in parsed
    assert output.err == ""


@pytest.mark.asyncio
async def test_cli_reconciliation_failure_precedes_kakao_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location("sync_institutions_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    neis_records = records_for_type_counts({"ELEMENTARY_SCHOOL": 602})
    neis_provenance = source_provenance_for(neis_records)["NEIS"]
    empty_kindergarten_provenance = SourceProvenance(
        source="KINDERGARTEN_INFO",
        endpoint="https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do",
        license_name="PUBLIC_DATA_PORTAL_TERMS",
        attribution="Ministry of Education Kindergarten Info",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-03-10",
        raw_sha256="b" * 64,
        page_count=25,
        row_count=0,
        fetched_row_count=0,
        request_region_code="11",
        request_timing="20261",
        normalized_sha256=normalized_records_sha256(()),
    )

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeNeisSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SourceFetchResult:
            return SourceFetchResult(neis_records, neis_provenance)

    class FakeKindergartenSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SourceFetchResult:
            return SourceFetchResult((), empty_kindergarten_provenance)

    class FakeStandardSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SimpleNamespace:
            return SimpleNamespace(
                locations=(),
                provenance=standard_enrichment_provenance(matched_row_count=0),
            )

    class ForbiddenKakaoClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("Kakao must not be created before reconciliation")

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "NeisSource", FakeNeisSource)
    monkeypatch.setattr(module, "KindergartenSource", FakeKindergartenSource)
    monkeypatch.setattr(module, "StandardSchoolLocationSource", FakeStandardSource)
    monkeypatch.setattr(module, "KakaoLocalClient", ForbiddenKakaoClient)
    monkeypatch.setattr(
        module,
        "load_reviewed_school_counts",
        lambda _path: reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 609}),
    )
    snapshot_root = tmp_path / "snapshots"
    args = argparse.Namespace(
        sen_csv=SOURCE_RESOURCES / "sen-institutions.csv",
        region_codes=SOURCE_RESOURCES / "kindergarten-region-codes.csv",
        school_counts=SOURCE_RESOURCES / "sen-annual-school-counts.csv",
        snapshot_root=snapshot_root,
        geodata_root=Path("apps/travel-map/resources/geodata"),
        timing="20261",
        snapshot_id="must-not-build",
    )

    with pytest.raises(
        SnapshotQualityError,
        match="official school count reconciliation failed",
    ):
        await module._run_with_keys(
            args,
            {
                "NEIS_API_KEY": "neis-test",
                "KINDERGARTEN_API_KEY": "kindergarten-test",
                "KAKAO_REST_API_KEY": "kakao-test",
            },
            [],
        )

    audit = json.loads(capsys.readouterr().out)
    assert audit["reconciliation"]["passed"] is False
    assert audit["passed"] is False
    assert not snapshot_root.exists()


@pytest.mark.asyncio
async def test_sync_cli_stops_at_candidate_review_without_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location(
        "candidate_only_sync_cli", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def fake_run_with_keys(
        _args: argparse.Namespace,
        _keys: dict[str, str],
        _holders: list[object],
    ) -> str:
        return "cli-candidate"

    monkeypatch.setattr(module, "_run_with_keys", fake_run_with_keys)
    keys = {
        "NEIS_API_KEY": "neis-test",
        "KINDERGARTEN_API_KEY": "kindergarten-test",
        "KAKAO_REST_API_KEY": "kakao-test",
    }
    result = await module.run(argparse.Namespace(), keys)

    assert result == "cli-candidate"
    assert set(keys.values()) == {""}
    assert not hasattr(module, "promote_snapshot")
    assert not (tmp_path / "current.json").exists()


def test_no_production_script_has_automatic_snapshot_promotion() -> None:
    assert not hasattr(sync_module, "promote_snapshot")
    for path in Path("apps/travel-map/scripts").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "promote_snapshot" not in source


def test_sync_cli_main_prints_compact_candidate_review_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location(
        "candidate_status_sync_cli", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def fake_run(
        _args: argparse.Namespace,
        _keys: dict[str, str],
    ) -> str:
        return "cli-candidate"

    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: argparse.Namespace(env_file=None),
    )
    monkeypatch.setattr(module, "load_environment_file", lambda _path: None)
    monkeypatch.setattr(module, "run", fake_run)
    for name in module._REQUIRED_KEYS:
        monkeypatch.setenv(name, f"{name}-test")

    assert module.main() == 0
    assert capsys.readouterr().out == (
        '{"snapshotId":"cli-candidate",'
        '"status":"CANDIDATE_REVIEW_REQUIRED"}\n'
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "69863ac78689fb4b6e9941aabea03c3c1d618ccb26568e844079afd9092eb2c2",
            "f" * 64,
        ),
        (
            r"\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \uc1a1\uc6d4\uae38 48",
            r"\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \ubcc0\uc870\ub85c 1",
        ),
    ],
)
def test_sen_resource_is_bound_to_reviewed_content(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source_text = (SOURCE_RESOURCES / "sen-institutions.csv").read_text(
        encoding="utf-8"
    )
    path = tmp_path / "tampered-sen.csv"
    path.write_text(source_text.replace(old, new), encoding="utf-8")
    source = SenCsvSource(
        path,
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    )

    with pytest.raises(SourceDataError, match="reviewed|SHA-256|normalized"):
        source.load()


def test_keyless_official_school_csv_only_enriches_matching_neis_identity() -> None:
    csv_bytes = (
        "\ufeff\ud559\uad50ID,\ud559\uad50\uba85,\ud559\uad50\uae09\uad6c\ubd84,"
        "\uc124\ub9bd\uc77c\uc790,\uc124\ub9bd\ud615\ud0dc,\ubcf8\uad50\ubd84\uad50\uad6c\ubd84,"
        "\uc6b4\uc601\uc0c1\ud0dc,\uc18c\uc7ac\uc9c0\uc9c0\ubc88\uc8fc\uc18c,"
        "\uc18c\uc7ac\uc9c0\ub3c4\ub85c\uba85\uc8fc\uc18c,\uc2dc\ub3c4\uad50\uc721\uccad\ucf54\ub4dc,"
        "\uc2dc\ub3c4\uad50\uc721\uccad\uba85,\uad50\uc721\uc9c0\uc6d0\uccad\ucf54\ub4dc,"
        "\uad50\uc721\uc9c0\uc6d0\uccad\uba85,\uc0dd\uc131\uc77c\uc790,\ubcc0\uacbd\uc77c\uc790,"
        "\uc704\ub3c4,\uacbd\ub3c4,\ub370\uc774\ud130\uae30\uc900\uc77c\uc790\n"
        "B100000001,\uac80\uc99d\ud559\uad50,\ucd08\ub4f1\ud559\uad50,20000101,"
        "\uacf5\ub9bd,\ubcf8\uad50,\uc6b4\uc601,\uc11c\uc6b8 \uc911\uad6c,"
        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1,7010000,"
        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad,7011000,"
        "\uc911\ubd80\uad50\uc721\uc9c0\uc6d0\uccad,20260320,20260320,37.56,126.97,"
        "2026-03-20\n"
    ).encode("utf-8")
    locations = parse_standard_school_locations(
        csv_bytes,
        expected_seoul_count=1,
    )
    neis = SourceInstitutionRecord(
        **{
            **source_record(
                institution_id="neis:B10:7010001"
            ).__dict__,
            "latitude": None,
            "longitude": None,
            "coordinate_quality": "MISSING",
        }
    )

    enriched = enrich_neis_coordinates((neis,), locations)

    assert (enriched[0].latitude, enriched[0].longitude) == (37.56, 126.97)
    assert enriched[0].institution_id == "neis:B10:7010001"
    assert enriched[0].source == "NEIS"
    assert enriched[0].coordinate_quality == "OFFICIAL_STANDARD_COORDINATE"


@pytest.mark.asyncio
async def test_neis_source_requires_real_key_and_paginates_to_declared_total() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        page = int(request.url.params["pIndex"])
        section = payload["schoolInfo"]
        assert type(section) is list
        section[0]["head"][0]["list_total_count"] = 2
        row = section[1]["row"][0]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(api_key="test-key", client=client, page_size=1)
        result = await source.fetch()

    assert len(result.records) == 2
    assert result.provenance.page_count == 2
    assert [request.url.params["pIndex"] for request in requests] == ["1", "2"]
    assert all(request.url.params["ATPT_OFCDC_SC_CODE"] == "B10" for request in requests)

    with pytest.raises(SourceDataError, match="NEIS_API_KEY"):
        NeisSource(api_key="", client=httpx.AsyncClient())


@pytest.mark.asyncio
async def test_neis_source_rejects_keyless_sample_and_redacts_invalid_key() -> None:
    secret = "never-show-this-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["KEY"] == secret
        return httpx.Response(
            200,
            json={"RESULT": {"CODE": "ERROR-290", "MESSAGE": secret}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await NeisSource(api_key=secret, client=client).fetch()

    assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_source_http_failure_traceback_does_not_retain_api_key() -> None:
    secret = "traceback-secret-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await NeisSource(api_key=secret, client=client).fetch()

    formatted = "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )
    assert secret not in formatted
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_unexpected_transport_failure_does_not_retain_api_key() -> None:
    secret = "unexpected-transport-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport exploded")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="NEIS request failed") as raised:
            await NeisSource(api_key=secret, client=client).fetch()

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_kindergarten_http_failure_traceback_does_not_retain_api_key(
    tmp_path: Path,
) -> None:
    secret = "kindergarten-traceback-secret"
    region_path = write_region_fixture(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await KindergartenSource(
                api_key=secret,
                client=client,
                region_codes_path=region_path,
                timing="20261",
            ).fetch()

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_kakao_http_failure_traceback_does_not_retain_api_key() -> None:
    secret = "kakao-traceback-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key=secret, client=client)
        with pytest.raises(SourceDataError) as raised:
            await kakao.geocode("서울특별시 종로구 송월길 48")

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_shared_http_boundary_scrubs_secret_parameters_and_headers() -> None:
    secret = "shared-helper-traceback-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await get_json_with_retry(
                client=client,
                url="https://example.invalid/source",
                params={"key": secret},
                headers={"Authorization": f"Bearer {secret}"},
                source_label="test source",
            )

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_successful_source_fetches_clear_api_keys(tmp_path: Path) -> None:
    neis_secret = "successful-neis-secret"
    kindergarten_secret = "successful-kindergarten-secret"

    def neis_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=neis_payload(source_type="초등학교"))

    def kindergarten_handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = request.url.params["sggCode"]
        row = payload["kinderInfo"][0]  # type: ignore[index]
        row["kinderCode"] = f"K{request.url.params['sggCode']}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(neis_handler)
    ) as client:
        neis = NeisSource(api_key=neis_secret, client=client)
        await neis.fetch()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(kindergarten_handler)
    ) as client:
        kindergarten = KindergartenSource(
            api_key=kindergarten_secret,
            client=client,
            region_codes_path=write_region_fixture(tmp_path),
            timing="20261",
        )
        await kindergarten.fetch()

    assert neis_secret not in repr(neis.__dict__)
    assert kindergarten_secret not in repr(kindergarten.__dict__)


@pytest.mark.asyncio
async def test_kindergarten_fetch_reports_all_raw_rows_at_pinned_timing(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = request.url.params["sggCode"]
        row = payload["kinderInfo"][0]  # type: ignore[index]
        row["kinderCode"] = f"K{request.url.params['sggCode']}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=write_region_fixture(tmp_path),
            timing="20261",
        ).fetch()

    assert result.provenance.source_observation_date_counts == (
        ("2026-04-01", 25),
    )


# Production break caught: publishing a zero-count source observation histogram
# after every requested kindergarten region returned a valid but empty response.
@pytest.mark.asyncio
async def test_kindergarten_fetch_rejects_no_raw_source_rows(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = request.url.params["sggCode"]
        payload["kinderInfo"] = []
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="no source rows"):
            await KindergartenSource(
                api_key="test-key",
                client=client,
                region_codes_path=write_region_fixture(tmp_path),
                timing="20261",
            ).fetch()


@pytest.mark.asyncio
async def test_neis_pagination_counts_explicitly_excluded_source_rows() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        first = sections[1]["row"][0]
        excluded = dict(first)
        excluded["SD_SCHUL_CODE"] = "7010999"
        excluded["SCHUL_KND_SC_NM"] = "\uacf5\ub3d9\uc2e4\uc2b5\uc18c"
        sections[0]["head"][0]["list_total_count"] = 2
        sections[1]["row"] = [first, excluded]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            page_size=2,
        ).fetch()

    assert len(result.records) == 1
    assert result.provenance.page_count == 1
    assert result.provenance.fetched_row_count == 2
    assert result.provenance.row_count == 1


@pytest.mark.asyncio
async def test_neis_fetch_preserves_mixed_raw_load_dates() -> None:
    dates = ("20260423", "20260607")

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = 2
        row = sections[1]["row"][0]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        row["LOAD_DTM"] = dates[page - 1]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(api_key="test-key", client=client, page_size=1)
        result = await source.fetch()

    assert {record.source_as_of for record in result.records} == {
        "2026-04-23",
        "2026-06-07",
    }
    assert result.provenance.source_as_of == "2026-06-07"
    assert result.provenance.source_observation_date_counts == (
        ("2026-04-23", 1),
        ("2026-06-07", 1),
    )


@pytest.mark.asyncio
async def test_neis_fetch_accepts_raw_observation_span_of_90_days() -> None:
    dates = ("20260401", "20260630")

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = 2
        row = sections[1]["row"][0]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        row["LOAD_DTM"] = dates[page - 1]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            page_size=1,
        ).fetch()

    assert result.provenance.source_observation_date_counts == (
        ("2026-04-01", 1),
        ("2026-06-30", 1),
    )


@pytest.mark.asyncio
async def test_neis_fetch_rejects_raw_observation_span_over_90_days() -> None:
    dates = ("20260401", "20260701")

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = 2
        row = sections[1]["row"][0]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        row["LOAD_DTM"] = dates[page - 1]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(api_key="test-key", client=client, page_size=1)
        with pytest.raises(SourceDataError, match="observation date span"):
            await source.fetch()


@pytest.mark.asyncio
async def test_neis_fetch_counts_excluded_rows_in_raw_observation_dates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_payload(
            source_type=("초등학교" if page == 1 else "공동실습소")
        )
        section = payload["schoolInfo"]
        section[0]["head"][0]["list_total_count"] = 2  # type: ignore[index]
        row = section[1]["row"][0]  # type: ignore[index]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        row["LOAD_DTM"] = "20260810" if page == 1 else "20260809"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(api_key="test-key", client=client, page_size=1)
        result = await source.fetch()

    assert [record.source_as_of for record in result.records] == ["2026-08-10"]
    assert result.provenance.source_observation_date_counts == (
        ("2026-08-09", 1),
        ("2026-08-10", 1),
    )


@pytest.mark.asyncio
async def test_neis_source_rejects_five_row_sample_success_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        first = sections[1]["row"][0]
        sections[0]["head"][0]["list_total_count"] = 5
        sections[1]["row"] = [
            {**first, "SD_SCHUL_CODE": f"701000{index}"}
            for index in range(1, 6)
        ]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="sample"):
            await NeisSource(api_key="test-key", client=client).fetch()

    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_total", "message"),
    [
        (2_147_483_647, "row ceiling"),
        (201, "page limit"),
    ],
)
async def test_neis_source_bounds_declared_total_before_second_request(
    declared_total: int,
    message: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = declared_total
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match=message):
            await NeisSource(api_key="test-key", client=client, page_size=1).fetch()

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_neis_source_rejects_oversized_response_before_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neis_module, "_MAX_RESPONSE_BYTES", 100)

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = neis_payload(source_type="초등학교")
        payload["padding"] = "x" * 500
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="response size"):
            await NeisSource(api_key="test-key", client=client).fetch()


@pytest.mark.asyncio
async def test_neis_response_stream_stops_after_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neis_module, "_MAX_RESPONSE_BYTES", 100)
    yielded_chunks = 0

    class CountingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal yielded_chunks
            for _ in range(10):
                yielded_chunks += 1
                yield b"x" * 50

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="response size"):
            await NeisSource(api_key="test-key", client=client).fetch()

    assert yielded_chunks < 10


@pytest.mark.asyncio
async def test_neis_source_rejects_more_rows_than_requested_page_size() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        first = sections[1]["row"][0]
        sections[0]["head"][0]["list_total_count"] = 2
        sections[1]["row"] = [
            first,
            {**first, "SD_SCHUL_CODE": "7010002"},
        ]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="page size"):
            await NeisSource(api_key="test-key", client=client, page_size=1).fetch()


@pytest.mark.asyncio
async def test_neis_source_bounds_actual_page_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neis_module, "_MAX_PAGE_COUNT", 2)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["pIndex"])
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = 3
        sections[1]["row"][0]["SD_SCHUL_CODE"] = f"701{page:04d}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="page limit|short page"):
            await NeisSource(
                api_key="test-key",
                client=client,
                page_size=1_000,
            ).fetch()

    assert len(requests) <= 2


@pytest.mark.asyncio
async def test_kindergarten_source_requires_key_and_detects_repeated_page(
    tmp_path: Path,
) -> None:
    region_path = write_region_fixture(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["currentPage"] = int(request.url.params["currentPage"])
        payload["pageCnt"] = 1
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=region_path,
            timing="20261",
            page_size=1,
        )
        with pytest.raises(SourceDataError, match="repeated page"):
            await source.fetch()

    with pytest.raises(SourceDataError, match="KINDERGARTEN_API_KEY"):
        KindergartenSource(
            api_key="",
            client=httpx.AsyncClient(),
            region_codes_path=region_path,
            timing="20261",
        )


@pytest.mark.asyncio
async def test_kindergarten_source_rejects_mismatched_response_echo(
    tmp_path: Path,
) -> None:
    region_path = write_region_fixture(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = "99999"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=region_path,
            timing="20261",
        )
        with pytest.raises(SourceDataError, match="response echo"):
            await source.fetch()


@pytest.mark.asyncio
async def test_kindergarten_source_bounds_pagination_without_total(
    tmp_path: Path,
) -> None:
    region_path = write_region_fixture(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        page = int(request.url.params["currentPage"])
        payload["currentPage"] = page
        payload["pageCnt"] = 1
        row = payload["kinderInfo"][0]  # type: ignore[index]
        row["kinderCode"] = f"K{page:08d}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=region_path,
            timing="20261",
            page_size=1,
        )
        with pytest.raises(SourceDataError, match="page limit"):
            await source.fetch()


@pytest.mark.asyncio
async def test_kindergarten_source_bounds_cumulative_response_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kindergarten_module, "_MAX_CUMULATIVE_BYTES", 100)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=kindergarten_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=write_region_fixture(tmp_path),
            timing="20261",
        )
        with pytest.raises(SourceDataError, match="cumulative response size"):
            await source.fetch()

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_standard_school_source_stops_stream_at_byte_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(standard_school_module, "_MAX_RESPONSE_BYTES", 100)
    yielded_chunks = 0

    class CountingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal yielded_chunks
            for _ in range(10):
                yielded_chunks += 1
                yield b"x" * 50

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="response size"):
            await StandardSchoolLocationSource(client=client).fetch()

    assert yielded_chunks < 10


@pytest.mark.asyncio
async def test_kakao_geocoder_bounds_paid_requests_and_cumulative_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kakao_module, "_MAX_REQUEST_COUNT", 1)
    monkeypatch.setattr(kakao_module, "_MAX_CUMULATIVE_BYTES", 10_000)
    address = "서울특별시 종로구 송월길 48"
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"documents": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key="test-key", client=client)
        assert await kakao.geocode(address) is None
        with pytest.raises(SourceDataError, match="request limit"):
            await kakao.geocode(address)

    assert requests == 1


@pytest.mark.asyncio
async def test_kakao_geocoder_does_not_retain_unbounded_raw_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kakao_module, "_MAX_CUMULATIVE_BYTES", 100)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": [], "padding": "x" * 200})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key="test-key", client=client)
        with pytest.raises(SourceDataError, match="cumulative response size"):
            await kakao.geocode("서울특별시 종로구 송월길 48")

    assert not hasattr(kakao, "_raw_responses")


@pytest.mark.asyncio
async def test_kakao_geocode_accepts_one_exact_road_address_and_redacts_key() -> None:
    secret = "never-show-kakao-key"
    address = "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \uc1a1\uc6d4\uae38 48"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"KakaoAK {secret}"
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "x": "126.9680",
                        "y": "37.5710",
                        "road_address": {"address_name": address},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key=secret, client=client)
        result = await kakao.geocode(address)
        provenance = kakao.provenance()
        kakao.clear_credentials()

    assert result is not None
    assert result.road_address == address
    assert result.confidence == "EXACT_ROAD_ADDRESS"
    assert provenance.fetched_row_count == 1
    assert provenance.matched_row_count == 1
    assert secret not in repr(provenance)
    assert secret not in repr(kakao.__dict__)

    with pytest.raises(SourceDataError, match="KAKAO_REST_API_KEY"):
        KakaoLocalClient(api_key="", client=httpx.AsyncClient())


@pytest.mark.asyncio
async def test_missing_coordinate_is_filled_only_by_exact_kakao_result() -> None:
    address = "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "x": "126.97",
                        "y": "37.56",
                        "road_address": {"address_name": address},
                    }
                ]
            },
        )

    missing = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "latitude": None,
            "longitude": None,
            "coordinate_quality": "MISSING",
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key="test-key", client=client)
        records = await geocode_missing_records((missing,), kakao)

    assert (records[0].latitude, records[0].longitude) == (37.56, 126.97)
    assert records[0].coordinate_quality == "GEOCODED"


def test_candidate_requires_seoul_coverage_service(tmp_path: Path) -> None:
    with pytest.raises(SnapshotQualityError, match="CoverageService"):
        build_candidate_snapshot(
            records=(source_record(),),
            previous=None,
            output_root=tmp_path,
            snapshot_id="missing-coverage",
        )


def test_candidate_requires_explicit_source_provenance(tmp_path: Path) -> None:
    with pytest.raises(SnapshotQualityError, match="source provenance is required"):
        build_candidate_snapshot(
            records=(source_record(),),
            previous=None,
            output_root=tmp_path,
            snapshot_id="missing-provenance",
            coverage=TEST_COVERAGE,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"institution_id": "kinder:wrong"}, "namespace"),
        ({"institution_type": "UNKNOWN_SCHOOL"}, "institution type"),
        ({"institution_type": "LIBRARY"}, "institution type"),
        ({"foundation_type": "UNKNOWN"}, "foundation type"),
        ({"coordinate_quality": "GUESSED"}, "coordinate quality"),
    ],
)
def test_candidate_rejects_cross_source_ids_and_unknown_enums(
    tmp_path: Path,
    updates: dict[str, str],
    message: str,
) -> None:
    original = source_record()
    invalid = SourceInstitutionRecord(**{**original.__dict__, **updates})

    with pytest.raises(SnapshotQualityError, match=message):
        build_test_candidate(
            records=(invalid,),
            previous=None,
            output_root=tmp_path,
            snapshot_id="invalid-source-contract",
            coverage=TEST_COVERAGE,
        )


def test_source_record_persists_official_branch_as_second_site(
    tmp_path: Path,
) -> None:
    branch = SourceInstitutionSiteRecord(
        site_code="gayang",
        site_name="Gay ang branch",
        road_address="서울특별시 강서구 양천로 61",
        district="강서구",
        latitude=37.5701,
        longitude=126.8412,
        coordinate_quality="MANUALLY_VERIFIED",
    )
    record = SourceInstitutionRecord(
        **{**source_record().__dict__, "additional_sites": (branch,)}
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="official-branch",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    assert len(verified.institutions) == 1
    assert {site.site_id for site in verified.sites} == {
        "neis:B10:7010001:main",
        "neis:B10:7010001:gayang",
    }
    assert sum(site.is_default for site in verified.sites) == 1


def test_missing_coordinate_branch_is_persisted_for_review(
    tmp_path: Path,
) -> None:
    branch = SourceInstitutionSiteRecord(
        site_code="future-branch",
        site_name="Future branch",
        road_address="서울특별시 강서구 검증로 2",
        district="강서구",
        latitude=None,
        longitude=None,
        coordinate_quality="MISSING",
    )
    record = SourceInstitutionRecord(
        **{**source_record().__dict__, "additional_sites": (branch,)}
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="missing-branch",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    branch_site = next(
        site for site in verified.sites if site.site_id.endswith(":future-branch")
    )
    assert branch_site.status.value == "REVIEW_REQUIRED"
    assert branch_site.latitude is None
    assert branch_site.routing_anchor_latitude is None


def test_manifest_persists_cross_source_possible_match_pairs(
    tmp_path: Path,
) -> None:
    neis = source_record()
    kindergarten = SourceInstitutionRecord(
        **{
            **neis.__dict__,
            "institution_id": "kinder:K12345678",
            "institution_type": "KINDERGARTEN",
            "source": "KINDERGARTEN_INFO",
            "source_region_code": "11",
            "source_as_of": "2026-04-01",
        }
    )
    candidate = build_test_candidate(
        records=(neis, kindergarten),
        previous=None,
        output_root=tmp_path,
        snapshot_id="possible-pair",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    assert {item.institution_id for item in verified.institutions} == {
        "neis:B10:7010001",
        "kinder:K12345678",
    }
    assert verified.manifest.possible_match_count == 1
    assert verified.manifest.possible_matches[0].institution_ids == (
        "kinder:K12345678",
        "neis:B10:7010001",
    )


# Production break caught: rejecting record-level source vintages that are all bound
# by the raw-source observation histogram.
def test_candidate_persists_mixed_source_observation_dates(tmp_path: Path) -> None:
    first = source_record(institution_id="neis:B10:7010001")
    second = SourceInstitutionRecord(
        **{
            **source_record(institution_id="neis:B10:7010002").__dict__,
            "source_as_of": "2026-08-09",
        }
    )
    records = (first, second)

    candidate = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="mixed-source-dates",
        coverage=TEST_COVERAGE,
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["sources"][0]["sourceObservationDateCounts"] == {
        "2026-08-09": 1,
        "2026-08-10": 1,
    }
    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert {row.source_as_of for row in verify_snapshot(tmp_path).institutions} == {
        "2026-08-09",
        "2026-08-10",
    }


# Production break caught: writing a candidate whose institution vintage does not
# occur in the supplied raw-source observation histogram.
def test_candidate_rejects_unobserved_source_date_before_writing(
    tmp_path: Path,
) -> None:
    records = (
        source_record(institution_id="neis:B10:7010001"),
        replace(
            source_record(institution_id="neis:B10:7010002"),
            source_as_of="2026-08-09",
        ),
    )
    valid = source_provenance_for(records)["NEIS"]
    invalid = replace(
        valid,
        source_observation_date_counts=(
            ("2026-08-08", 1),
            ("2026-08-10", 1),
        ),
    )

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        build_test_candidate(
            records=records,
            previous=None,
            output_root=tmp_path,
            snapshot_id="unobserved-source-date",
            coverage=TEST_COVERAGE,
            source_provenance={"NEIS": invalid},
        )
    assert not (tmp_path / ".unobserved-source-date.candidate").exists()


# Production break caught: deriving snapshotAsOf only from selectable institutions
# when a later reviewed source observation belongs to a filtered raw row.
def test_candidate_snapshot_as_of_includes_latest_raw_observation(
    tmp_path: Path,
) -> None:
    record = replace(source_record(), source_as_of="2026-04-23")
    provenance = replace(
        source_provenance_for((record,))["NEIS"],
        fetched_row_count=2,
        source_as_of="2026-06-07",
        source_observation_date_counts=(
            ("2026-04-23", 1),
            ("2026-06-07", 1),
        ),
    )

    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="latest-raw-observation",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": provenance},
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["snapshotAsOf"] == "2026-06-07"
    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)


# Production break caught: retaining an older preserved institution date instead of
# advancing it to the latest observation in the current raw-source histogram.
def test_missing_institution_uses_current_observation_histogram_maximum(
    tmp_path: Path,
) -> None:
    initial_records = (
        source_record(institution_id="neis:B10:7010001"),
        source_record(institution_id="neis:B10:7010002"),
    )
    initial = build_test_candidate(
        records=initial_records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="histogram-before-missing",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    current = replace(initial_records[0], source_as_of="2026-04-23")
    provenance = replace(
        source_provenance_for((current,))["NEIS"],
        fetched_row_count=2,
        source_as_of="2026-06-07",
        source_observation_date_counts=(
            ("2026-04-23", 1),
            ("2026-06-07", 1),
        ),
    )

    candidate = build_test_candidate(
        records=(current,),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="histogram-after-missing",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": provenance},
    )
    rows = [
        json.loads(line)
        for line in (candidate.candidate_path / "institutions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    preserved = next(row for row in rows if row["status"] == "MISSING_FROM_SOURCE")
    assert preserved["sourceAsOf"] == "2026-06-07"


# Production break caught: replacing an approved pointer after a source loses 40%
# of its active rows.
def test_failed_candidate_does_not_replace_current_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    records = tuple(
        SourceInstitutionRecord(
            institution_id=f"neis:B10:{index:07d}",
            official_name=f"\uac80\uc99d\ud559\uad50{index}",
            institution_type="ELEMENTARY_SCHOOL",
            foundation_type="PUBLIC",
            education_office="\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            road_address=f"\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c {index}",
            district="\uc911\uad6c",
            latitude=37.56,
            longitude=126.97 + index / 100_000,
            source="NEIS",
            source_region_code="B10",
            source_as_of="2026-08-10",
            coordinate_quality="MANUALLY_VERIFIED",
        )
        for index in range(10)
    )
    initial = build_test_candidate(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="initial",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, root, coverage=TEST_COVERAGE)
    before = (root / "current.json").read_bytes()
    result = build_test_candidate(
        records=records[:6],
        previous=verify_snapshot(root),
        output_root=root,
        snapshot_id="candidate-with-drop",
        coverage=TEST_COVERAGE,
    )

    assert result.approved is False
    forged_result = replace(result, issues=())
    with pytest.raises(SnapshotQualityError, match="record count drop"):
        approve_test_candidate(forged_result, root, coverage=TEST_COVERAGE)
    assert (root / "current.json").read_bytes() == before


def test_existing_current_cannot_be_replaced_when_previous_is_omitted(
    tmp_path: Path,
) -> None:
    records = tuple(
        SourceInstitutionRecord(
            **{
                **source_record(
                    institution_id=f"neis:B10:{index:07d}",
                    road_address=(
                        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c "
                        f"\uac80\uc99d\ub85c {index + 1}"
                    ),
                ).__dict__,
                "longitude": 126.97 + index / 100_000,
            }
        )
        for index in range(10)
    )
    initial = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="existing-current",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    before = (tmp_path / "current.json").read_bytes()
    omitted = build_test_candidate(
        records=records[:1],
        previous=None,
        output_root=tmp_path,
        snapshot_id="omitted-previous",
        coverage=TEST_COVERAGE,
    )

    with pytest.raises(SnapshotQualityError, match="previous snapshot"):
        approve_test_candidate(omitted, tmp_path, coverage=TEST_COVERAGE)
    assert (tmp_path / "current.json").read_bytes() == before


def test_coordinate_gate_uses_only_current_rows_and_stale_sites_are_inactive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    records = tuple(
        SourceInstitutionRecord(
            **{
                **source_record(
                    institution_id=f"neis:B10:{index:07d}",
                    road_address=(
                        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c "
                        f"\uac80\uc99d\ub85c {index + 1}"
                    ),
                ).__dict__,
                "longitude": 126.97 + index / 100_000,
            }
        )
        for index in range(100)
    )
    initial = build_test_candidate(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="full",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, root, coverage=TEST_COVERAGE)
    current = list(records[:90])
    for index in (88, 89):
        current[index] = SourceInstitutionRecord(
            **{
                **current[index].__dict__,
                "latitude": None,
                "longitude": None,
                "coordinate_quality": "MISSING",
            }
        )

    candidate = build_test_candidate(
        records=tuple(current),
        previous=verify_snapshot(root),
        output_root=root,
        snapshot_id="partial",
        coverage=TEST_COVERAGE,
    )
    site_rows = [
        json.loads(line)
        for line in (candidate.candidate_path / "sites.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert any("coordinate validation" in issue for issue in candidate.issues)
    stale_sites = [
        row
        for row in site_rows
        if int(row["institutionId"].rsplit(":", 1)[-1]) >= 90
    ]
    assert stale_sites
    assert {row["status"] for row in stale_sites} == {"MISSING_FROM_SOURCE"}


def test_preserved_enriched_site_does_not_require_current_enrichment_match(
    tmp_path: Path,
) -> None:
    enriched = replace(
        source_record(),
        coordinate_quality="OFFICIAL_STANDARD_COORDINATE",
    )
    initial = build_test_candidate(
        records=(enriched,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="enriched-before-missing",
        coverage=TEST_COVERAGE,
        enrichment_provenance=(
            standard_enrichment_provenance(matched_row_count=1),
        ),
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    replacement = source_record(institution_id="neis:B10:7010002")
    candidate = build_test_candidate(
        records=(replacement,),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="enriched-now-missing",
        coverage=TEST_COVERAGE,
    )

    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    old = next(
        institution
        for institution in verified.institutions
        if institution.institution_id == enriched.institution_id
    )
    assert old.status is InstitutionStatus.MISSING_FROM_SOURCE
    assert len(verified.manifest.enrichments) == 1
    assert verified.manifest.enrichments[0].preserved_matched_row_count == 1


def test_missing_official_branch_is_preserved_when_parent_remains(
    tmp_path: Path,
) -> None:
    branch = SourceInstitutionSiteRecord(
        site_code="gayang",
        site_name="Gay ang branch",
        road_address="서울특별시 강서구 양천로 61",
        district="강서구",
        latitude=37.5701,
        longitude=126.8412,
        coordinate_quality="MANUALLY_VERIFIED",
    )
    with_branch = replace(source_record(), additional_sites=(branch,))
    initial = build_test_candidate(
        records=(with_branch,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="branch-before-missing",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="branch-now-missing",
        coverage=TEST_COVERAGE,
    )

    approve_test_candidate(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    old_branch = next(
        site for site in verified.sites if site.site_id.endswith(":gayang")
    )
    assert old_branch.status is InstitutionStatus.MISSING_FROM_SOURCE


def test_concurrent_promotions_from_same_previous_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="concurrent-base",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    previous = verify_snapshot(tmp_path)
    first = build_test_candidate(
        records=(source_record(),),
        previous=previous,
        output_root=tmp_path,
        snapshot_id="concurrent-first",
        coverage=TEST_COVERAGE,
    )
    second = build_test_candidate(
        records=(source_record(),),
        previous=previous,
        output_root=tmp_path,
        snapshot_id="concurrent-second",
        coverage=TEST_COVERAGE,
    )
    first_digest = review_test_candidate(first, tmp_path)
    second_digest = review_test_candidate(second, tmp_path)
    real_quality = sync_module._recheck_promotion_quality
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def controlled_quality(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return real_quality(*args, **kwargs)

    monkeypatch.setattr(
        sync_module,
        "_recheck_promotion_quality",
        controlled_quality,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            approve_test_candidate,
            first,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=first_digest,
        )
        assert first_entered.wait(timeout=2)
        second_future = executor.submit(
            approve_test_candidate,
            second,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=second_digest,
        )
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        outcomes = []
        for future in (first_future, second_future):
            try:
                future.result(timeout=3)
                outcomes.append("success")
            except SnapshotQualityError:
                outcomes.append("blocked")

    assert sorted(outcomes) == ["blocked", "success"]


def test_manifest_counts_changed_institution_records(tmp_path: Path) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="before-change",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    original = source_record()
    changed = SourceInstitutionRecord(
        **{**original.__dict__, "official_name": "Changed Official Name"}
    )

    candidate = build_test_candidate(
        records=(changed,),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="after-change",
        coverage=TEST_COVERAGE,
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["diff"]["changedCount"] == 1


def test_address_region_mismatch_is_quarantined(tmp_path: Path) -> None:
    record = source_record(
        institution_id="neis:B10:7010001",
        road_address="\ubd80\uc0b0\uad11\uc5ed\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1",
    )

    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="address-mismatch",
        coverage=TEST_COVERAGE,
    )

    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["quarantinedCount"] == 1
    assert candidate.approved is False
    assert any("coordinate validation" in issue for issue in candidate.issues)


def test_coordinate_outside_seoul_is_quarantined(tmp_path: Path) -> None:
    coverage = CoverageService.from_geojson(
        seoul_path="apps/travel-map/resources/geodata/seoul.geojson",
        buffer_distance_m=12_000,
    )
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "latitude": 35.1796,
            "longitude": 129.0756,
        }
    )

    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="coordinate-mismatch",
        coverage=coverage,
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["quarantinedCount"] == 1
    assert any("coordinate validation" in issue for issue in candidate.issues)
    forged_candidate = replace(candidate, issues=())
    with pytest.raises(SnapshotQualityError, match="coordinate validation"):
        approve_test_candidate(forged_candidate, tmp_path, coverage=TEST_COVERAGE)


def test_namesake_across_sources_is_not_merged(tmp_path: Path) -> None:
    first = source_record(institution_id="neis:B10:7010001")
    second = SourceInstitutionRecord(
        **{
            **first.__dict__,
            "institution_id": "kinder:verified-kindergarten",
            "institution_type": "KINDERGARTEN",
            "source": "KINDERGARTEN_INFO",
            "source_region_code": "11",
            "source_as_of": "2026-04-01",
        }
    )

    candidate = build_test_candidate(
        records=(first, second),
        previous=None,
        output_root=tmp_path,
        snapshot_id="possible-match",
        coverage=TEST_COVERAGE,
    )
    rows = (candidate.candidate_path / "institutions.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert len(rows) == 2
    assert manifest["possibleMatchCount"] == 1


def test_promotion_rechecks_hash_before_pointer_change(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="tampered",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    (candidate.candidate_path / "institutions.jsonl").write_text(
        "tampered\n", encoding="utf-8"
    )

    with pytest.raises(SnapshotQualityError, match="hash mismatch"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


@pytest.mark.parametrize(
    "review_digest",
    ["", "A" * 64, "0" * 63, "0" * 65, True, 1],
)
def test_approval_requires_exact_lowercase_review_digest(
    tmp_path: Path,
    review_digest: object,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="digest-contract",
        coverage=TEST_COVERAGE,
    )
    with pytest.raises(SnapshotQualityError, match="review digest"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=cast(str, review_digest),
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


def test_approval_requires_data_steward_role(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="role-contract",
        coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    with pytest.raises(SnapshotQualityError, match="reviewer role"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=cast(str, packet["reviewDigest"]),
            reviewer_role="developer",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )


def test_approval_rechecks_review_digest_before_pointer_mutation(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="digest-recheck",
        coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["countsByType"] = {"ELEMENTARY_SCHOOL": 2}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="attestation|manifest|digest"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=cast(str, packet["reviewDigest"]),
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


def test_reviewed_approval_writes_verified_current_snapshot(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="reviewed-approval",
        coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    digest = approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=cast(str, packet["reviewDigest"]),
        reviewer_role="data-steward",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    verified = verify_snapshot(tmp_path)
    assert digest == packet["reviewDigest"]
    assert verified.manifest.snapshot_id == candidate.snapshot_id
    assert verified.manifest.approved is True
    assert verified.manifest.approved_by_role == "data-steward"


def test_candidate_review_leaves_existing_current_pointer_unchanged(
    tmp_path: Path,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="initial-current",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path)
    before = (tmp_path / "current.json").read_bytes()
    second = build_test_candidate(
        records=(source_record(),),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="next-candidate",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=second.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert packet["previousSnapshotId"] == "initial-current"
    assert (tmp_path / "current.json").read_bytes() == before


def test_reviewed_approval_retry_is_idempotent(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="reviewed-retry",
        coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    kwargs = {
        "snapshot_id": candidate.snapshot_id,
        "review_digest": packet["reviewDigest"],
        "reviewer_role": "data-steward",
        "snapshot_root": tmp_path,
        "coverage": TEST_COVERAGE,
    }
    approve_candidate_snapshot(**kwargs)
    first_pointer = (tmp_path / "current.json").read_bytes()
    approve_candidate_snapshot(**kwargs)
    assert (tmp_path / "current.json").read_bytes() == first_pointer


def test_review_packet_is_deterministic_and_contains_audit_categories(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="review-packet",
        coverage=TEST_COVERAGE,
    )

    first = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    second = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert first == second
    assert set(first) == {
        "status",
        "snapshotId",
        "createdAt",
        "snapshotAsOf",
        "previousSnapshotId",
        "sourceCounts",
        "sourceObservationDateRanges",
        "institutionTypeCounts",
        "foundationCounts",
        "districtCounts",
        "statusCounts",
        "coordinateQualityCounts",
        "quarantinedInstitutionIds",
        "quarantinedSiteIds",
        "diff",
        "siteOnlyDiff",
        "institutionsSha256",
        "sitesSha256",
        "candidateManifestSha256",
        "sourceProvenanceSha256",
        "enrichmentProvenanceSha256",
        "reviewDigest",
    }
    assert first["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert first["snapshotId"] == candidate.snapshot_id
    assert first["districtCounts"] == {
        "강남구": 0,
        "강동구": 0,
        "강북구": 0,
        "강서구": 0,
        "관악구": 0,
        "광진구": 0,
        "구로구": 0,
        "금천구": 0,
        "노원구": 0,
        "도봉구": 0,
        "동대문구": 0,
        "동작구": 0,
        "마포구": 0,
        "서대문구": 0,
        "서초구": 0,
        "성동구": 0,
        "성북구": 0,
        "송파구": 0,
        "양천구": 0,
        "영등포구": 0,
        "용산구": 0,
        "은평구": 0,
        "종로구": 0,
        "중구": 1,
        "중랑구": 0,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", cast(str, first["reviewDigest"]))
    without_digest = {
        key: value for key, value in first.items() if key != "reviewDigest"
    }
    assert (
        first["reviewDigest"]
        == hashlib.sha256(
            json.dumps(
                without_digest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    assert not (tmp_path / "current.json").exists()


# Production break caught: omitting or misrepresenting the bounded raw-source
# observation dates that a data steward must review before approving a snapshot.
def test_review_packet_binds_sorted_source_observation_date_ranges(
    tmp_path: Path,
) -> None:
    records = (
        replace(
            source_record(),
            official_name="비공개검토학교",
            source_as_of="2026-04-23",
        ),
        replace(
            source_record(institution_id="neis:B10:7010002"),
            source_as_of="2026-06-07",
        ),
    )
    provenance = replace(
        source_provenance_for(records)["NEIS"],
        fetched_row_count=3,
        source_as_of="2026-06-07",
        source_observation_date_counts=(
            ("2026-04-23", 1),
            ("2026-06-07", 2),
        ),
    )
    candidate = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="review-date-ranges",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": provenance},
    )

    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert packet["sourceObservationDateRanges"]["NEIS"] == {
        "earliest": "2026-04-23",
        "latest": "2026-06-07",
        "spanDays": 45,
        "rawRowCounts": {"2026-04-23": 1, "2026-06-07": 2},
    }
    assert "비공개검토학교" not in json.dumps(packet, ensure_ascii=False)


# Production break caught: approving a candidate with a digest reviewed against
# a different but otherwise valid raw-source observation-date histogram.
def test_approval_rejects_digest_for_changed_source_observation_histogram(
    tmp_path: Path,
) -> None:
    records = (
        replace(source_record(), source_as_of="2026-04-23"),
        replace(
            source_record(institution_id="neis:B10:7010002"),
            source_as_of="2026-06-07",
        ),
    )
    original_provenance = replace(
        source_provenance_for(records)["NEIS"],
        fetched_row_count=3,
        source_as_of="2026-06-07",
        source_observation_date_counts=(
            ("2026-04-23", 1),
            ("2026-06-07", 2),
        ),
    )
    changed_provenance = replace(
        original_provenance,
        source_observation_date_counts=(
            ("2026-04-23", 2),
            ("2026-06-07", 1),
        ),
    )
    original_root = tmp_path / "original"
    changed_root = tmp_path / "changed"
    original = build_test_candidate(
        records=records,
        previous=None,
        output_root=original_root,
        snapshot_id="date-histogram-digest",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": original_provenance},
    )
    changed = build_test_candidate(
        records=records,
        previous=None,
        output_root=changed_root,
        snapshot_id="date-histogram-digest",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": changed_provenance},
    )
    original_packet = build_candidate_review_packet(
        snapshot_id=original.snapshot_id,
        snapshot_root=original_root,
        coverage=TEST_COVERAGE,
    )
    changed_packet = build_candidate_review_packet(
        snapshot_id=changed.snapshot_id,
        snapshot_root=changed_root,
        coverage=TEST_COVERAGE,
    )

    assert changed_packet["sourceObservationDateRanges"]["NEIS"]["rawRowCounts"] == {
        "2026-04-23": 2,
        "2026-06-07": 1,
    }
    assert original_packet["reviewDigest"] != changed_packet["reviewDigest"]
    with pytest.raises(SnapshotQualityError, match="review digest"):
        approve_candidate_snapshot(
            snapshot_id=changed.snapshot_id,
            review_digest=cast(str, original_packet["reviewDigest"]),
            reviewer_role="data-steward",
            snapshot_root=changed_root,
            coverage=TEST_COVERAGE,
        )
    assert not (changed_root / "current.json").exists()


def test_administrator_snapshot_guidance_names_source_date_range_review() -> None:
    guide = Path("apps/travel-map/README.md").read_text(encoding="utf-8")

    assert (
        "each source's earliest/latest date, span, and raw-row counts"
        in " ".join(guide.split())
    )


def test_review_packet_excludes_sensitive_source_values(tmp_path: Path) -> None:
    sensitive = replace(
        source_record(road_address="서울특별시 중구 비공개로 987"),
        official_name="비공개검토학교",
    )
    candidate = build_test_candidate(
        records=(sensitive,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="private-review",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)

    for forbidden in (
        sensitive.official_name,
        sensitive.road_address,
        str(sensitive.latitude),
        str(sensitive.longitude),
        "Authorization",
        "rawResponse",
        "KAKAO_REST_API_KEY",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("invalid_site", ["main", "branch"])
def test_candidate_rejects_non_seoul_district_before_writing_review_data(
    tmp_path: Path,
    invalid_site: str,
) -> None:
    sensitive_name = "비공개검토학교"
    record = replace(
        source_record(
            road_address=f"서울특별시 {sensitive_name} 검증로 1",
        ),
        official_name=sensitive_name,
        district=sensitive_name if invalid_site == "main" else "중구",
        additional_sites=(
            SourceInstitutionSiteRecord(
                site_code="private-branch",
                site_name="Private branch",
                road_address="서울특별시 강서구 양천로 61",
                district=(
                    sensitive_name if invalid_site == "branch" else "강서구"
                ),
                latitude=37.5701,
                longitude=126.8412,
                coordinate_quality="MANUALLY_VERIFIED",
            ),
        ),
    )

    with pytest.raises(SnapshotQualityError, match="district"):
        build_test_candidate(
            records=(record,),
            previous=None,
            output_root=tmp_path,
            snapshot_id=f"invalid-{invalid_site}-district",
            coverage=TEST_COVERAGE,
        )

    assert not (tmp_path / f".invalid-{invalid_site}-district.candidate").exists()
    assert not (tmp_path / "current.json").exists()


def test_review_rejects_signed_legacy_candidate_with_non_seoul_district(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="legacy-district-current",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    previous = verify_snapshot(tmp_path)
    pointer_before = (tmp_path / "current.json").read_bytes()
    sensitive_name = "비공개검토학교"
    legacy_record = replace(
        source_record(
            road_address=f"서울특별시 {sensitive_name} 검증로 1",
        ),
        official_name=sensitive_name,
        district=sensitive_name,
    )
    with monkeypatch.context() as legacy_context:
        legacy_context.setattr(
            sync_module,
            "_validate_source_districts",
            lambda _records, _provenance: None,
        )
        candidate = build_test_candidate(
            records=(legacy_record,),
            previous=previous,
            output_root=tmp_path,
            snapshot_id="legacy-private-district",
            coverage=TEST_COVERAGE,
        )

    with pytest.raises(SnapshotQualityError, match="district"):
        build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    with pytest.raises(SnapshotQualityError, match="district"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest="0" * 64,
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    assert (tmp_path / "current.json").read_bytes() == pointer_before


def test_review_packet_reports_disjoint_site_only_changes(tmp_path: Path) -> None:
    changed = SourceInstitutionSiteRecord(
        site_code="changed",
        site_name="Changed branch",
        road_address="서울특별시 강서구 양천로 61",
        district="강서구",
        latitude=37.5701,
        longitude=126.8412,
        coordinate_quality="MANUALLY_VERIFIED",
    )
    removed = replace(
        changed,
        site_code="removed",
        site_name="Removed branch",
    )
    initial = build_test_candidate(
        records=(replace(source_record(), additional_sites=(changed, removed)),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="site-only-before",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    added = replace(changed, site_code="added", site_name="Added branch")
    candidate = build_test_candidate(
        records=(
            replace(
                source_record(),
                additional_sites=(
                    replace(changed, road_address="서울특별시 강서구 양천로 62"),
                    added,
                ),
            ),
        ),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="site-only-after",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert cast(dict[str, object], packet["diff"])["changedCount"] == 0
    assert packet["siteOnlyDiff"] == {
        "addedSiteIds": ["neis:B10:7010001:added"],
        "changedSiteIds": ["neis:B10:7010001:changed"],
        "missingSiteIds": ["neis:B10:7010001:removed"],
    }


def test_review_packet_counts_institution_only_change_once(tmp_path: Path) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="institution-only-before",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    candidate = build_test_candidate(
        records=(replace(source_record(), official_name="Changed Official Name"),),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="institution-only-after",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert cast(dict[str, object], packet["diff"])["changedCount"] == 1
    assert packet["siteOnlyDiff"] == {
        "addedSiteIds": [],
        "changedSiteIds": [],
        "missingSiteIds": [],
    }


def test_review_packet_does_not_report_site_change_for_changed_institution(
    tmp_path: Path,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="institution-and-site-before",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    candidate = build_test_candidate(
        records=(
            replace(
                source_record(
                    road_address="서울특별시 중구 검증로 2",
                ),
                official_name="Changed Official Name",
            ),
        ),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="institution-and-site-after",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert cast(dict[str, object], packet["diff"])["changedCount"] == 1
    assert packet["siteOnlyDiff"] == {
        "addedSiteIds": [],
        "changedSiteIds": [],
        "missingSiteIds": [],
    }


def test_review_rejects_tampered_candidate_before_emitting_packet(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="tampered-review",
        coverage=TEST_COVERAGE,
    )
    (candidate.candidate_path / "sites.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="hash|candidate"):
        build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


def test_review_rejects_unsafe_id_symlink_and_quality_issues(tmp_path: Path) -> None:
    with pytest.raises(SnapshotQualityError, match="snapshot ID"):
        build_candidate_review_packet(
            snapshot_id="../unsafe",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    external = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path / "external",
        snapshot_id="symlink-review",
        coverage=TEST_COVERAGE,
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / ".symlink-review.candidate").symlink_to(
        external.candidate_path,
        target_is_directory=True,
    )
    with pytest.raises(SnapshotQualityError, match="symlink"):
        build_candidate_review_packet(
            snapshot_id="symlink-review",
            snapshot_root=target,
            coverage=TEST_COVERAGE,
        )

    invalid = replace(source_record(), latitude=35.1796, longitude=129.0756)
    rejected = build_test_candidate(
        records=(invalid,),
        previous=None,
        output_root=tmp_path / "quality",
        snapshot_id="quality-review",
        coverage=TEST_COVERAGE,
    )
    assert rejected.issues
    with pytest.raises(SnapshotQualityError, match="coordinate|quality"):
        build_candidate_review_packet(
            snapshot_id=rejected.snapshot_id,
            snapshot_root=tmp_path / "quality",
            coverage=TEST_COVERAGE,
        )


def test_promotion_replays_coverage_for_persisted_active_site(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="tampered-coverage",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    sites_path = candidate.candidate_path / "sites.jsonl"
    site = json.loads(sites_path.read_text(encoding="utf-8"))
    site.update(
        {
            "latitude": 35.1796,
            "longitude": 129.0756,
            "routingAnchorLatitude": 35.1796,
            "routingAnchorLongitude": 129.0756,
        }
    )
    site_bytes = (json.dumps(site, ensure_ascii=False) + "\n").encode()
    sites_path.write_bytes(site_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="Seoul coverage"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_candidate_cannot_self_approve_before_promotion(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="self-approved",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["approved"] = True
    manifest["approvedAt"] = "2026-08-10T09:00:00Z"
    manifest["approvedByRole"] = "data-steward"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approved=false"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_promotion_rejects_candidate_from_another_snapshot_root(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "target"
    target_root.mkdir()
    external_root = tmp_path / "external"
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=external_root,
        snapshot_id="external-candidate",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, external_root)

    with pytest.raises(SnapshotQualityError, match="candidate path"):
        approve_test_candidate(
            candidate,
            target_root,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert candidate.candidate_path.is_dir()
    assert not (target_root / "current.json").exists()


def test_promotion_rejects_candidate_symlink(tmp_path: Path) -> None:
    external = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path / "external",
        snapshot_id="symlinked",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(external, tmp_path / "external")
    target_root = tmp_path / "target"
    target_root.mkdir()
    candidate_path = target_root / ".symlinked.candidate"
    candidate_path.symlink_to(external.candidate_path, target_is_directory=True)
    forged = replace(
        external,
        snapshot_id="symlinked",
        candidate_path=candidate_path,
        issues=(),
    )

    with pytest.raises(SnapshotQualityError, match="symlink"):
        approve_test_candidate(
            forged,
            target_root,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (target_root / "current.json").exists()


@pytest.mark.parametrize(
    "file_name",
    ["manifest.json", "institutions.jsonl", "sites.jsonl"],
)
def test_promotion_rejects_symlinked_candidate_file(
    tmp_path: Path,
    file_name: str,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"symlink-{file_name.split('.')[0]}",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    candidate_file = candidate.candidate_path / file_name
    external_file = tmp_path / f"external-{file_name}"
    candidate_file.rename(external_file)
    candidate_file.symlink_to(external_file)

    with pytest.raises(SnapshotQualityError, match="symlink"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_promotion_revalidates_safe_snapshot_slug(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="safe-slug",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    forged = replace(
        candidate,
        snapshot_id="../escaped-final",
        issues=(),
    )

    with pytest.raises(SnapshotQualityError, match="unsafe"):
        approve_test_candidate(
            forged,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path.parent / "escaped-final").exists()


def test_promotion_recounts_candidate_manifest_before_pointer_change(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="bad-count",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionCount"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="institutionCount"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_promotion_binds_source_digest_to_persisted_site_content(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="site-provenance-binding",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    sites_path = candidate.candidate_path / "sites.jsonl"
    site = json.loads(sites_path.read_text(encoding="utf-8"))
    site.update(
        {
            "roadAddress": "서울특별시 송파구 변조로 10",
            "district": "송파구",
            "latitude": 37.51,
            "longitude": 127.10,
            "routingAnchorLatitude": 37.51,
            "routingAnchorLongitude": 127.10,
        }
    )
    site_bytes = (json.dumps(site, ensure_ascii=False) + "\n").encode()
    sites_path.write_bytes(site_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_promotion_rejects_replacement_acquisition_provenance(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="replacement-acquisition",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0].update(
        {
            "rawSha256": "f" * 64,
            "pageCount": 199,
            "fetchedRowCount": 4_999,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_public_result_cannot_authorize_replaced_raw_provenance(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="forged-public-attestations",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["rawSha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    forged = replace(candidate, issues=())

    with pytest.raises(SnapshotQualityError, match="transaction attestation"):
        approve_test_candidate(
            forged,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_builder_writes_private_root_transaction_without_source_pii(
    tmp_path: Path,
) -> None:
    record = source_record()
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="durable-transaction",
        coverage=TEST_COVERAGE,
    )
    key_path = tmp_path / ".sync-attestation.key"
    receipt_path = tmp_path / ".sync-transactions" / "durable-transaction.json"

    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert receipt_path.is_file()
    assert candidate.candidate_path not in receipt_path.parents
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert record.official_name not in receipt_text
    assert record.road_address not in receipt_text


def test_builder_fsyncs_existing_root_before_durable_transaction_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="transaction-bootstrap",
        coverage=TEST_COVERAGE,
    )
    events: list[tuple[str, Path]] = []
    real_fsync_directory = sync_module._fsync_directory
    real_replace = os.replace

    def record_fsync(path: Path) -> None:
        events.append(("fsync", Path(path)))
        real_fsync_directory(path)

    def record_replace(source: str | Path, destination: str | Path) -> None:
        destination_path = Path(destination)
        if destination_path.parent.name == ".sync-transactions":
            events.append(("receipt-replace", destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(sync_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    second = build_test_candidate(
        records=(source_record(institution_id="neis:B10:7010002"),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="transaction-after-bootstrap",
        coverage=TEST_COVERAGE,
    )

    receipt_index = next(
        index for index, event in enumerate(events) if event[0] == "receipt-replace"
    )
    candidate_fsync_index = events.index(("fsync", second.candidate_path))
    root_fsync_indices = [
        index
        for index, event in enumerate(events)
        if event == ("fsync", tmp_path.resolve())
    ]
    assert candidate_fsync_index < root_fsync_indices[-1] < receipt_index


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_promotion_rejects_missing_or_tampered_build_transaction(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"{mutation}-transaction",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    receipt_path = (
        tmp_path / ".sync-transactions" / f"{candidate.snapshot_id}.json"
    )
    if mutation == "missing":
        receipt_path.unlink()
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["manifestSha256"] = "f" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="transaction"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_build_transaction_cannot_be_copied_between_output_roots(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=first_root,
        snapshot_id="copied-transaction",
        coverage=TEST_COVERAGE,
    )
    second = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=second_root,
        snapshot_id="copied-transaction",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(second, second_root)
    first_receipt = (
        first_root / ".sync-transactions" / "copied-transaction.json"
    )
    second_receipt = (
        second_root / ".sync-transactions" / "copied-transaction.json"
    )
    second_receipt.write_bytes(first_receipt.read_bytes())

    with pytest.raises(SnapshotQualityError, match="transaction attestation"):
        approve_test_candidate(
            second,
            second_root,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (second_root / "current.json").exists()
    assert first.candidate_path.is_dir()


def test_standard_enrichment_binds_selected_site_mapping(
    tmp_path: Path,
) -> None:
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="standard-selected-mapping",
        coverage=TEST_COVERAGE,
        enrichment_provenance=(
            standard_enrichment_provenance(matched_row_count=1),
        ),
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    sites_path = candidate.candidate_path / "sites.jsonl"
    site = json.loads(sites_path.read_text(encoding="utf-8"))
    site.update(
        {
            "latitude": 37.51,
            "longitude": 127.10,
            "routingAnchorLatitude": 37.51,
            "routingAnchorLongitude": 127.10,
        }
    )
    site_bytes = (json.dumps(site, ensure_ascii=False) + "\n").encode()
    sites_path.write_bytes(site_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered_record = replace(record, latitude=37.51, longitude=127.10)
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest["sources"][0]["normalizedSha256"] = normalized_records_sha256(
        [tampered_record]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="provenance"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_promotion_runs_task3_strict_checks_before_pointer(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="strict-before-pointer",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    institutions_path = candidate.candidate_path / "institutions.jsonl"
    institution = json.loads(institutions_path.read_text(encoding="utf-8"))
    institution["lastSeenSnapshot"] = "other-snapshot"
    institution_bytes = (
        json.dumps(institution, ensure_ascii=False) + "\n"
    ).encode()
    institutions_path.write_bytes(institution_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(institution_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="transaction attestation"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


# Production break caught: trusting a correctly signed candidate whose reviewed
# histogram does not contain the persisted institution's source vintage.
def test_review_and_approval_reject_signed_unobserved_institution_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_candidate_manifest = sync_module._candidate_manifest

    def forged_candidate_manifest(
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        manifest = cast(Any, real_candidate_manifest)(*args, **kwargs)
        source = cast(list[dict[str, object]], manifest["sources"])[0]
        source["sourceAsOf"] = "2026-08-09"
        source["sourceObservationDateCounts"] = {"2026-08-09": 1}
        return manifest

    with monkeypatch.context() as legacy_context:
        legacy_context.setattr(
            sync_module,
            "_candidate_manifest",
            forged_candidate_manifest,
        )
        candidate = build_test_candidate(
            records=(source_record(),),
            previous=None,
            output_root=tmp_path,
            snapshot_id="signed-unobserved-date",
            coverage=TEST_COVERAGE,
        )

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    with pytest.raises(SnapshotQualityError, match="source provenance"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest="0" * 64,
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


# Production break caught: accepting descending serialized observation-date keys
# after the canonical transaction and review digests conceal their ordering change.
def test_review_and_approval_reject_signed_unsorted_observation_histogram(
    tmp_path: Path,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="histogram-order-current",
        coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    records = (
        source_record(institution_id="neis:B10:7010001"),
        replace(
            source_record(institution_id="neis:B10:7010002"),
            source_as_of="2026-08-09",
        ),
    )
    candidate = build_test_candidate(
        records=records,
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="histogram-order-tampered",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["sourceObservationDateCounts"] = {
        "2026-08-10": 1,
        "2026-08-09": 1,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="manifest|provenance"):
        build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    with pytest.raises(SnapshotQualityError, match="manifest|provenance"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=review_digest,
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert (tmp_path / "current.json").read_bytes() == pointer_before


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("endpoint", "https://attacker.invalid/source"),
        ("requestRegionCode", "NOT-B10"),
        ("pageCount", 0),
        ("fetchedRowCount", 0),
        ("normalizedSha256", "f" * 64),
    ],
)
def test_promotion_replays_source_provenance_from_persisted_rows(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"promotion-source-{field_name}",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0][field_name] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_promotion_replays_enrichment_provenance_from_persisted_rows(
    tmp_path: Path,
) -> None:
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="promotion-enrichment",
        coverage=TEST_COVERAGE,
        enrichment_provenance=(
            standard_enrichment_provenance(matched_row_count=1),
        ),
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["enrichments"][0]["normalizedSha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="enrichment provenance"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_manifest_replays_live_source_provenance(tmp_path: Path) -> None:
    official_record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    provenance = SourceProvenance(
        source="NEIS",
        endpoint="https://open.neis.go.kr/hub/schoolInfo",
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Ministry of Education NEIS education data",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-08-10",
        raw_sha256="b" * 64,
        page_count=2,
        row_count=1,
        fetched_row_count=2,
        request_region_code="B10",
        request_timing=None,
        normalized_sha256=normalized_records_sha256(
            [_before_enrichment(official_record)]
        ),
        source_observation_date_counts=(("2026-08-10", 2),),
    )
    enrichment = EnrichmentProvenance(
        source="OFFICIAL_STANDARD_SCHOOL_LOCATION",
        endpoint=standard_school_module.DOWNLOAD_URL,
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Korea Education Facilities Safety Authority",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-03-20",
        raw_sha256=standard_school_module.PINNED_SHA256,
        normalized_sha256="ebb2643be10bda983ca9cb81a7ce2820474a53c2f65fc3ac6a7bcc179527cb4a",
        request_region_code="7010000",
        request_timing=None,
        page_count=1,
        fetched_row_count=12_011,
        matched_row_count=1,
        matched_normalized_sha256=enrichment_records_sha256(
            (official_record,),
            "OFFICIAL_STANDARD_COORDINATE",
        ),
    )

    candidate = build_test_candidate(
        records=(official_record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="provenance",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": provenance},
        enrichment_provenance=(enrichment,),
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["sources"][0]["rawSha256"] == "b" * 64
    assert manifest["sources"][0]["pageCount"] == 2
    assert manifest["sources"][0]["fetchedAt"] == "2026-08-10T09:00:00Z"
    assert manifest["sources"][0]["fetchedRowCount"] == 2
    assert manifest["sources"][0]["sourceObservationDateCounts"] == {
        "2026-08-10": 2
    }
    assert manifest["sources"][0]["normalizedRowCount"] == 1
    assert manifest["sources"][0]["preservedRowCount"] == 0
    assert manifest["sources"][0]["requestRegionCode"] == "B10"
    assert (
        manifest["enrichments"][0]["rawSha256"]
        == standard_school_module.PINNED_SHA256
    )
    assert manifest["enrichments"][0]["matchedRowCount"] == 1


def test_candidate_requires_matching_coordinate_enrichment(
    tmp_path: Path,
) -> None:
    for quality, message in (
        ("OFFICIAL_STANDARD_COORDINATE", "official school-location"),
        ("GEOCODED", "Kakao"),
    ):
        record = SourceInstitutionRecord(
            **{**source_record().__dict__, "coordinate_quality": quality}
        )
        with pytest.raises(SnapshotQualityError, match=message):
            build_test_candidate(
                records=(record,),
                previous=None,
                output_root=tmp_path / quality,
                snapshot_id=f"missing-{quality.lower()}",
                coverage=TEST_COVERAGE,
            )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("endpoint", "https://attacker.invalid/locations.csv"),
        ("request_region_code", "NOT-SEOUL"),
        ("page_count", 0),
        ("fetched_row_count", 0),
        ("matched_row_count", 0),
        ("raw_sha256", "c" * 64),
        ("normalized_sha256", "d" * 64),
    ],
)
def test_candidate_rejects_untrusted_standard_enrichment(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    valid = standard_enrichment_provenance(matched_row_count=1)
    invalid = EnrichmentProvenance(
        **{**valid.__dict__, field_name: value}
    )

    with pytest.raises(SnapshotQualityError, match="enrichment"):
        build_test_candidate(
            records=(record,),
            previous=None,
            output_root=tmp_path,
            snapshot_id=f"invalid-enrichment-{field_name}",
            coverage=TEST_COVERAGE,
            enrichment_provenance=(invalid,),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("endpoint", "https://attacker.invalid/source"),
        ("license_name", "UNVERIFIED"),
        ("attribution", "attacker"),
        ("request_region_code", "NOT-B10"),
        ("request_timing", "20261"),
        ("page_count", 0),
        ("page_count", 2),
        ("fetched_row_count", 0),
        ("row_count", 2),
        ("source_as_of", "2026-08-09"),
        ("normalized_sha256", "b" * 64),
        ("raw_sha256", "not-a-sha256"),
    ],
)
def test_candidate_rejects_untrusted_source_provenance(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    records = (source_record(),)
    valid = source_provenance_for(records)["NEIS"]
    invalid = SourceProvenance(**{**valid.__dict__, field_name: value})

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        build_test_candidate(
            records=records,
            previous=None,
            output_root=tmp_path,
            snapshot_id=f"invalid-provenance-{field_name}",
            coverage=TEST_COVERAGE,
            source_provenance={"NEIS": invalid},
        )


def test_pointer_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="recoverable",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_pointer_once(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer_once)
    with pytest.raises(OSError, match="simulated pointer failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()
    assert (tmp_path / "recoverable").is_dir()

    monkeypatch.setattr(os, "replace", real_replace)
    approve_test_candidate(
        candidate,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "recoverable"


def test_pointer_failure_restart_uses_durable_transaction_not_result_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="restart-from-transaction",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    monkeypatch.setattr(os, "replace", real_replace)
    snapshot_id = candidate.snapshot_id
    candidate_path = candidate.candidate_path
    del candidate
    restarted = SnapshotBuildResult(
        snapshot_id=snapshot_id,
        candidate_path=candidate_path,
        approved=False,
        issues=(),
    )

    approve_test_candidate(
        restarted,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )
    assert verify_snapshot(tmp_path).manifest.snapshot_id == snapshot_id


def test_restart_after_pointer_fsync_before_published_phase_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="pointer-written-before-phase",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_advance = sync_module._advance_build_transaction

    def fail_published_phase(
        root: Path,
        transaction: Mapping[str, object],
        *,
        phase: str,
        approved_manifest: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if phase == "PUBLISHED":
            raise OSError("simulated crash before published receipt")
        return real_advance(
            root,
            transaction,
            phase=phase,
            approved_manifest=approved_manifest,
        )

    monkeypatch.setattr(
        sync_module,
        "_advance_build_transaction",
        fail_published_phase,
    )
    with pytest.raises(OSError, match="before published receipt"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    pointer_before = (tmp_path / "current.json").read_bytes()
    receipt_path = (
        tmp_path
        / ".sync-transactions"
        / "pointer-written-before-phase.json"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["phase"] == (
        "POINTER_PREPARED"
    )
    monkeypatch.setattr(
        sync_module,
        "_advance_build_transaction",
        real_advance,
    )
    restarted = SnapshotBuildResult(
        snapshot_id=candidate.snapshot_id,
        candidate_path=candidate.candidate_path,
        approved=False,
        issues=(),
    )

    approve_test_candidate(
        restarted,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )

    assert (tmp_path / "current.json").read_bytes() == pointer_before
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["phase"] == (
        "PUBLISHED"
    )
    assert verify_snapshot(tmp_path).manifest.snapshot_id == candidate.snapshot_id


def test_published_transaction_cannot_authorize_a_different_current_pointer(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="published-pointer-binding",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    approve_test_candidate(
        candidate,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )
    (tmp_path / "current.json").write_text(
        json.dumps({"snapshotId": "different-snapshot"}),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotQualityError, match="pointer|current snapshot"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )

    assert json.loads((tmp_path / "current.json").read_text(encoding="utf-8")) == {
        "snapshotId": "different-snapshot"
    }


def test_pointer_failure_rejects_changed_attested_approval_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="changed-approved-at",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    monkeypatch.setattr(os, "replace", real_replace)
    manifest_path = tmp_path / candidate.snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["approvedAt"] = "2099-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approval phase"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_forged_approved_final_without_attested_phase_is_rejected(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="forged-approved-final",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    final_path = tmp_path / candidate.snapshot_id
    os.replace(candidate.candidate_path, final_path)
    manifest_path = final_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "approved": True,
            "approvedAt": manifest["createdAt"],
            "approvedByRole": "data-steward",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approval phase"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_manifest_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="manifest-recovery",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_manifest_once(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "manifest.json":
            raise OSError("simulated manifest replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest_once)
    with pytest.raises(OSError, match="manifest replacement failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()
    assert (tmp_path / "manifest-recovery").is_dir()

    monkeypatch.setattr(os, "replace", real_replace)
    approve_test_candidate(
        candidate,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "manifest-recovery"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("approvedAt", None), ("approvedByRole", "personal-account")],
)
def test_pointer_retry_validates_real_approved_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: object,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"retry-{field_name}",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    monkeypatch.setattr(os, "replace", real_replace)
    manifest_path = tmp_path / candidate.snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field_name] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approved manifest"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_pointer_retry_rejects_duplicate_approval_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="retry-duplicate-key",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    monkeypatch.setattr(os, "replace", real_replace)
    manifest_path = tmp_path / candidate.snapshot_id / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest_text.replace(
            '"approvedAt":',
            '"approvedAt":null,"approvedAt":',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotQualityError, match="duplicate JSON key"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_successful_promotion_retry_is_idempotent(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="already-current-retry",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    approve_test_candidate(
        candidate,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )
    first_pointer = (tmp_path / "current.json").read_bytes()

    approve_test_candidate(
        candidate,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )

    assert (tmp_path / "current.json").read_bytes() == first_pointer
    assert verify_snapshot(tmp_path).manifest.snapshot_id == candidate.snapshot_id


def test_promotion_rejects_duplicate_jsonl_key_before_pointer(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="duplicate-jsonl-key",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    institutions_path = candidate.candidate_path / "institutions.jsonl"
    line = institutions_path.read_text(encoding="utf-8")
    tampered = line.replace(
        "{",
        '{"institutionId":"../unsafe",',
        1,
    ).encode("utf-8")
    institutions_path.write_bytes(tampered)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(tampered).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="duplicate JSON key"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    assert not (tmp_path / "current.json").exists()


def test_candidate_directory_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="rename-recovery",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    real_replace = os.replace

    def fail_directory_once(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "rename-recovery":
            raise OSError("simulated directory replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_directory_once)
    with pytest.raises(OSError, match="directory replacement failure"):
        approve_test_candidate(
            candidate,
            tmp_path,
            coverage=TEST_COVERAGE,
            review_digest=review_digest,
        )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["approved"] is False
    assert not (tmp_path / "current.json").exists()

    monkeypatch.setattr(os, "replace", real_replace)
    approve_test_candidate(
        candidate,
        tmp_path,
        coverage=TEST_COVERAGE,
        review_digest=review_digest,
    )
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "rename-recovery"


def test_review_cli_is_offline_and_emits_only_review_packet(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="review-cli",
        coverage=TEST_COVERAGE,
    )
    command = [
        sys.executable,
        "apps/travel-map/scripts/review-institution-snapshot.py",
        "--snapshot-id",
        candidate.snapshot_id,
        "--snapshot-root",
        str(tmp_path),
        "--geodata-root",
        "apps/travel-map/resources/geodata",
    ]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "apps/travel-map",
    }

    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    packet = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert packet["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert completed.stderr == ""

    rejected = subprocess.run(
        [*command, "--env-file", "ignored"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "unrecognized arguments: --env-file ignored" in rejected.stderr


def test_approval_cli_is_offline_and_emits_exact_success_object(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="approval-cli",
        coverage=TEST_COVERAGE,
    )
    review_digest = review_test_candidate(candidate, tmp_path)
    command = [
        sys.executable,
        "apps/travel-map/scripts/approve-institution-snapshot.py",
        "--snapshot-id",
        candidate.snapshot_id,
        "--review-digest",
        review_digest,
        "--reviewer-role",
        "data-steward",
        "--snapshot-root",
        str(tmp_path),
        "--geodata-root",
        "apps/travel-map/resources/geodata",
    ]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "apps/travel-map",
    }

    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    expected = {
        "reviewDigest": review_digest,
        "snapshotId": candidate.snapshot_id,
        "status": "SNAPSHOT_APPROVED",
    }

    assert completed.returncode == 0
    assert completed.stdout == (
        json.dumps(
            expected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert completed.stderr == ""
    assert verify_snapshot(tmp_path).manifest.snapshot_id == candidate.snapshot_id
    for credential_name in (
        "NEIS_API_KEY",
        "KINDERGARTEN_API_KEY",
        "KAKAO_REST_API_KEY",
        "Authorization",
    ):
        assert credential_name not in completed.stdout + completed.stderr

    rejected = subprocess.run(
        [*command, "--env-file", "ignored"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "unrecognized arguments: --env-file ignored" in rejected.stderr


def test_sync_cli_fails_closed_without_credentials(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "apps/travel-map",
        "NEIS_API_KEY": "must-not-appear",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "apps/travel-map/scripts/sync-institutions.py",
            "--sen-csv",
            str(SOURCE_RESOURCES / "sen-institutions.csv"),
            "--region-codes",
            str(SOURCE_RESOURCES / "kindergarten-region-codes.csv"),
            "--snapshot-root",
            str(snapshot_root),
            "--geodata-root",
            "apps/travel-map/resources/geodata",
            "--timing",
            "20261",
        ],
        cwd=Path.cwd(),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "KINDERGARTEN_API_KEY" in completed.stderr
    assert "KAKAO_REST_API_KEY" in completed.stderr
    assert "must-not-appear" not in completed.stdout + completed.stderr
    assert not (snapshot_root / "current.json").exists()


def neis_payload(*, source_type: str) -> dict[str, object]:
    payload = copy.deepcopy(load_json("neis-school-info.json"))
    section = payload["schoolInfo"]
    assert type(section) is list
    section[0]["head"][0]["list_total_count"] = 1
    row = section[1]["row"][0]
    section[1]["row"] = [row]
    row.update(
        {
            "ATPT_OFCDC_SC_CODE": "B10",
            "ATPT_OFCDC_SC_NM": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            "SCHUL_NM": "\uac80\uc99d\ud559\uad50",
            "SCHUL_KND_SC_NM": source_type,
            "LCTN_SC_NM": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc",
            "JU_ORG_NM": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            "FOND_SC_NM": "\uacf5\ub9bd",
            "ORG_RDNMA": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1",
            "LOAD_DTM": "20260810",
        }
    )
    return payload


def kindergarten_payload() -> dict[str, object]:
    payload = copy.deepcopy(load_json("kindergarten-info.json"))
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row.update(
        {
            "officeedu": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            "subofficeedu": "\uc911\ubd80\uad50\uc721\uc9c0\uc6d0\uccad",
            "kindername": "\uac80\uc99d\uc720\uce58\uc6d0",
            "establish": "\uacf5\ub9bd(\ubcd1\uc124)",
            "addr": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \uac80\uc99d\ub85c 3",
        }
    )
    return payload


def write_region_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "regions.csv"
    path.write_text(
        (SOURCE_RESOURCES / "kindergarten-region-codes.csv").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return path


def build_test_candidate(
    *,
    records: tuple[SourceInstitutionRecord, ...],
    previous: VerifiedSnapshot | None,
    output_root: Path,
    snapshot_id: str,
    coverage: CoverageService | None = TEST_COVERAGE,
    source_provenance: Mapping[str, SourceProvenance] | None = None,
    enrichment_provenance: tuple[EnrichmentProvenance, ...] = (),
) -> SnapshotBuildResult:
    selected_provenance = (
        source_provenance
        if source_provenance is not None
        else source_provenance_for(records)
    )
    return build_candidate_snapshot(
        records=records,
        previous=previous,
        output_root=output_root,
        snapshot_id=snapshot_id,
        coverage=coverage,
        source_provenance=selected_provenance,
        enrichment_provenance=enrichment_provenance,
    )


def approve_test_candidate(
    candidate: SnapshotBuildResult,
    output_root: Path,
    *,
    coverage: CoverageService = TEST_COVERAGE,
    review_digest: str | None = None,
) -> str:
    digest = review_digest or review_test_candidate(
        candidate,
        output_root,
        coverage=coverage,
    )
    return approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=digest,
        reviewer_role="data-steward",
        snapshot_root=output_root,
        coverage=coverage,
    )


def review_test_candidate(
    candidate: SnapshotBuildResult,
    output_root: Path,
    *,
    coverage: CoverageService = TEST_COVERAGE,
) -> str:
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=output_root,
        coverage=coverage,
    )
    return cast(str, packet["reviewDigest"])


def source_provenance_for(
    records: tuple[SourceInstitutionRecord, ...],
) -> dict[str, SourceProvenance]:
    endpoints = {
        "NEIS": "https://open.neis.go.kr/hub/schoolInfo",
        "KINDERGARTEN_INFO": (
            "https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do"
        ),
        "SEN_REVIEWED_CSV": "https://www.sen.go.kr/www/website.jsp",
    }
    licenses = {
        "NEIS": "PUBLIC_DATA_NO_USE_RESTRICTION",
        "KINDERGARTEN_INFO": "PUBLIC_DATA_PORTAL_TERMS",
        "SEN_REVIEWED_CSV": "KOGL_TYPE_1_ATTRIBUTION",
    }
    attributions = {
        "NEIS": "Ministry of Education NEIS education data",
        "KINDERGARTEN_INFO": "Ministry of Education Kindergarten Info",
        "SEN_REVIEWED_CSV": (
            "Source: Seoul Metropolitan Office of Education "
            "(organization directory and 2026 civil-service handbook)"
        ),
    }
    regions = {
        "NEIS": "B10",
        "KINDERGARTEN_INFO": "11",
        "SEN_REVIEWED_CSV": "SEOUL",
    }
    grouped: dict[str, list[SourceInstitutionRecord]] = {}
    for record in records:
        grouped.setdefault(record.source, []).append(record)
    return {
        source: SourceProvenance(
            source=source,
            endpoint=endpoints[source],
            license_name=licenses[source],
            attribution=attributions[source],
            fetched_at="2026-08-10T09:00:00Z",
            source_as_of=max(record.source_as_of for record in source_records),
            raw_sha256=(
                "69863ac78689fb4b6e9941aabea03c3c1d618ccb26568e844079afd9092eb2c2"
                if source == "SEN_REVIEWED_CSV"
                else "a" * 64
            ),
            page_count=(25 if source == "KINDERGARTEN_INFO" else 1),
            row_count=len(source_records),
            fetched_row_count=(
                len(source_records) + 1
                if source == "SEN_REVIEWED_CSV"
                else len(source_records)
            ),
            request_region_code=regions[source],
            request_timing=(
                "20261" if source == "KINDERGARTEN_INFO" else None
            ),
            normalized_sha256=normalized_records_sha256(
                [_before_enrichment(record) for record in source_records]
            ),
            source_observation_date_counts=source_observation_date_counts_for(
                source,
                source_records,
            ),
        )
        for source, source_records in grouped.items()
    }


def source_observation_date_counts_for(
    source: str,
    records: list[SourceInstitutionRecord],
) -> tuple[tuple[str, int], ...]:
    counts = Counter(record.source_as_of for record in records)
    if source == "SEN_REVIEWED_CSV":
        counts[max(counts)] += 1
    return tuple(sorted(counts.items()))


def standard_enrichment_provenance(
    *,
    matched_row_count: int,
) -> EnrichmentProvenance:
    official_record = replace(
        source_record(),
        coordinate_quality="OFFICIAL_STANDARD_COORDINATE",
    )
    return EnrichmentProvenance(
        source="OFFICIAL_STANDARD_SCHOOL_LOCATION",
        endpoint=standard_school_module.DOWNLOAD_URL,
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Korea Education Facilities Safety Authority",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of=standard_school_module.PINNED_SOURCE_AS_OF,
        raw_sha256=standard_school_module.PINNED_SHA256,
        normalized_sha256=(
            "ebb2643be10bda983ca9cb81a7ce2820474a53c2f65fc3ac6a7bcc179527cb4a"
        ),
        request_region_code="7010000",
        request_timing=None,
        page_count=1,
        fetched_row_count=standard_school_module.PINNED_NATIONWIDE_COUNT,
        matched_row_count=matched_row_count,
        matched_normalized_sha256=enrichment_records_sha256(
            (official_record,),
            "OFFICIAL_STANDARD_COORDINATE",
        ),
    )


def _before_enrichment(
    record: SourceInstitutionRecord,
) -> SourceInstitutionRecord:
    if record.coordinate_quality in {
        "OFFICIAL_STANDARD_COORDINATE",
        "GEOCODED",
    }:
        record = replace(
            record,
            latitude=None,
            longitude=None,
            coordinate_quality="MISSING",
        )
    return replace(
        record,
        additional_sites=tuple(
            replace(
                site,
                latitude=None,
                longitude=None,
                coordinate_quality="MISSING",
            )
            if site.coordinate_quality in {
                "OFFICIAL_STANDARD_COORDINATE",
                "GEOCODED",
            }
            else site
            for site in record.additional_sites
        ),
    )


def reviewed_counts_fixture(counts: Mapping[str, int]) -> ReviewedSchoolCounts:
    evidence = SchoolCountEvidence(
        source_url=(
            "https://enews.sen.go.kr/uploads/img_smart//"
            "2026-06-08/20260608075519432.png"
        ),
        source_as_of="2026-03-10",
        source_sha256=(
            "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70"
        ),
        status="PRELIMINARY_2026",
    )
    return ReviewedSchoolCounts(
        normalized_sha256="a" * 64,
        license_name="KOGL_TYPE_1_ATTRIBUTION",
        attribution="Source: Seoul Metropolitan Office of Education",
        counts=dict(counts),
        category_evidence={name: evidence for name in counts},
        category_composition={name: name for name in counts},
        reported_totals=(
            ReportedSchoolTotal(
                expected_count=sum(counts.values()),
                population="+".join(counts),
                used_for_gate=False,
                evidence=evidence,
            ),
        ),
    )


def records_for_type_counts(
    counts: Mapping[str, int],
) -> tuple[SourceInstitutionRecord, ...]:
    records: list[SourceInstitutionRecord] = []
    sequence = 1
    for institution_type, count in counts.items():
        for _ in range(count):
            source = (
                "KINDERGARTEN_INFO"
                if institution_type == "KINDERGARTEN"
                else "NEIS"
            )
            institution_id = (
                f"kindergarten:{sequence:07d}"
                if source == "KINDERGARTEN_INFO"
                else f"neis:B10:{sequence:07d}"
            )
            records.append(
                replace(
                    source_record(
                        institution_id=institution_id
                    ),
                    institution_type=institution_type,
                    source=source,
                )
            )
            sequence += 1
    return tuple(records)


def source_record(
    *,
    institution_id: str = "neis:B10:7010001",
    road_address: str = "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1",
) -> SourceInstitutionRecord:
    return SourceInstitutionRecord(
        institution_id=institution_id,
        official_name="\uac80\uc99d\ud559\uad50",
        institution_type="ELEMENTARY_SCHOOL",
        foundation_type="PUBLIC",
        education_office="\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
        road_address=road_address,
        district="\uc911\uad6c",
        latitude=37.56,
        longitude=126.97,
        source="NEIS",
        source_region_code="B10",
        source_as_of="2026-08-10",
        coordinate_quality="MANUALLY_VERIFIED",
    )
