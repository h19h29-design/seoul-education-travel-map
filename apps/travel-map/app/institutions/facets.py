from dataclasses import dataclass


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


_EDUCATION_OFFICES = (
    ("SEOUL_EDU_OFFICE", "서울특별시교육청", ("서울특별시교육청",)),
    ("MINISTRY_OF_EDUCATION", "교육부", ("교육부",)),
    ("SEOUL_EDU_SUPPORT_DONGBU", "동부교육지원청", ("동부교육지원청",)),
    ("SEOUL_EDU_SUPPORT_SEOBU", "서부교육지원청", ("서부교육지원청",)),
    ("SEOUL_EDU_SUPPORT_NAMBU", "남부교육지원청", ("남부교육지원청",)),
    ("SEOUL_EDU_SUPPORT_BUKBU", "북부교육지원청", ("북부교육지원청",)),
    ("SEOUL_EDU_SUPPORT_JUNGBU", "중부교육지원청", ("중부교육지원청",)),
    (
        "SEOUL_EDU_SUPPORT_GANGDONG_SONGPA",
        "강동송파교육지원청",
        ("강동송파교육지원청",),
    ),
    (
        "SEOUL_EDU_SUPPORT_GANGSEO_YANGCHEON",
        "강서양천교육지원청",
        ("강서양천교육지원청",),
    ),
    (
        "SEOUL_EDU_SUPPORT_GANGNAM_SEOCHO",
        "강남서초교육지원청",
        ("강남서초교육지원청",),
    ),
    (
        "SEOUL_EDU_SUPPORT_DONGJAK_GWANAK",
        "동작관악교육지원청",
        ("동작관악교육지원청",),
    ),
    (
        "SEOUL_EDU_SUPPORT_SEONGBUK_GANGBU",
        "성북강북교육지원청",
        ("성북강북교육지원청",),
    ),
    (
        "SEOUL_EDU_SUPPORT_SEONGDONG_GWANGJIN",
        "성동광진교육지원청",
        ("성동광진교육지원청",),
    ),
)
_OFFICE_BY_ALIAS = {
    alias: (value, label)
    for value, label, aliases in _EDUCATION_OFFICES
    for alias in aliases
}
_OFFICE_BY_VALUE = {
    value: (value, label) for value, label, _aliases in _EDUCATION_OFFICES
}
_SUPPORT_OFFICE_LABELS = frozenset(
    label
    for value, label, _aliases in _EDUCATION_OFFICES
    if value.startswith("SEOUL_EDU_SUPPORT_")
)


def canonical_education_office(value: str | None) -> tuple[str | None, str | None]:
    """Return the stable public office ID and Korean label for an approved value."""
    if value is None or not value.strip():
        return (None, None)
    if value in _OFFICE_BY_VALUE:
        return _OFFICE_BY_VALUE[value]
    if value in _OFFICE_BY_ALIAS:
        return _OFFICE_BY_ALIAS[value]
    if value.startswith("서울특별시"):
        stripped = value.removeprefix("서울특별시")
        if stripped in _SUPPORT_OFFICE_LABELS:
            return _OFFICE_BY_ALIAS[stripped]
    raise ValueError(f"unknown education office: {value}")


def institution_display_name(
    official_name: str,
    site_name: str,
    site_count: int,
) -> str:
    if site_count == 1 or site_name == "main" or site_name == official_name:
        return official_name
    return f"{official_name} · {site_name}"
