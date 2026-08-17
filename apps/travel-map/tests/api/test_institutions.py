from pathlib import Path

from tests.institutions.test_store import load_store_with_verified_unclassified_school


# Break caught: returning a private snapshot record or snake_case API field names.
def test_institutions_search_returns_public_camel_case_records(client) -> None:
    response = client.get("/api/v1/institutions", params={"q": "샘물"})

    assert response.status_code == 200
    body = response.json()
    assert "test-neis:B10:SEMWATER-ES:main" in {
        item["siteId"] for item in body["items"]
    }
    assert "site_id" not in body["items"][0]


# Production mutation caught: requiring an anonymous text query even when an
# approved server-side facet alone identifies the institution search result.
def test_institutions_api_lists_items_for_filter_only_blank_query(client) -> None:
    response = client.get(
        "/api/v1/institutions",
        params={"institution_type": "KINDERGARTEN"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["siteId"] for item in body["items"]] == [
        "test-neis:B10:SEMWATER-KG:main"
    ]
    assert body["total"] == 1
    assert body["nextOffset"] is None
    assert body["snapshotId"] == "fixture-001"


# Production mutation caught: leaking source-office strings or snake_case facet
# fields instead of counted canonical options at the anonymous API boundary.
def test_institution_facets_api_uses_counted_camel_case_options(client) -> None:
    response = client.get("/api/v1/institutions/facets")

    assert response.status_code == 200
    body = response.json()
    assert body["snapshotId"] == "fixture-001"
    assert body["institutionTypes"] == [
        {"value": "ELEMENTARY_SCHOOL", "label": "ELEMENTARY_SCHOOL", "count": 5},
        {"value": "KINDERGARTEN", "label": "KINDERGARTEN", "count": 1},
    ]
    assert {
        item["value"]: (item["label"], item["count"])
        for item in body["educationOffices"]
    }["SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO"] == ("강남서초교육지원청", 1)
    assert "institution_types" not in body


def test_institutions_search_applies_normalized_type_and_foundation_filters(
    client,
) -> None:
    response = client.get(
        "/api/v1/institutions",
        params={
            "q": "샘물",
            "institution_type": "ELEMENTARY_SCHOOL",
            "foundation_type": "PUBLIC",
        },
    )

    assert response.status_code == 200
    assert [item["siteId"] for item in response.json()["items"]] == [
        "test-neis:B10:SEMWATER-ES:main"
    ]


# Break caught: leaking a review-required school through the public API despite
# its valid stored coordinates, either by name or by supported filters.
def test_institutions_api_excludes_review_required_school_from_name_and_filters(
    client,
) -> None:
    by_name = client.get("/api/v1/institutions", params={"q": "검토학교"})
    by_filter = client.get(
        "/api/v1/institutions",
        params={"institution_type": "ELEMENTARY_SCHOOL"},
    )

    assert by_name.status_code == 200
    assert by_name.json()["items"] == []
    assert by_filter.status_code == 200
    assert "test-neis:B10:REVIEW-PARENT:main" not in {
        item["siteId"] for item in by_filter.json()["items"]
    }


def test_institutions_api_excludes_verified_unclassified_school_from_name_and_filters(
    client,
    tmp_path: Path,
) -> None:
    client.app.state.dependencies.institutions = (
        load_store_with_verified_unclassified_school(tmp_path)
    )

    by_name = client.get("/api/v1/institutions", params={"q": "공개 제외"})
    by_filter = client.get(
        "/api/v1/institutions",
        params={"institution_type": "UNCLASSIFIED_SCHOOL"},
    )

    assert by_name.status_code == 200
    assert by_name.json()["items"] == []
    assert by_filter.status_code == 200
    assert by_filter.json()["items"] == []
