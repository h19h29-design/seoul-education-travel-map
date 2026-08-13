import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.institutions.sources.common import SourceDataError

_PRELIMINARY_URL = (
    "https://enews.sen.go.kr/uploads/img_smart//2026-06-08/"
    "20260608075519432.png"
)
_PRELIMINARY_AS_OF = "2026-03-10"
_PRELIMINARY_SHA256 = (
    "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70"
)
_KINDERGARTEN_DISCLOSURE_URL = "https://e-childschoolinfo.moe.go.kr/?mi=2782"
_KINDERGARTEN_DISCLOSURE_AS_OF = "2026-04-01"
_KINDERGARTEN_DISCLOSURE_SHA256 = (
    "a64b2af7fcbe91892b438b80cfbca16567cbfd898d3f23d26ad24d66cd9ec0bd"
)
_KINDERGARTEN_DISCLOSURE_NORMALIZED_SHA256 = (
    "a88594b566286fa898796ceed8f26719cc88d5b0d546970abac2ffb2e6adcdc1"
)
_DETAILED_CORROBORATING_URL = (
    "https://www.sen.go.kr/www/information/statistics/"
    "statistics_2/statistics_2025.jsp"
)
_DETAILED_CORROBORATING_SHA256 = (
    "8d3791e2ebf84799c7af53be0d662a4eaeb922bab3e85f0c82fe08793b1bd26b"
)
_NORMALIZED_SHA256 = (
    "532225bc7f1d2dd63e976880e53a4217b548e83e7dbc278363808aba41132907"
)
_LICENSE_NAME = "KOGL_TYPE_1_AND_PUBLIC_DATA_PORTAL_TERMS"
_ATTRIBUTION = (
    "Sources: Seoul Metropolitan Office of Education and "
    "Ministry of Education Kindergarten Info"
)
_REPORTED_POPULATION = (
    "KINDERGARTEN+ELEMENTARY_SCHOOL+MIDDLE_SCHOOL+HIGH_SCHOOL+"
    "SPECIAL_SCHOOL+MISC_SCHOOL"
)
_CATEGORY_COMPOSITION = {
    "KINDERGARTEN": "유치원",
    "ELEMENTARY_SCHOOL": "초등학교",
    "MIDDLE_SCHOOL": "중학교",
    "HIGH_SCHOOL": "고등학교",
    "SPECIAL_SCHOOL": "특수학교",
    "MISC_SCHOOL": "각종학교17+고등기술학교1",
}
_EXPECTED_ROWS = {
    "KINDERGARTEN": (
        706,
        _KINDERGARTEN_DISCLOSURE_URL,
        _KINDERGARTEN_DISCLOSURE_AS_OF,
        _KINDERGARTEN_DISCLOSURE_SHA256,
        "OFFICIAL_DISCLOSURE_2026_04",
    ),
    "ELEMENTARY_SCHOOL": (
        609,
        _PRELIMINARY_URL,
        _PRELIMINARY_AS_OF,
        _PRELIMINARY_SHA256,
        "PRELIMINARY_2026",
    ),
    "MIDDLE_SCHOOL": (
        390,
        _PRELIMINARY_URL,
        _PRELIMINARY_AS_OF,
        _PRELIMINARY_SHA256,
        "PRELIMINARY_2026",
    ),
    "HIGH_SCHOOL": (
        319,
        _PRELIMINARY_URL,
        _PRELIMINARY_AS_OF,
        _PRELIMINARY_SHA256,
        "PRELIMINARY_2026",
    ),
    "SPECIAL_SCHOOL": (
        32,
        _PRELIMINARY_URL,
        _PRELIMINARY_AS_OF,
        _PRELIMINARY_SHA256,
        "PRELIMINARY_2026",
    ),
    "MISC_SCHOOL": (
        18,
        _PRELIMINARY_URL,
        _PRELIMINARY_AS_OF,
        _PRELIMINARY_SHA256,
        "PRELIMINARY_2026",
    ),
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SchoolCountEvidence:
    source_url: str
    source_as_of: str
    source_sha256: str
    status: str


@dataclass(frozen=True)
class ReportedSchoolTotal:
    expected_count: int
    population: str
    used_for_gate: bool
    evidence: SchoolCountEvidence


@dataclass(frozen=True)
class ReviewedSchoolCounts:
    normalized_sha256: str
    license_name: str
    attribution: str
    counts: dict[str, int]
    category_evidence: dict[str, SchoolCountEvidence]
    category_composition: dict[str, str]
    reported_totals: tuple[ReportedSchoolTotal, ...]


def load_reviewed_school_counts(path: Path) -> ReviewedSchoolCounts:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    metadata: dict[str, str] = {}
    data_lines: list[str] = []
    canonical_lines: list[str] = []
    for line in lines:
        if line.startswith("# "):
            key, separator, value = line[2:].partition("=")
            if not separator or not key or not value or key in metadata:
                raise SourceDataError("SEN school count metadata is invalid")
            metadata[key] = value
            if key != "normalized_sha256":
                canonical_lines.append(line)
        elif line.strip():
            data_lines.append(line)
            canonical_lines.append(line)
    expected_metadata = {
        "normalized_sha256",
        "license_name",
        "attribution",
        "detailed_corroborating_url",
        "detailed_corroborating_raw_sha256",
        "preliminary_table_source_url",
        "preliminary_table_source_raw_sha256",
        "kindergarten_disclosure_source_url",
        "kindergarten_disclosure_source_raw_sha256",
        "kindergarten_disclosure_normalized_sha256",
        "kindergarten_disclosure_sido_code",
        "kindergarten_disclosure_district_count",
        "kindergarten_disclosure_total",
        "kindergarten_disclosure_public",
        "kindergarten_disclosure_private",
        "misc_school_composition",
        "reported_total_count",
        "reported_total_population",
        "reported_total_evidence_url",
        "reported_total_evidence_as_of",
        "reported_total_evidence_raw_sha256",
        "reported_total_evidence_status",
        "reported_total_used_for_gate",
    }
    if set(metadata) != expected_metadata:
        raise SourceDataError("SEN school count provenance is incomplete")
    normalized_sha256 = hashlib.sha256(
        ("\n".join(canonical_lines) + "\n").encode("utf-8")
    ).hexdigest()
    if (
        metadata["normalized_sha256"] != _NORMALIZED_SHA256
        or metadata["normalized_sha256"] != normalized_sha256
        or metadata["license_name"] != _LICENSE_NAME
        or metadata["attribution"] != _ATTRIBUTION
        or metadata["detailed_corroborating_url"]
        != _DETAILED_CORROBORATING_URL
        or metadata["detailed_corroborating_raw_sha256"]
        != _DETAILED_CORROBORATING_SHA256
        or metadata["preliminary_table_source_url"] != _PRELIMINARY_URL
        or metadata["preliminary_table_source_raw_sha256"]
        != _PRELIMINARY_SHA256
        or metadata["kindergarten_disclosure_source_url"]
        != _KINDERGARTEN_DISCLOSURE_URL
        or metadata["kindergarten_disclosure_source_raw_sha256"]
        != _KINDERGARTEN_DISCLOSURE_SHA256
        or metadata["kindergarten_disclosure_normalized_sha256"]
        != _KINDERGARTEN_DISCLOSURE_NORMALIZED_SHA256
        or metadata["kindergarten_disclosure_sido_code"] != "11"
        or metadata["kindergarten_disclosure_district_count"] != "25"
        or metadata["kindergarten_disclosure_total"] != "706"
        or metadata["kindergarten_disclosure_public"] != "295"
        or metadata["kindergarten_disclosure_private"] != "411"
        or metadata["misc_school_composition"]
        != _CATEGORY_COMPOSITION["MISC_SCHOOL"]
        or metadata["reported_total_count"] != "2092"
        or metadata["reported_total_population"] != _REPORTED_POPULATION
        or metadata["reported_total_evidence_url"] != _PRELIMINARY_URL
        or metadata["reported_total_evidence_as_of"] != _PRELIMINARY_AS_OF
        or metadata["reported_total_evidence_raw_sha256"]
        != _PRELIMINARY_SHA256
        or metadata["reported_total_evidence_status"] != "PRELIMINARY_2026"
        or metadata["reported_total_used_for_gate"] != "false"
        or any(
            _SHA256.fullmatch(metadata[field]) is None
            for field in (
                "normalized_sha256",
                "detailed_corroborating_raw_sha256",
                "preliminary_table_source_raw_sha256",
                "kindergarten_disclosure_source_raw_sha256",
                "kindergarten_disclosure_normalized_sha256",
                "reported_total_evidence_raw_sha256",
            )
        )
    ):
        raise SourceDataError("SEN school count resource is not reviewed")

    reader = csv.DictReader(data_lines)
    if reader.fieldnames != [
        "institution_type",
        "count",
        "evidence_url",
        "evidence_as_of",
        "evidence_raw_sha256",
        "evidence_status",
    ]:
        raise SourceDataError("SEN school count fields are invalid")
    counts: dict[str, int] = {}
    category_evidence: dict[str, SchoolCountEvidence] = {}
    for row in reader:
        institution_type = (row.get("institution_type") or "").strip()
        if institution_type in counts or institution_type not in _EXPECTED_ROWS:
            raise SourceDataError("SEN school count type is invalid")
        try:
            count = int((row.get("count") or "").strip())
        except ValueError as exc:
            raise SourceDataError("SEN school count is invalid") from exc
        evidence = SchoolCountEvidence(
            source_url=(row.get("evidence_url") or "").strip(),
            source_as_of=(row.get("evidence_as_of") or "").strip(),
            source_sha256=(row.get("evidence_raw_sha256") or "").strip(),
            status=(row.get("evidence_status") or "").strip(),
        )
        expected = _EXPECTED_ROWS[institution_type]
        if (
            (count, evidence.source_url, evidence.source_as_of,
             evidence.source_sha256, evidence.status)
            != expected
            or _SHA256.fullmatch(evidence.source_sha256) is None
        ):
            raise SourceDataError("SEN school count evidence is not reviewed")
        counts[institution_type] = count
        category_evidence[institution_type] = evidence
    if set(counts) != set(_EXPECTED_ROWS):
        raise SourceDataError("SEN school count categories are incomplete")
    preliminary_evidence = SchoolCountEvidence(
        source_url=_PRELIMINARY_URL,
        source_as_of=_PRELIMINARY_AS_OF,
        source_sha256=_PRELIMINARY_SHA256,
        status="PRELIMINARY_2026",
    )
    return ReviewedSchoolCounts(
        normalized_sha256=metadata["normalized_sha256"],
        license_name=metadata["license_name"],
        attribution=metadata["attribution"],
        counts=counts,
        category_evidence=category_evidence,
        category_composition=dict(_CATEGORY_COMPOSITION),
        reported_totals=(
            ReportedSchoolTotal(
                expected_count=2_092,
                population=_REPORTED_POPULATION,
                used_for_gate=False,
                evidence=preliminary_evidence,
            ),
        ),
    )
