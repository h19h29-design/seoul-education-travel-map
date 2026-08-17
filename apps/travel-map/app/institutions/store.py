import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from app.institutions.facets import (
    InstitutionFacetOption,
    InstitutionFacets,
    canonical_education_office,
    institution_display_name,
)
from app.institutions.models import (
    Institution,
    InstitutionSearchItem,
    InstitutionSearchPage,
    InstitutionSite,
    InstitutionStatus,
)
from app.institutions.snapshot import verify_snapshot
from app.routing.models import Coordinate

_PARENTHESES = frozenset("()（）")
_KOREAN_INITIALS = (
    "ㄱ",
    "ㄲ",
    "ㄴ",
    "ㄷ",
    "ㄸ",
    "ㄹ",
    "ㅁ",
    "ㅂ",
    "ㅃ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅉ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
_KOREAN_INITIAL_SET = frozenset(_KOREAN_INITIALS)


class UnknownSiteError(LookupError):
    def __init__(self, site_id: str) -> None:
        self.site_id = site_id
        super().__init__(f"unknown or inactive institution site: {site_id}")


@dataclass(frozen=True)
class _SearchRecord:
    institution: Institution
    site: InstitutionSite
    item: InstitutionSearchItem
    normalized_name: str
    terms: tuple[str, ...]
    initial_terms: tuple[str, ...]


class InstitutionStore:
    def __init__(
        self,
        *,
        active_sites: dict[str, InstitutionSite],
        records: tuple[_SearchRecord, ...],
        display_names: Mapping[str, str],
        facets: InstitutionFacets,
    ) -> None:
        self._active_sites = active_sites
        self._records = records
        self._display_names = MappingProxyType(dict(display_names))
        self._facets = facets

    @classmethod
    def load(cls, snapshot_root: Path) -> "InstitutionStore":
        verified = verify_snapshot(snapshot_root)
        institutions = {
            institution.institution_id: institution
            for institution in verified.institutions
        }
        active_sites: dict[str, InstitutionSite] = {}
        active_pairs: list[tuple[Institution, InstitutionSite]] = []
        for site in verified.sites:
            institution = institutions[site.institution_id]
            if (
                institution.status is not InstitutionStatus.ACTIVE
                or site.status is not InstitutionStatus.ACTIVE
            ):
                continue
            active_sites[site.site_id] = site
            active_pairs.append((institution, site))
        active_site_counts = Counter(
            institution.institution_id for institution, _site in active_pairs
        )
        records: list[_SearchRecord] = []
        display_names: dict[str, str] = {}
        for institution, site in active_pairs:
            education_office, _office_label = canonical_education_office(
                institution.education_office
            )
            display_name = institution_display_name(
                institution.official_name,
                site.site_name,
                active_site_counts[institution.institution_id],
            )
            display_names[site.site_id] = display_name
            item = InstitutionSearchItem(
                institution_id=institution.institution_id,
                site_id=site.site_id,
                site_name=site.site_name,
                official_name=institution.official_name,
                display_name=display_name,
                institution_type=institution.institution_type,
                foundation_type=institution.foundation_type,
                education_office=education_office,
                road_address=site.road_address,
                district=site.district,
                coordinate=Coordinate(
                    latitude=_routing_anchor(site, "latitude"),
                    longitude=_routing_anchor(site, "longitude"),
                ),
                coordinate_quality=site.coordinate_quality,
                snapshot_id=verified.manifest.snapshot_id,
                snapshot_as_of=verified.manifest.snapshot_as_of,
            )
            terms = _deduplicate(
                _normalize(value)
                for value in (
                    institution.official_name,
                    *institution.aliases,
                    site.site_name,
                    institution.official_name + site.site_name,
                    site.site_id,
                )
            )
            records.append(
                _SearchRecord(
                    institution=institution,
                    site=site,
                    item=item,
                    normalized_name=_normalize(institution.official_name),
                    terms=terms,
                    initial_terms=_deduplicate(
                        _initial_consonants(term) for term in terms
                    ),
                )
            )
        return cls(
            active_sites=active_sites,
            records=tuple(records),
            display_names=display_names,
            facets=_build_facets(verified.manifest.snapshot_id, tuple(records)),
        )

    def facets(self) -> InstitutionFacets:
        return self._facets

    def search(
        self,
        query: str = "",
        institution_type: str | None = None,
        foundation_type: str | None = None,
        education_office: str | None = None,
        district: str | None = None,
        limit: int = 20,
    ) -> tuple[InstitutionSearchItem, ...]:
        return self.search_page(
            query=query,
            institution_type=institution_type,
            foundation_type=foundation_type,
            education_office=education_office,
            district=district,
            limit=limit,
            offset=0,
        ).items

    def search_page(
        self,
        *,
        query: str = "",
        institution_type: str | None = None,
        foundation_type: str | None = None,
        education_office: str | None = None,
        district: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> InstitutionSearchPage:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        if type(offset) is not int or not 0 <= offset <= 100_000:
            raise ValueError("offset must be an integer from 0 to 100000")
        if type(query) is not str:
            raise TypeError("query must be a string")
        for filter_name, filter_value in (
            ("institution_type", institution_type),
            ("foundation_type", foundation_type),
            ("education_office", education_office),
            ("district", district),
        ):
            if filter_value is not None and type(filter_value) is not str:
                raise TypeError(f"{filter_name} must be a string or None")
        canonical_filter_office = _canonical_filter_office(education_office)
        normalized_query = _normalize(query)
        use_initial_index = bool(normalized_query) and all(
            character in _KOREAN_INITIAL_SET for character in normalized_query
        )
        matches: list[tuple[int, str, str, InstitutionSearchItem]] = []
        for record in self._records:
            if not _matches_filters(
                record,
                institution_type=institution_type,
                foundation_type=foundation_type,
                education_office=canonical_filter_office,
                district=district,
            ):
                continue
            if not normalized_query:
                rank = 0
            else:
                terms = record.initial_terms if use_initial_index else record.terms
                matched_rank = _match_rank(normalized_query, terms)
                if matched_rank is None:
                    continue
                rank = matched_rank
            matches.append(
                (rank, record.normalized_name, record.site.site_id, record.item)
            )
        matches.sort(key=lambda match: match[:3])
        items = tuple(match[3] for match in matches[offset : offset + limit])
        next_offset = offset + len(items)
        return InstitutionSearchPage(
            items=items,
            total=len(matches),
            next_offset=next_offset if next_offset < len(matches) else None,
            snapshot_id=self._facets.snapshot_id,
        )

    def require_site(self, site_id: str) -> InstitutionSite:
        if type(site_id) is not str:
            raise TypeError("site_id must be a string")
        site = self._active_sites.get(site_id)
        if site is None:
            raise UnknownSiteError(site_id)
        return site

    def display_name_for_site(self, site_id: str) -> str:
        if type(site_id) is not str:
            raise TypeError("site_id must be a string")
        try:
            return self._display_names[site_id]
        except KeyError as error:
            raise UnknownSiteError(site_id) from error


def _matches_filters(
    record: _SearchRecord,
    *,
    institution_type: str | None,
    foundation_type: str | None,
    education_office: str | None,
    district: str | None,
) -> bool:
    institution = record.institution
    site = record.site
    return (
        (institution_type is None or institution.institution_type == institution_type)
        and (foundation_type is None or institution.foundation_type == foundation_type)
        and (
            education_office is None or record.item.education_office == education_office
        )
        and (district is None or site.district == district)
    )


def _routing_anchor(site: InstitutionSite, axis: str) -> float:
    value = getattr(site, f"routing_anchor_{axis}")
    if value is None:
        raise ValueError("active institution site has no verified routing anchor")
    return value


def _canonical_filter_office(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        canonical, _label = canonical_education_office(value)
    except ValueError:
        return value
    return canonical


def _build_facets(
    snapshot_id: str,
    records: tuple[_SearchRecord, ...],
) -> InstitutionFacets:
    return InstitutionFacets(
        snapshot_id=snapshot_id,
        institution_types=_facet_options(
            Counter(record.item.institution_type for record in records)
        ),
        foundation_types=_facet_options(
            Counter(record.item.foundation_type for record in records)
        ),
        education_offices=_facet_options(
            Counter(
                record.item.education_office
                for record in records
                if record.item.education_office is not None
            ),
            labels={
                value: label
                for value, label in (
                    canonical_education_office(record.institution.education_office)
                    for record in records
                )
                if value is not None and label is not None
            },
        ),
        districts=_facet_options(Counter(record.item.district for record in records)),
    )


def _facet_options(
    counts: Counter[str],
    *,
    labels: dict[str, str] | None = None,
) -> tuple[InstitutionFacetOption, ...]:
    return tuple(
        InstitutionFacetOption(
            value=value,
            label=labels[value] if labels is not None else value,
            count=count,
        )
        for value, count in sorted(counts.items())
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and character not in _PARENTHESES
    )


def _initial_consonants(value: str) -> str:
    result: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 0xAC00 <= codepoint <= 0xD7A3:
            result.append(_KOREAN_INITIALS[(codepoint - 0xAC00) // 588])
        else:
            result.append(character)
    return "".join(result)


def _match_rank(query: str, terms: tuple[str, ...]) -> int | None:
    if any(term == query for term in terms):
        return 0
    if any(term.startswith(query) for term in terms):
        return 1
    if any(query in term for term in terms):
        return 2
    return None


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
