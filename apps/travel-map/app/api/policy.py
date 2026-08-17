from datetime import date, datetime
from typing import Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.common import dependencies_for
from app.contracts import PolicyDisclosureResponse

router = APIRouter(tags=["policy"])

_SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")
_PROFILE: Final = "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"
_PROFILE_LABEL: Final = "서울특별시교육청 공무원 여비 기준"
_DISCLOSABLE_SOURCE_HOSTS = frozenset({"www.law.go.kr", "www.mpm.go.kr"})


def _today_in_seoul() -> date:
    return datetime.now(_SEOUL_TIMEZONE).date()


@router.get("/policy/current", response_model=PolicyDisclosureResponse)
def current_policy(request: Request, response: Response) -> PolicyDisclosureResponse:
    rules = dependencies_for(request).policy.rule_for_date(_today_in_seoul())
    source_refs = _disclosable_source_refs(rules.source_refs)
    if not source_refs:
        raise HTTPException(
            status_code=503,
            detail="POLICY_SOURCES_UNAVAILABLE",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return PolicyDisclosureResponse(
        profile=_PROFILE,
        profile_label=_PROFILE_LABEL,
        rule_set_id=rules.rule_set_id,
        effective_from=rules.effective_from.isoformat(),
        local_round_trip_exclusive_meters=rules.local_round_trip_exclusive_meters,
        actual_expense_inclusive_meters=rules.actual_expense_inclusive_meters,
        four_hours_minutes=rules.four_hours_minutes,
        under_four_hours_krw=rules.under_four_hours_krw,
        four_hours_or_more_krw=rules.four_hours_or_more_krw,
        official_vehicle_deduction_krw=rules.official_vehicle_deduction_krw,
        source_refs=source_refs,
    )


def _disclosable_source_refs(source_refs: tuple[str, ...]) -> tuple[str, ...]:
    validated: list[str] = []
    for source_ref in source_refs:
        try:
            parsed = urlsplit(source_ref)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and parsed.hostname in _DISCLOSABLE_SOURCE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        ):
            validated.append(source_ref)
    return tuple(validated)
