import importlib
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from app.policy.engine import PolicyEngine
from app.policy.rules import RuleRepository, RuleSet
from fastapi.testclient import TestClient

SEOUL = ZoneInfo("Asia/Seoul")
REGULATION_URL = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=287535"
ORDINANCE_URL = "https://www.law.go.kr/LSW/ordinInfoP.do?ordinSeq=2099835"
PERSONNEL_MINISTRY_URL = (
    "https://www.mpm.go.kr/mpm/info/resultPay/payBoard/"
    "?boardId=bbs_0000000000000035&cntId=693&mode=view"
)
PROFILE = "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"
PROFILE_LABEL = "서울특별시교육청 공무원 여비 기준"
DISCLOSURE_KEYS = {
    "profile",
    "profileLabel",
    "ruleSetId",
    "effectiveFrom",
    "localRoundTripExclusiveMeters",
    "actualExpenseInclusiveMeters",
    "fourHoursMinutes",
    "underFourHoursKrw",
    "fourHoursOrMoreKrw",
    "officialVehicleDeductionKrw",
    "sourceRefs",
}
STATIC_ROOT = Path("apps/travel-map/app/static")


# Production break caught: selecting a rule by the host/UTC date instead of the
# injectable current Asia/Seoul date, or allowing the public profile to drift.
def test_current_policy_disclosure_matches_effective_rule_and_fixed_profile(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.get("/api/v1/policy/current").status_code == 200
    policy_api = importlib.import_module("app.api.policy")
    monkeypatch.setattr(policy_api, "_today_in_seoul", lambda: date(2031, 4, 3))
    client.app.state.dependencies.policy = PolicyEngine(
        RuleRepository(
            (
                make_rule(
                    "older",
                    date(2031, 4, 2),
                    under_four_hours_krw=8_000,
                    official_vehicle_deduction_krw=8_000,
                ),
                make_rule(
                    "active",
                    date(2031, 4, 3),
                    local_round_trip_exclusive_meters=15_000,
                    actual_expense_inclusive_meters=2_500,
                    four_hours_minutes=300,
                    under_four_hours_krw=11_000,
                    four_hours_or_more_krw=22_000,
                    official_vehicle_deduction_krw=5_000,
                    source_refs=(REGULATION_URL, ORDINANCE_URL),
                ),
                make_rule("future", date(2031, 4, 4), under_four_hours_krw=12_000),
            )
        )
    )

    response = client.get("/api/v1/policy/current")

    assert response.status_code == 200
    assert response.json() == {
        "profile": PROFILE,
        "profileLabel": PROFILE_LABEL,
        "ruleSetId": "active",
        "effectiveFrom": "2031-04-03",
        "localRoundTripExclusiveMeters": 15_000,
        "actualExpenseInclusiveMeters": 2_500,
        "fourHoursMinutes": 300,
        "underFourHoursKrw": 11_000,
        "fourHoursOrMoreKrw": 22_000,
        "officialVehicleDeductionKrw": 5_000,
        "sourceRefs": [REGULATION_URL, ORDINANCE_URL],
    }


# Production break caught: allowing a browser or intermediary to retain today's
# rule response after the next Asia/Seoul effective-date boundary.
def test_current_policy_disclosure_prevents_caching(client: TestClient) -> None:
    response = client.get("/api/v1/policy/current")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


# Production break caught: treating a URL that merely contains an official host
# as official, or disclosing arbitrary HTTPS hosts from a corrupted rule artifact.
def test_policy_disclosure_exposes_only_validated_https_sources(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.get("/api/v1/policy/current").status_code == 200
    policy_api = importlib.import_module("app.api.policy")
    monkeypatch.setattr(policy_api, "_today_in_seoul", lambda: date(2026, 8, 17))
    client.app.state.dependencies.policy = PolicyEngine(
        RuleRepository(
            (
                make_rule(
                    "active",
                    date(2026, 8, 17),
                    source_refs=(
                        REGULATION_URL,
                        PERSONNEL_MINISTRY_URL,
                        "https://www.law.go.kr@attacker.invalid/forged",
                        "https://www.law.go.kr.attacker.invalid/forged",
                        "https://www.law.go.kr:444/forged",
                        "https://user:pass@www.law.go.kr/forged",
                    ),
                ),
            )
        )
    )

    response = client.get("/api/v1/policy/current")

    assert response.status_code == 200
    source_refs = response.json()["sourceRefs"]
    assert source_refs == [REGULATION_URL, PERSONNEL_MINISTRY_URL]
    for source_ref in source_refs:
        parsed = urlsplit(source_ref)
        assert parsed.scheme == "https"
        assert parsed.hostname in {"www.law.go.kr", "www.mpm.go.kr"}
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.port in {None, 443}


# Production break caught: returning a successful disclosure with no official
# links after every source is rejected by validation or the host allowlist.
def test_policy_disclosure_fails_closed_when_no_source_is_disclosable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_api = importlib.import_module("app.api.policy")
    monkeypatch.setattr(policy_api, "_today_in_seoul", lambda: date(2031, 4, 3))
    client.app.state.dependencies.policy = PolicyEngine(
        RuleRepository(
            (
                make_rule(
                    "no-official-source",
                    date(2031, 4, 3),
                    source_refs=(
                        "https://attacker.invalid/forged",
                        "https://user:pass@www.law.go.kr/forged",
                        "https://www.law.go.kr:444/forged",
                    ),
                ),
            )
        )
    )

    response = client.get("/api/v1/policy/current")

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "POLICY_SOURCES_UNAVAILABLE"}}
    assert response.headers["cache-control"] == "no-store"
    assert "attacker" not in response.text
    assert "user" not in response.text


# Production break caught: expanding the public response with an implementation
# path, index filename, or other RuleRepository detail.
def test_policy_disclosure_does_not_expose_rule_repository_paths(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/policy/current")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == DISCLOSURE_KEYS
    serialized = response.text.lower()
    assert "repository" not in serialized
    assert "resources/rules" not in serialized
    assert "index.json" not in serialized


# Production break caught: making the seam use UTC/local-host time rather than
# explicitly asking datetime for the Asia/Seoul calendar date.
def test_policy_today_seam_uses_asia_seoul_timezone(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.get("/api/v1/policy/current").status_code == 200
    policy_api = importlib.import_module("app.api.policy")
    observed_timezones: list[object] = []

    class FrozenDateTime:
        @classmethod
        def now(cls, timezone: object) -> datetime:
            observed_timezones.append(timezone)
            return datetime(2026, 8, 17, 0, 5, tzinfo=SEOUL)

    monkeypatch.setattr(policy_api, "datetime", FrozenDateTime)

    assert policy_api._today_in_seoul() == date(2026, 8, 17)
    assert observed_timezones == [SEOUL]


# Production break caught: weakening either fixed disclosure identity field from
# Literal to arbitrary text while leaving the endpoint's current values unchanged.
def test_policy_disclosure_schema_fixes_profile_and_label(client: TestClient) -> None:
    assert client.get("/api/v1/policy/current").status_code == 200

    schema = client.get("/openapi.json").json()["components"]["schemas"][
        "PolicyDisclosureResponse"
    ]["properties"]

    assert schema["profile"]["const"] == PROFILE
    assert schema["profileLabel"]["const"] == PROFILE_LABEL


# Production break caught: copying a current rule amount/threshold into shipped
# browser artifacts, where it can drift from the server disclosure.
def test_public_html_and_javascript_do_not_duplicate_policy_numbers(
    client: TestClient,
) -> None:
    active_rule = RuleRepository.from_directory(
        "apps/travel-map/resources/rules"
    ).for_date(date(2026, 8, 17))
    public_artifacts = {"/": client.get("/").text}
    javascript_paths = tuple(sorted(STATIC_ROOT.rglob("*.js")))
    assert javascript_paths
    for javascript_path in javascript_paths:
        relative_path = javascript_path.relative_to(STATIC_ROOT).as_posix()
        response = client.get(f"/static/{relative_path}")
        assert response.status_code == 200
        public_artifacts[f"/static/{relative_path}"] = response.text
    disclosed_numbers = {
        active_rule.local_round_trip_exclusive_meters,
        active_rule.actual_expense_inclusive_meters,
        active_rule.four_hours_minutes,
        active_rule.under_four_hours_krw,
        active_rule.four_hours_or_more_krw,
        active_rule.official_vehicle_deduction_krw,
    }

    assert set(public_artifacts) == {"/"} | {
        f"/static/{path.relative_to(STATIC_ROOT).as_posix()}"
        for path in javascript_paths
    }
    assert_no_policy_numbers(public_artifacts, disclosed_numbers)


# Test-harness mutation proof: both ordinary and JavaScript/Python-style grouped
# numeric literals are recognized without matching a longer unrelated number.
@pytest.mark.parametrize("literal", ["20000", "20_000"])
def test_policy_number_scanner_detects_grouped_and_plain_literals(literal: str) -> None:
    with pytest.raises(AssertionError, match="policy number 20000"):
        assert_no_policy_numbers(
            {"/static/nested/feature.js": f"const dailyLimit = {literal};"},
            {20_000},
        )

    assert_no_policy_numbers(
        {"/static/nested/feature.js": "const unrelatedLimit = 2_000_000;"},
        {20_000},
    )


def assert_no_policy_numbers(
    public_artifacts: dict[str, str],
    disclosed_numbers: set[int],
) -> None:
    for artifact_name, contents in public_artifacts.items():
        normalized_contents = re.sub(r"(?<=\d)_(?=\d)", "", contents)
        for value in disclosed_numbers:
            assert re.search(rf"(?<!\d){value}(?!\d)", normalized_contents) is None, (
                f"policy number {value} duplicated in {artifact_name}"
            )


def make_rule(
    rule_set_id: str,
    effective_from: date,
    *,
    local_round_trip_exclusive_meters: int = 12_000,
    actual_expense_inclusive_meters: int = 2_000,
    four_hours_minutes: int = 240,
    under_four_hours_krw: int = 10_000,
    four_hours_or_more_krw: int = 20_000,
    official_vehicle_deduction_krw: int = 10_000,
    source_refs: tuple[str, ...] = (REGULATION_URL,),
) -> RuleSet:
    return RuleSet(
        rule_set_id=rule_set_id,
        effective_from=effective_from,
        local_round_trip_exclusive_meters=local_round_trip_exclusive_meters,
        actual_expense_inclusive_meters=actual_expense_inclusive_meters,
        four_hours_minutes=four_hours_minutes,
        under_four_hours_krw=under_four_hours_krw,
        four_hours_or_more_krw=four_hours_or_more_krw,
        official_vehicle_deduction_krw=official_vehicle_deduction_krw,
        source_refs=source_refs,
    )
