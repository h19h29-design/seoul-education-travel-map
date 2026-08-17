from app.institutions.facets import canonical_education_office
from app.institutions.models import InstitutionStatus
from app.institutions.snapshot import verify_snapshot
from app.institutions.store import InstitutionStore
from tests.institutions.test_store import SNAPSHOT_ROOT


# Production mutation caught: treating prefixed support-office source values as
# separate public filters, or silently publishing an unregistered office value.
def test_canonical_office_merges_prefixed_and_unprefixed_support_offices() -> None:
    assert canonical_education_office("강남서초교육지원청") == (
        "SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO",
        "강남서초교육지원청",
    )
    assert canonical_education_office("서울특별시강남서초교육지원청") == (
        "SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO",
        "강남서초교육지원청",
    )
    assert canonical_education_office("서울특별시교육청") == (
        "SEOUL_EDU_OFFICE",
        "서울특별시교육청",
    )
    assert canonical_education_office("교육부") == (
        "MINISTRY_OF_EDUCATION",
        "교육부",
    )
    assert canonical_education_office(None) == (None, None)

    try:
        canonical_education_office("알 수 없는 교육지원청")
    except ValueError as error:
        assert "unknown education office" in str(error)
    else:
        raise AssertionError("unknown office must not become a public facet ID")


# Production mutation caught: adding an active source office without a matching
# canonical registry entry, which would make a public facet unstable or invalid.
def test_canonical_office_registry_covers_every_active_snapshot_office() -> None:
    verified = verify_snapshot(SNAPSHOT_ROOT)
    active_offices = {
        institution.education_office
        for institution in verified.institutions
        if institution.status is InstitutionStatus.ACTIVE
        and institution.education_office is not None
        and institution.education_office.strip()
    }

    assert verified.manifest.approved is True
    assert active_offices
    canonical_offices = {
        canonical_education_office(raw_office)[0] for raw_office in active_offices
    }
    assert canonical_offices == {
        "SEOUL_EDU_OFFICE",
        "SEOUL_EDU_SUPPORT_DONGBU",
        "SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO",
        "SEOUL_EDU_SUPPORT_SEOBU",
    }


# Production mutation caught: deriving filter counts from manifest totals or
# quarantined/inactive rows rather than the selectable active site records.
def test_facets_include_all_active_values_and_exclude_quarantined_records() -> None:
    facets = InstitutionStore.load(SNAPSHOT_ROOT).facets()

    assert facets.snapshot_id == "fixture-001"
    assert [(item.value, item.count) for item in facets.institution_types] == [
        ("ELEMENTARY_SCHOOL", 5),
        ("KINDERGARTEN", 1),
    ]
    assert [(item.value, item.count) for item in facets.foundation_types] == [
        ("PUBLIC", 6)
    ]
    assert [
        (item.value, item.label, item.count) for item in facets.education_offices
    ] == [
        ("SEOUL_EDU_OFFICE", "서울특별시교육청", 2),
        ("SEOUL_EDU_SUPPORT_DONGBU", "동부교육지원청", 2),
        ("SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO", "강남서초교육지원청", 1),
        ("SEOUL_EDU_SUPPORT_SEOBU", "서부교육지원청", 1),
    ]
    assert [(item.value, item.count) for item in facets.districts] == [
        ("강남구", 1),
        ("동대문구", 1),
        ("샘물구", 2),
        ("성동구", 1),
        ("은평구", 1),
    ]
