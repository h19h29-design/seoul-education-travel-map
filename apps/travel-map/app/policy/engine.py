from zoneinfo import ZoneInfo

from app.policy.models import (
    AllowanceResult,
    AllowanceStatus,
    Classification,
    DistanceEvidenceBasis,
    PolicyInput,
    PolicyProfile,
    PolicyResult,
    VehicleUse,
)
from app.policy.rules import RuleRepository, RuleSet

SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")


class PolicyEngine:
    def __init__(self, rule_repository: RuleRepository) -> None:
        self._rule_repository = rule_repository

    def calculate(self, policy_input: PolicyInput) -> PolicyResult:
        self._validate_input(policy_input)
        korean_start_date = policy_input.starts_at.astimezone(SEOUL_TIMEZONE).date()
        rules = self._rule_repository.for_date(korean_start_date)
        profile_source_refs = self._profile_source_refs(policy_input, rules)

        if policy_input.measured_distance_m is None:
            return PolicyResult(
                classification=Classification.REVIEW_REQUIRED,
                allowance=AllowanceResult(
                    status=AllowanceStatus.REVIEW_REQUIRED,
                    amount_krw=None,
                    warnings=("DISTANCE_EVIDENCE_UNAVAILABLE",),
                ),
                rule_set_id=rules.rule_set_id,
                effective_from=rules.effective_from.isoformat(),
                source_refs=profile_source_refs,
            )

        classification = self._classify(policy_input, rules)
        duration_minutes = int(
            (policy_input.ends_at - policy_input.starts_at).total_seconds() // 60
        )
        calculated_status = (
            AllowanceStatus.REFERENCE_ESTIMATE
            if policy_input.policy_profile
            is PolicyProfile.INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER
            else AllowanceStatus.ESTIMATED
        )
        lower_bound_warning: tuple[str, ...] = ()
        if (
            policy_input.distance_evidence_basis
            is DistanceEvidenceBasis.ONE_WAY_LOWER_BOUND
        ):
            if (
                not policy_input.destination_in_seoul
                or policy_input.measured_distance_m
                <= rules.actual_expense_inclusive_meters
            ):
                return PolicyResult(
                    classification=classification,
                    allowance=AllowanceResult(
                        status=AllowanceStatus.REVIEW_REQUIRED,
                        amount_krw=None,
                        warnings=("TRIP_PATTERN_DISTANCE_RULE_UNVERIFIED",),
                    ),
                    rule_set_id=rules.rule_set_id,
                    effective_from=rules.effective_from.isoformat(),
                    source_refs=profile_source_refs,
                )
            lower_bound_warning = ("ONE_WAY_DISTANCE_LOWER_BOUND",)

        if classification is Classification.NON_LOCAL_EXPECTED:
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
                warnings=("NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE",),
            )
        elif classification is Classification.REVIEW_REQUIRED:
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
                warnings=("TRIP_PATTERN_DISTANCE_RULE_UNVERIFIED",),
            )
        elif policy_input.policy_profile is PolicyProfile.NONPUBLIC_OR_UNKNOWN:
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
            )
        elif policy_input.vehicle_use is VehicleUse.ASSIGNED_OFFICIAL:
            allowance = AllowanceResult(
                status=calculated_status,
                amount_krw=0,
                warnings=lower_bound_warning,
            )
        elif policy_input.measured_distance_m <= rules.actual_expense_inclusive_meters:
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
            )
        else:
            base = (
                rules.four_hours_or_more_krw
                if duration_minutes >= rules.four_hours_minutes
                else rules.under_four_hours_krw
            )
            if policy_input.vehicle_use is VehicleUse.OFFICIAL_OR_RENTED:
                base = max(0, base - rules.official_vehicle_deduction_krw)
            if (
                policy_input.has_other_local_trips_today
                and duration_minutes >= rules.four_hours_minutes
            ):
                remaining_daily_ceiling = max(
                    0,
                    rules.four_hours_or_more_krw - policy_input.previous_allowance_krw,
                )
                base = min(base, remaining_daily_ceiling)
            elif policy_input.has_other_local_trips_today:
                return PolicyResult(
                    classification=classification,
                    allowance=AllowanceResult(
                        status=AllowanceStatus.REVIEW_REQUIRED,
                        amount_krw=None,
                        warnings=("RULE_INTERPRETATION_UNVERIFIED",),
                    ),
                    rule_set_id=rules.rule_set_id,
                    effective_from=rules.effective_from.isoformat(),
                    source_refs=profile_source_refs,
                )
            allowance = AllowanceResult(
                status=calculated_status,
                amount_krw=base,
                warnings=lower_bound_warning,
            )

        return PolicyResult(
            classification=classification,
            allowance=allowance,
            rule_set_id=rules.rule_set_id,
            effective_from=rules.effective_from.isoformat(),
            source_refs=profile_source_refs,
        )

    @staticmethod
    def _validate_input(policy_input: PolicyInput) -> None:
        if policy_input.measured_distance_m is not None:
            if type(policy_input.measured_distance_m) is not int:
                raise ValueError("measured_distance_m must be an integer")
            if policy_input.measured_distance_m < 0:
                raise ValueError("measured_distance_m must be non-negative")
            if type(policy_input.distance_evidence_basis) is not DistanceEvidenceBasis:
                raise ValueError(
                    "distance_evidence_basis must be DistanceEvidenceBasis"
                )
        if type(policy_input.previous_allowance_krw) is not int:
            raise ValueError("previous_allowance_krw must be an integer")
        if policy_input.previous_allowance_krw < 0:
            raise ValueError("previous_allowance_krw must be non-negative")
        if (
            policy_input.starts_at.tzinfo is None
            or policy_input.starts_at.utcoffset() is None
            or policy_input.ends_at.tzinfo is None
            or policy_input.ends_at.utcoffset() is None
        ):
            raise ValueError("starts_at and ends_at must be timezone-aware")
        if policy_input.ends_at <= policy_input.starts_at:
            raise ValueError("ends_at must be after starts_at")

    @staticmethod
    def _profile_source_refs(
        policy_input: PolicyInput,
        rules: RuleSet,
    ) -> tuple[str, ...]:
        if (
            policy_input.policy_profile
            is not PolicyProfile.NATIONAL_PUBLIC_OFFICIAL_CONFIRMED
        ):
            return rules.source_refs
        return tuple(
            source_ref
            for source_ref in rules.source_refs
            if "ordinInfoP.do?ordinSeq=2099835" not in source_ref
        )

    @staticmethod
    def _classify(policy_input: PolicyInput, rules: RuleSet) -> Classification:
        if policy_input.destination_in_seoul:
            return Classification.LOCAL
        if policy_input.measured_distance_m is None:
            return Classification.REVIEW_REQUIRED
        if (
            policy_input.distance_evidence_basis
            is DistanceEvidenceBasis.ONE_WAY_LOWER_BOUND
        ):
            if (
                policy_input.measured_distance_m
                >= rules.local_round_trip_exclusive_meters
            ):
                return Classification.NON_LOCAL_EXPECTED
            return Classification.REVIEW_REQUIRED
        if policy_input.measured_distance_m < rules.local_round_trip_exclusive_meters:
            return Classification.LOCAL
        return Classification.NON_LOCAL_EXPECTED
