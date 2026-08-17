from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CoverageState(StrEnum):
    SEOUL = "SEOUL"
    BUFFER = "BUFFER"
    OUTSIDE = "OUTSIDE"


class PolicyProfile(StrEnum):
    SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED = "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"
    NATIONAL_PUBLIC_OFFICIAL_CONFIRMED = "NATIONAL_PUBLIC_OFFICIAL_CONFIRMED"
    INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER = (
        "INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER"
    )
    NONPUBLIC_OR_UNKNOWN = "NONPUBLIC_OR_UNKNOWN"


class VehicleUse(StrEnum):
    NONE = "NONE"
    PRIVATE = "PRIVATE"
    OFFICIAL_OR_RENTED = "OFFICIAL_OR_RENTED"
    ASSIGNED_OFFICIAL = "ASSIGNED_OFFICIAL"


class Classification(StrEnum):
    LOCAL = "LOCAL"
    NON_LOCAL_EXPECTED = "NON_LOCAL_EXPECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class AllowanceStatus(StrEnum):
    ESTIMATED = "ESTIMATED"
    REFERENCE_ESTIMATE = "REFERENCE_ESTIMATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class DistanceEvidenceBasis(StrEnum):
    ROUND_TRIP_EXACT = "ROUND_TRIP_EXACT"
    ONE_WAY_LOWER_BOUND = "ONE_WAY_LOWER_BOUND"


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
        if (self.measured_distance_m is None) != (self.distance_evidence_basis is None):
            raise ValueError(
                "distance and evidence basis must be both present or absent"
            )
        is_negative = False
        if self.measured_distance_m is not None:
            try:
                is_negative = self.measured_distance_m < 0
            except TypeError:
                pass
        if is_negative:
            raise ValueError("measured distance must be nonnegative")


@dataclass(frozen=True)
class AllowanceResult:
    status: AllowanceStatus
    amount_krw: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    classification: Classification
    allowance: AllowanceResult
    rule_set_id: str
    effective_from: str
    source_refs: tuple[str, ...]
