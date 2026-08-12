# 서울교육기관 관내출장 지도 공개 MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 서울 주소를 둔 교육청 기관·국립·공립·사립 학교·유치원을 출발지로 검색하고, 출장지까지 대중교통·자동차·도보 복수 경로와 최단시간·최단거리·최저비용, 관내출장 예상 지급액을 로그인 없이 제공하는 공개 웹 MVP를 만든다.

**Architecture:** `apps/travel-map/`을 기존 런처와 RAG 코드에서 분리된 FastAPI 배포 단위로 만들고, 브라우저는 표준 ES module과 Kakao Map Web SDK만 사용한다. 기관 원천은 배치 동기화 후 승인된 불변 snapshot으로 서비스하며, 모든 경로 제공자는 하나의 비동기 계약으로 정규화한다. 정책 엔진은 서울 경계·왕복 판정거리·출장 입력시간·적용대상 신분을 서버에서 계산하고 이동비와 여비를 분리해 반환한다.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic 2, pydantic-settings, HTTPX, Shapely, pyproj, orjson, pytest, pytest-asyncio, respx, Ruff, mypy, vanilla HTML/CSS/ES modules, Kakao Map Web SDK, Playwright, pnpm, Docker.

## Global Constraints

- 기준 설계는 `docs/superpowers/specs/2026-08-10-seoul-education-local-travel-map-design.md`다. 계약이나 법적 경계를 바꾸려면 구현보다 먼저 설계 변경을 승인받는다.
- 현재 작업 폴더의 미커밋 `교육행정_AI_Launcher.html`, `README.md`, `index.html`, `run.command`, `tests/`, `.superpowers/`는 사용자 작업이다. 실행 시 `superpowers:using-git-worktrees`로 `codex/travel-map-mvp` worktree를 만들고 이 파일들을 수정하거나 stage하지 않는다.
- 앱 코드는 전부 `apps/travel-map/` 아래에 둔다. 기존 루트 HTML, RAG `src/`, `data/`, `tests/`, 루트 `pyproject.toml`을 import하거나 수정하지 않는다.
- 출발지는 공식 주소와 검증 좌표가 모두 서울 경계 안인 승인된 `siteId`만 허용한다. 클라이언트가 보낸 임의 출발 좌표는 받거나 신뢰하지 않는다.
- 서울 안 목적지는 거리에 관계없이 `LOCAL`이다. 서울 밖은 지정된 일반 최단 네트워크 왕복거리 `< 12,000m`만 `LOCAL`이고 `>= 12,000m`는 `NON_LOCAL_EXPECTED`다. 직선거리나 서울 경계 버퍼 길이를 법적 판정거리로 쓰지 않는다.
- 서울 안도 왕복 `<= 2,000m` 실비 분기를 위해 기관→출장지와 출장지→기관의 일반 최단 네트워크 거리를 계산한다. 이 거리는 서울 안이라는 `LOCAL` 판정을 뒤집지 않고 지급액 분기에만 사용한다.
- `서울 경계 + 외곽 12km`는 서비스 지원영역 필터일 뿐이다. 그 밖의 목적지는 `OUT_OF_COVERAGE`와 관외 안내를 반환하고 상세 경로·여비 자동계산을 중단한다.
- 4시간 기준은 경로 ETA가 아니라 `returnsAt - startsAt`이다. 239분은 10,000원, 240분은 20,000원이며 왕복 `<= 2,000m`는 실비 검토 분기다.
- `policyProfile`은 기본값 없이 명시적으로 선택한다. `SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED`와 `NATIONAL_PUBLIC_OFFICIAL_CONFIRMED`만 공식 예상액을 계산하며, 비공무원·미확인은 경로만 제공하고 여비를 `REVIEW_REQUIRED`로 둔다.
- `mobilityCost`와 `allowance`는 응답·UI·테스트에서 별도 객체와 제목으로 유지한다. 자동차 연료비·통행료·주차비 추정치를 법정 지급액으로 표현하지 않는다.
- 기관 API는 런타임 사용자 요청에서 호출하지 않는다. 승인된 snapshot만 검색하고, 기관 원천의 대표자·원장명·전화번호 등 경로 계산에 불필요한 필드는 수집·저장·응답하지 않는다.
- Kakao JavaScript 키만 등록 도메인 제한을 전제로 bootstrap 응답에 포함할 수 있다. Kakao REST 키, Kakao Mobility 키, NEIS 키, 유치원알리미 키, 서울 Open API 키, 오피넷 키는 서버 환경변수에만 둔다.
- 실제 목적지 검색어·정밀 좌표·신분 선택·출장시각은 애플리케이션 로그에 남기지 않는다. 로그인, 사용자 식별 쿠키, 계산 이력, 승인 이력, 개인 DB를 만들지 않는다.
- 모든 기본 테스트는 네트워크 없이 합성 fixture와 mock HTTP로 통과해야 한다. 실제 키를 사용하는 검증은 `TRAVEL_MAP_LIVE_SMOKE=1` 명시 시에만 실행한다.
- 단계 A의 기본 경로 체인은 대중교통 `(SeoulTransitProvider, KakaoTransitProvider)`, 자동차 `(KakaoCarProvider,)`, 도보 `(KakaoWalkProvider,)`다. 단계 B·C는 같은 provider 계약과 등록 지점만 확장한다.
- 각 작업은 아래에 명시한 파일만 `git add`한다. `git add -A`를 쓰지 않고 테스트 실패 상태에서는 커밋하지 않는다.

---

## Repository Target

```text
apps/travel-map/
├── .env.example
├── .python-version
├── Dockerfile
├── README.md
├── package.json
├── pnpm-lock.yaml
├── pyproject.toml
├── uv.lock
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── settings.py
│   ├── contracts.py
│   ├── cache.py
│   ├── rate_limit.py
│   ├── api/{__init__,bootstrap,institutions,geodata,places,trips}.py
│   ├── institutions/{__init__,models,store,snapshot,sync}.py
│   ├── institutions/sources/{__init__,common,sen,neis,kindergarten}.py
│   ├── policy/{__init__,coverage,models,rules,engine}.py
│   ├── providers/{__init__,kakao_local,kakao_map,kakao_mobility,seoul_transit}.py
│   ├── routing/{__init__,models,provider,ranking,orchestrator,bootstrap}.py
│   ├── services/{__init__,trip_preview}.py
│   └── static/{index.html,styles.css,api.js,kakao-map.js,app.js}
├── resources/
│   ├── geodata/{seoul.geojson,seoul-plus-12km.geojson,manifest.json}
│   ├── institution-sources/sen-institutions.csv
│   ├── institution-snapshots/{current.json,20260810T000000Z-initial/institutions.jsonl,20260810T000000Z-initial/sites.jsonl,20260810T000000Z-initial/manifest.json}
│   └── rules/{index.json,local-travel-2026-07-01.json}
├── scripts/{build-geodata.py,sync-institutions.py,probe-route-providers.py,smoke-live.py}
├── tests/
│   ├── api/{test_bootstrap,test_institutions,test_geodata,test_places,test_trips}.py
│   ├── institutions/{test_store,test_snapshot,test_sync}.py
│   ├── policy/{test_coverage,test_engine,test_rules}.py
│   ├── providers/{test_kakao_local,test_kakao_map,test_kakao_mobility,test_seoul_transit}.py
│   ├── routing/{test_ranking,test_orchestrator,test_bootstrap}.py
│   ├── security/{test_key_exposure,test_rate_limit}.py
│   └── fixtures/{geodata,institutions,providers}/
└── e2e/route-preview.spec.ts
```

## Task 1: 격리된 앱 scaffold와 기본 품질 게이트

**Files:**

- Create: `apps/travel-map/.python-version`
- Create: `apps/travel-map/pyproject.toml`
- Create: `apps/travel-map/uv.lock`
- Create: `apps/travel-map/package.json`
- Create: `apps/travel-map/pnpm-lock.yaml`
- Create: `apps/travel-map/Dockerfile`
- Create: `apps/travel-map/.env.example`
- Create: `apps/travel-map/app/__init__.py`
- Create: `apps/travel-map/app/settings.py`
- Create: `apps/travel-map/app/main.py`
- Test: `apps/travel-map/tests/test_health.py`

**Interfaces:**

- Produces: `create_app() -> FastAPI` and `GET /healthz -> {"status":"ok"}` for all later API tasks.
- Produces: one locked Python environment and one locked Playwright environment scoped to `apps/travel-map/`.

- [ ] **Step 1: 전용 worktree를 만들고 사용자 작업이 섞이지 않았는지 확인한다.**

```bash
git check-ignore -q .worktrees
git worktree add .worktrees/travel-map-mvp -b codex/travel-map-mvp main
cd .worktrees/travel-map-mvp
git status --short
```

Expected: 마지막 명령의 출력이 없다. `.worktrees`가 ignore되지 않았다면 worktree를 만들기 전에 루트 `.gitignore`에 `.worktrees/`만 추가해 별도 커밋한다.

- [ ] **Step 2: 앱 전용 의존성과 lockfile을 만든다.**

```bash
uv init --bare --python 3.12 apps/travel-map
uv add --project apps/travel-map fastapi 'uvicorn[standard]' httpx pydantic-settings shapely pyproj orjson
uv add --project apps/travel-map --dev pytest pytest-asyncio respx ruff mypy
```

`pyproject.toml`에 앱 package 설치와 pytest root를 명시한다.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.pytest.ini_options]
pythonpath = ["."]
asyncio_mode = "auto"
```

`apps/travel-map/package.json`은 다음 script만 갖게 만든 뒤 `pnpm --dir apps/travel-map add -D @playwright/test`를 실행한다.

```json
{
  "name": "seoul-education-travel-map",
  "private": true,
  "scripts": {
    "test:e2e": "playwright test",
    "test:e2e:ui": "playwright test --ui"
  },
  "devDependencies": {}
}
```

Expected: `uv.lock`과 `pnpm-lock.yaml`이 생성되고 모든 exact artifact version이 고정된다.

- [ ] **Step 3: health endpoint의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/test_health.py
from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 4: health 테스트가 실패하는지 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/test_health.py -q
```

Expected: `ModuleNotFoundError: No module named 'app'` 또는 `create_app` 부재로 실패한다.

- [ ] **Step 5: 최소 FastAPI app factory를 구현한다.**

```python
# apps/travel-map/app/main.py
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="서울교육기관 관내출장 지도", version="0.1.0")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

`Dockerfile`은 `python:3.12-slim-bookworm`을 base로 사용하고, `uv sync --frozen --no-dev`, 비root 사용자, `uvicorn app.main:app --host 0.0.0.0 --port 8080`만 포함한다. `.env.example`에는 값 없이 `KAKAO_JAVASCRIPT_KEY`, `KAKAO_REST_API_KEY`, `KAKAO_MOBILITY_REST_API_KEY`, `SEOUL_OPEN_API_KEY`, `NEIS_API_KEY`, `KINDERGARTEN_API_KEY`, `OPINET_API_KEY`, `ALLOWED_HOSTS`, `ALLOWED_ORIGINS`를 나열한다.

`app/settings.py`에는 이후 단계가 확장할 Pydantic settings 뼈대를 먼저 둔다. 이 파일 경로와 `Settings` 이름은 단계 B·C까지 고정한다.

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    environment: str = "development"
```

- [ ] **Step 6: scaffold 품질 게이트를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/test_health.py -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests
uv run --project apps/travel-map mypy apps/travel-map/app
```

Expected: 테스트 1개 PASS, Ruff와 mypy 오류 0개다.

- [ ] **Step 7: 앱 scaffold만 커밋한다.**

```bash
git add apps/travel-map/.python-version apps/travel-map/pyproject.toml apps/travel-map/uv.lock apps/travel-map/package.json apps/travel-map/pnpm-lock.yaml apps/travel-map/Dockerfile apps/travel-map/.env.example apps/travel-map/app/__init__.py apps/travel-map/app/settings.py apps/travel-map/app/main.py apps/travel-map/tests/test_health.py
git commit -m "build: scaffold travel map app"
```

## Task 2: 서울 경계·지원영역과 버전형 여비 정책 엔진

**Files:**

- Create: `apps/travel-map/app/policy/__init__.py`
- Create: `apps/travel-map/app/routing/__init__.py`
- Create: `apps/travel-map/app/routing/models.py` (Task 2에서는 공통 `Coordinate`만 정의)
- Create: `apps/travel-map/app/policy/models.py`
- Create: `apps/travel-map/app/policy/coverage.py`
- Create: `apps/travel-map/app/policy/rules.py`
- Create: `apps/travel-map/app/policy/engine.py`
- Create: `apps/travel-map/resources/rules/index.json`
- Create: `apps/travel-map/resources/rules/local-travel-2026-07-01.json`
- Create: `apps/travel-map/scripts/build-geodata.py`
- Create: `apps/travel-map/resources/geodata/seoul.geojson`
- Create: `apps/travel-map/resources/geodata/seoul-plus-12km.geojson`
- Create: `apps/travel-map/resources/geodata/manifest.json`
- Test: `apps/travel-map/tests/policy/test_coverage.py`
- Test: `apps/travel-map/tests/policy/test_rules.py`
- Test: `apps/travel-map/tests/policy/test_engine.py`
- Fixture: `apps/travel-map/tests/fixtures/geodata/seoul-square.geojson`

**Interfaces:**

- Produces: `CoverageService.classify(point: Coordinate) -> CoverageState` where state is `SEOUL`, `BUFFER`, or `OUTSIDE`.
- Produces: `RuleRepository.for_date(on_date: date) -> RuleSet`.
- Produces: `PolicyEngine.calculate(policy_input: PolicyInput) -> PolicyResult` with separate classification and allowance status.
- Produces: 이후 모든 지도·정책·provider가 공유하는 `app.routing.models.Coordinate`.

- [ ] **Step 1: 경계 상태의 RED 테스트를 합성 polygon으로 작성한다.**

```python
# apps/travel-map/tests/policy/test_coverage.py
from pathlib import Path
import shutil

from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.routing.models import Coordinate


def test_coverage_separates_seoul_buffer_and_outside() -> None:
    service = CoverageService.from_geojson(
        seoul_path=Path("apps/travel-map/tests/fixtures/geodata/seoul-square.geojson"),
        buffer_distance_m=12_000,
    )
    assert service.classify(Coordinate(37.55, 126.98)) is CoverageState.SEOUL
    assert service.classify(Coordinate(37.55, 127.09)) is CoverageState.BUFFER
    assert service.classify(Coordinate(37.55, 127.30)) is CoverageState.OUTSIDE
```

- [ ] **Step 2: 규칙 경계의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/policy/test_engine.py
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.policy.engine import PolicyEngine
from app.policy.models import PolicyInput, PolicyProfile, VehicleUse
from app.policy.rules import RuleRepository

SEOUL = ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    ("round_trip_m", "expected_classification"),
    [(11_999, "LOCAL"), (12_000, "NON_LOCAL_EXPECTED")],
)
def test_outside_seoul_uses_strict_twelve_km_boundary(
    round_trip_m: int, expected_classification: str
) -> None:
    engine = PolicyEngine(RuleRepository.from_directory("apps/travel-map/resources/rules"))
    result = engine.calculate(
        PolicyInput(
            destination_in_seoul=False,
            round_trip_distance_m=round_trip_m,
            starts_at=datetime(2026, 8, 10, 9, 0, tzinfo=SEOUL),
            returns_at=datetime(2026, 8, 10, 12, 59, tzinfo=SEOUL),
            policy_profile=PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED,
            vehicle_use=VehicleUse.NONE,
            has_other_local_trips_today=False,
            previous_allowance_krw=0,
        )
    )
    assert result.classification.value == expected_classification


@pytest.mark.parametrize(("minutes", "amount"), [(239, 10_000), (240, 20_000)])
def test_duration_boundary_uses_entered_trip_time(minutes: int, amount: int) -> None:
    result = make_policy_engine().calculate(make_policy_input(minutes=minutes))
    assert result.allowance.amount_krw == amount


def test_unknown_employment_profile_withholds_allowance() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(policy_profile=PolicyProfile.NONPUBLIC_OR_UNKNOWN)
    )
    assert result.allowance.status.value == "REVIEW_REQUIRED"
    assert result.allowance.amount_krw is None


def test_non_local_result_does_not_apply_local_flat_allowance() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(destination_in_seoul=False, round_trip_distance_m=12_000)
    )
    assert result.classification.value == "NON_LOCAL_EXPECTED"
    assert result.allowance.status.value == "REVIEW_REQUIRED"
    assert result.allowance.amount_krw is None
    assert result.allowance.warnings == ("NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE",)


def make_policy_engine() -> PolicyEngine:
    return PolicyEngine(RuleRepository.from_directory("apps/travel-map/resources/rules"))


def make_policy_input(
    *,
    minutes: int = 239,
    policy_profile: PolicyProfile = PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED,
    destination_in_seoul: bool = True,
    round_trip_distance_m: int = 3_000,
) -> PolicyInput:
    starts_at = datetime(2026, 8, 10, 9, 0, tzinfo=SEOUL)
    return PolicyInput(
        destination_in_seoul=destination_in_seoul,
        round_trip_distance_m=round_trip_distance_m,
        starts_at=starts_at,
        returns_at=starts_at + timedelta(minutes=minutes),
        policy_profile=policy_profile,
        vehicle_use=VehicleUse.NONE,
        has_other_local_trips_today=False,
        previous_allowance_krw=0,
    )
```

위 테스트 import에 `from datetime import timedelta`를 추가한다.

- [ ] **Step 3: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/policy -q
```

Expected: `app.policy` 모듈과 모델이 없어 collection 단계에서 실패한다.

- [ ] **Step 4: 정책 모델과 시행일별 규칙 로더를 구현한다.**

```python
# apps/travel-map/app/policy/models.py
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
    INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER = "INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER"
    NONPUBLIC_OR_UNKNOWN = "NONPUBLIC_OR_UNKNOWN"


class VehicleUse(StrEnum):
    NONE = "NONE"
    PRIVATE = "PRIVATE"
    OFFICIAL_OR_RENTED = "OFFICIAL_OR_RENTED"
    ASSIGNED_OFFICIAL = "ASSIGNED_OFFICIAL"


@dataclass(frozen=True)
class PolicyInput:
    destination_in_seoul: bool
    round_trip_distance_m: int
    starts_at: datetime
    returns_at: datetime
    policy_profile: PolicyProfile
    vehicle_use: VehicleUse
    has_other_local_trips_today: bool
    previous_allowance_krw: int
```

공통 좌표는 별도 파일에 다음처럼 둔다.

```python
# apps/travel-map/app/routing/models.py
from dataclasses import dataclass


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float
```

같은 `policy.models.py`에 `Classification`, `AllowanceStatus`, `AllowanceResult`, `PolicyResult`를 정의한다.

```python
class Classification(StrEnum):
    LOCAL = "LOCAL"
    NON_LOCAL_EXPECTED = "NON_LOCAL_EXPECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class AllowanceStatus(StrEnum):
    ESTIMATED = "ESTIMATED"
    REFERENCE_ESTIMATE = "REFERENCE_ESTIMATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


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
```

`local-travel-2026-07-01.json`에는 `effectiveFrom`, `localRoundTripExclusiveMeters: 12000`, `actualExpenseInclusiveMeters: 2000`, `fourHoursMinutes: 240`, `underFourHoursKrw: 10000`, `fourHoursOrMoreKrw: 20000`, `officialVehicleDeductionKrw: 10000`, 법령 URL 3개를 기록한다. `RuleRepository`는 `index.json`의 시행일을 정렬해 출장 시작일에 유효한 파일 하나만 선택하고 빈 구간에서는 예외를 낸다.

```python
# apps/travel-map/app/policy/rules.py
@dataclass(frozen=True)
class RuleSet:
    rule_set_id: str
    effective_from: date
    local_round_trip_exclusive_meters: int
    actual_expense_inclusive_meters: int
    four_hours_minutes: int
    under_four_hours_krw: int
    four_hours_or_more_krw: int
    official_vehicle_deduction_krw: int
    source_refs: tuple[str, ...]


class RuleRepository:
    def __init__(self, rules: tuple[RuleSet, ...]) -> None:
        self._rules = tuple(sorted(rules, key=lambda item: item.effective_from))

    @classmethod
    def from_directory(cls, directory: str | Path) -> "RuleRepository":
        root = Path(directory)
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        rules: list[RuleSet] = []
        for entry in index["rules"]:
            payload = json.loads((root / entry["file"]).read_text(encoding="utf-8"))
            rules.append(
                RuleSet(
                    rule_set_id=str(payload["ruleSetId"]),
                    effective_from=date.fromisoformat(payload["effectiveFrom"]),
                    local_round_trip_exclusive_meters=int(payload["localRoundTripExclusiveMeters"]),
                    actual_expense_inclusive_meters=int(payload["actualExpenseInclusiveMeters"]),
                    four_hours_minutes=int(payload["fourHoursMinutes"]),
                    under_four_hours_krw=int(payload["underFourHoursKrw"]),
                    four_hours_or_more_krw=int(payload["fourHoursOrMoreKrw"]),
                    official_vehicle_deduction_krw=int(payload["officialVehicleDeductionKrw"]),
                    source_refs=tuple(str(url) for url in payload["sourceRefs"]),
                )
            )
        return cls(tuple(rules))

    def for_date(self, on_date: date) -> RuleSet:
        eligible = [item for item in self._rules if item.effective_from <= on_date]
        if not eligible:
            raise LookupError(f"no rule set for {on_date.isoformat()}")
        return eligible[-1]
```

파일 상단에 `json`, `dataclass`, `date`, `Path` import를 둔다. 생성 시 금액 음수·겹친 시행일·근거 URL 없음이면 `ValueError`를 내는 검증을 추가하고 각각 테스트한다.

- [ ] **Step 5: 경계와 여비 계산을 최소 구현한다.**

`CoverageService`는 WGS84 입력을 `EPSG:5179`로 투영한 뒤 미터 단위 buffer를 만들고 `covers()`로 경계점까지 포함한다. `PolicyEngine`은 다음 순서를 코드 그대로 보존한다.

```python
calculated_status = (
    AllowanceStatus.REFERENCE_ESTIMATE
    if input.policy_profile is PolicyProfile.INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER
    else AllowanceStatus.ESTIMATED
)
if classification is Classification.NON_LOCAL_EXPECTED:
    allowance = AllowanceResult(
        status=AllowanceStatus.REVIEW_REQUIRED,
        amount_krw=None,
        warnings=("NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE",),
    )
elif input.policy_profile is PolicyProfile.NONPUBLIC_OR_UNKNOWN:
    allowance = AllowanceResult(status=AllowanceStatus.REVIEW_REQUIRED, amount_krw=None)
elif input.vehicle_use is VehicleUse.ASSIGNED_OFFICIAL:
    allowance = AllowanceResult(status=calculated_status, amount_krw=0)
elif input.round_trip_distance_m <= rules.actual_expense_inclusive_meters:
    allowance = AllowanceResult(status=AllowanceStatus.REVIEW_REQUIRED, amount_krw=None)
else:
    base = (
        rules.four_hours_or_more_krw
        if duration_minutes >= rules.four_hours_minutes
        else rules.under_four_hours_krw
    )
    if input.vehicle_use is VehicleUse.OFFICIAL_OR_RENTED:
        base = max(0, base - rules.official_vehicle_deduction_krw)
    if input.has_other_local_trips_today and duration_minutes >= rules.four_hours_minutes:
        base = max(0, rules.four_hours_or_more_krw - input.previous_allowance_krw)
    elif input.has_other_local_trips_today:
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
    allowance = AllowanceResult(status=calculated_status, amount_krw=base)
```

분류는 `destination_in_seoul`이면 항상 `LOCAL`, 아니면 `round_trip_distance_m < 12_000`만 `LOCAL`로 한다. `classification`은 allowance 분기 전에 계산하고, `NON_LOCAL_EXPECTED`에는 관내 정액 산식을 적용하지 않는다. `NATIONAL_PUBLIC_OFFICIAL_CONFIRMED`의 `profile_source_refs`에서는 서울교육감 조례를 제외하고 공무원 여비 규정을 직접 근거로 표시한다. `returns_at <= starts_at` 또는 timezone 없는 datetime은 validation error다. 테스트에는 공무용차량 239분 0원·240분 10,000원, 전용차량 0원, 2,000m 검토, 12,000m 관외 정액 미적용, 4시간 이상 동일일 잔여 상한, 4시간 미만 동일일 판정 보류를 추가한다.

- [ ] **Step 6: 공식 경계 원본을 정규화하고 manifest를 생성한다.**

[국토교통부 행정구역도 WFS](https://www.data.go.kr/data/15059008/openapi.do)의 광역시도 레이어에서 서울특별시 feature만 `resources/geodata/source/seoul-boundary.geojson`으로 내려받는다. `scripts/build-geodata.py`는 이 파일을 WGS84로 정규화하고 유효성 보정 후, EPSG:5179에서 12,000m buffer를 만들어 다시 WGS84 GeoJSON으로 내보낸다. manifest는 원천 페이지 URL, 수집시각, CRS, 원본·산출물 SHA-256, feature 수를 코드로 계산한다.

```bash
uv run --project apps/travel-map python apps/travel-map/scripts/build-geodata.py \
  --source apps/travel-map/resources/geodata/source/seoul-boundary.geojson \
  --output apps/travel-map/resources/geodata
```

Expected: `seoul.geojson`, `seoul-plus-12km.geojson`, `manifest.json`이 생성되고 서울시청 좌표는 서울, 인천시청 좌표는 지원영역 밖으로 검증된다.

- [ ] **Step 7: 정책 전체 테스트와 정적 검사를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/policy -q
uv run --project apps/travel-map ruff check apps/travel-map/app/policy apps/travel-map/tests/policy apps/travel-map/scripts/build-geodata.py
uv run --project apps/travel-map mypy apps/travel-map/app/policy apps/travel-map/scripts/build-geodata.py
```

Expected: 11,999/12,000m, 2,000m, 239/240분, 차량 4종, 신분 4종, 날짜 버전 테스트가 모두 PASS한다.

- [ ] **Step 8: 정책과 경계 리소스를 커밋한다.**

```bash
git add apps/travel-map/app/policy apps/travel-map/app/routing/__init__.py apps/travel-map/app/routing/models.py apps/travel-map/resources/rules apps/travel-map/resources/geodata apps/travel-map/scripts/build-geodata.py apps/travel-map/tests/policy apps/travel-map/tests/fixtures/geodata
git commit -m "feat: add versioned local travel policy"
```

## Task 3: 기관·물리 출발지 snapshot 검색

**Files:**

- Create: `apps/travel-map/app/institutions/__init__.py`
- Create: `apps/travel-map/app/institutions/models.py`
- Create: `apps/travel-map/app/institutions/store.py`
- Create: `apps/travel-map/app/institutions/snapshot.py`
- Test: `apps/travel-map/tests/institutions/test_store.py`
- Test: `apps/travel-map/tests/institutions/test_snapshot.py`
- Fixture: `apps/travel-map/tests/fixtures/institutions/snapshot/current.json`
- Fixture: `apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/institutions.jsonl`
- Fixture: `apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/sites.jsonl`
- Fixture: `apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/manifest.json`

**Interfaces:**

- Produces: `InstitutionStore.load(snapshot_root: Path) -> InstitutionStore`.
- Produces: `InstitutionStore.search(query, institution_type, foundation_type, education_office, district, limit) -> tuple[InstitutionSearchItem, ...]`.
- Produces: `InstitutionStore.require_site(site_id: str) -> InstitutionSite`; later trip requests resolve their origin only through this method.

- [ ] **Step 1: 병설기관·폐교·동명기관 fixture와 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/institutions/test_store.py
from pathlib import Path

import pytest

from app.institutions.store import InstitutionStore, UnknownSiteError


def test_search_keeps_co_located_school_and_kindergarten_separate() -> None:
    store = InstitutionStore.load(Path("apps/travel-map/tests/fixtures/institutions/snapshot"))
    results = store.search(query="샘물", limit=20)
    assert [(item.institution_type, item.official_name) for item in results] == [
        ("KINDERGARTEN", "샘물초등학교병설유치원"),
        ("ELEMENTARY_SCHOOL", "샘물초등학교"),
    ]


def test_closed_site_is_hidden_and_cannot_be_route_origin() -> None:
    store = InstitutionStore.load(Path("apps/travel-map/tests/fixtures/institutions/snapshot"))
    assert all(item.official_name != "폐교학교" for item in store.search(query="폐교", limit=20))
    with pytest.raises(UnknownSiteError):
        store.require_site("neis:B10:CLOSED:main")
```

fixture에는 같은 주소·좌표의 초등학교와 병설유치원, 같은 이름의 서로 다른 자치구 학교, `CLOSED`, `MISSING_FROM_SOURCE`, 본관·분관 site를 실제 JSONL 행으로 넣는다.

- [ ] **Step 2: snapshot 무결성 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/institutions/test_snapshot.py
from pathlib import Path

import pytest

from app.institutions.snapshot import SnapshotIntegrityError, verify_snapshot


def test_snapshot_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    (fixture / "fixture-001" / "sites.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError, match="sites.jsonl sha256"):
        verify_snapshot(fixture)


def copy_fixture_snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "snapshot"
    shutil.copytree(Path("apps/travel-map/tests/fixtures/institutions/snapshot"), destination)
    return destination
```

- [ ] **Step 3: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_store.py apps/travel-map/tests/institutions/test_snapshot.py -q
```

Expected: `app.institutions` 모듈 부재로 실패한다.

- [ ] **Step 4: 기관과 site 모델을 구현한다.**

```python
# apps/travel-map/app/institutions/models.py
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class InstitutionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TEMPORARILY_CLOSED = "TEMPORARILY_CLOSED"
    CLOSED = "CLOSED"
    MISSING_FROM_SOURCE = "MISSING_FROM_SOURCE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class Institution(BaseModel):
    model_config = ConfigDict(frozen=True)
    institution_id: str
    official_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    status: InstitutionStatus
    status_source: str
    effective_from: str
    effective_to: str | None
    last_seen_snapshot: str
    aliases: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    merged_into: str | None = None
    source: str
    source_region_code: str
    source_as_of: str


class InstitutionSite(BaseModel):
    model_config = ConfigDict(frozen=True)
    site_id: str
    institution_id: str
    site_name: str
    road_address: str
    district: str
    latitude: float
    longitude: float
    coordinate_quality: str
    routing_anchor_latitude: float
    routing_anchor_longitude: float
    is_default: bool
    status: InstitutionStatus
    effective_from: str
    effective_to: str | None


class InstitutionSearchItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    institution_id: str
    site_id: str
    official_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    road_address: str
    district: str
    coordinate_quality: str
    snapshot_id: str
    snapshot_as_of: str


class SourceSnapshotInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str
    raw_sha256: str
    page_count: int
    row_count: int


class SnapshotDiff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    previous_snapshot_id: str | None
    added_count: int
    changed_count: int
    missing_count: int
    closed_candidate_count: int


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int
    snapshot_id: str
    created_at: str
    snapshot_as_of: str
    approved: bool
    approved_at: str | None
    approved_by_role: str | None
    sources: tuple[SourceSnapshotInfo, ...]
    institutions_sha256: str
    sites_sha256: str
    institution_count: int
    site_count: int
    quarantined_count: int
    possible_match_count: int
    counts_by_type: dict[str, int]
    counts_by_foundation: dict[str, int]
    counts_by_status: dict[str, int]
    coordinate_quality_counts: dict[str, int]
    diff: SnapshotDiff
```

- [ ] **Step 5: snapshot 검증과 검색 store를 구현한다.**

`verify_snapshot()`은 `current.json`의 snapshot ID를 디렉터리 밖으로 탈출할 수 없는 slug로 검증하고 manifest schema 1, `approved=true`, 비어 있지 않은 `approvedAt`·`approvedByRole`·`sources`, manifest에 기록된 `institutions.jsonl`·`sites.jsonl` SHA-256과 행 수를 다시 계산한다. source별 행 합계·유형/설립/상태 합계와 전체 건수도 일치해야 한다. `InstitutionStore.load()`는 검증 성공 후에만 ACTIVE 기관과 site를 메모리에 올린다. 검색 정규화는 Unicode NFC, 공백·괄호 제거, 한글 초성 인덱스를 사용하며 자동 fuzzy 병합은 하지 않는다.

```python
def require_site(self, site_id: str) -> InstitutionSite:
    site = self._active_sites.get(site_id)
    if site is None:
        raise UnknownSiteError(site_id)
    return site
```

정렬은 정확일치, 접두일치, 부분일치, 기관명, `siteId` 순으로 고정하고 최대 limit는 50이다.

- [ ] **Step 6: 기관 store 테스트와 품질 검사를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_store.py apps/travel-map/tests/institutions/test_snapshot.py -q
uv run --project apps/travel-map ruff check apps/travel-map/app/institutions apps/travel-map/tests/institutions
uv run --project apps/travel-map mypy apps/travel-map/app/institutions
```

Expected: 병설기관 비병합, 동명 분리, 폐교 숨김, hash 변조 차단, 필터와 초성 검색 테스트가 PASS한다.

- [ ] **Step 7: 기관 snapshot core만 커밋한다.**

```bash
git add apps/travel-map/app/institutions apps/travel-map/tests/institutions apps/travel-map/tests/fixtures/institutions
git commit -m "feat: add verified institution snapshot store"
```

## Task 4: 교육청·NEIS·유치원알리미 동기화와 원자적 승격

**Files:**

- Create: `apps/travel-map/app/institutions/sources/__init__.py`
- Create: `apps/travel-map/app/institutions/sources/common.py`
- Create: `apps/travel-map/app/institutions/sources/sen.py`
- Create: `apps/travel-map/app/institutions/sources/neis.py`
- Create: `apps/travel-map/app/institutions/sources/kindergarten.py`
- Create: `apps/travel-map/app/institutions/sync.py`
- Create: `apps/travel-map/app/providers/__init__.py`
- Create: `apps/travel-map/app/providers/kakao_local.py` (Task 4에서는 주소 배치 지오코딩만 구현)
- Create: `apps/travel-map/scripts/sync-institutions.py`
- Create: `apps/travel-map/resources/institution-sources/sen-institutions.csv`
- Create: `apps/travel-map/resources/institution-sources/kindergarten-region-codes.csv`
- Create: `apps/travel-map/resources/institution-snapshots/current.json`
- Create: `apps/travel-map/resources/institution-snapshots/20260810T000000Z-initial/institutions.jsonl`
- Create: `apps/travel-map/resources/institution-snapshots/20260810T000000Z-initial/sites.jsonl`
- Create: `apps/travel-map/resources/institution-snapshots/20260810T000000Z-initial/manifest.json`
- Test: `apps/travel-map/tests/institutions/test_sync.py`
- Fixture: `apps/travel-map/tests/fixtures/institutions/sources/neis-school-info.json`
- Fixture: `apps/travel-map/tests/fixtures/institutions/sources/kindergarten-info.json`
- Fixture: `apps/travel-map/tests/fixtures/institutions/sources/sen-institutions.csv`

**Interfaces:**

- Consumes: `Institution`, `InstitutionSite`, `verify_snapshot()`, and `CoverageService` from Tasks 2–3.
- Produces: `build_candidate_snapshot(records, previous, output_root, snapshot_id) -> SnapshotBuildResult`.
- Produces: `promote_snapshot(candidate, output_root) -> None`, which replaces `current.json` only after all gates pass.
- Produces: `KakaoLocalClient.geocode(address: str) -> GeocodeResult | None` for source rows without coordinates.

- [ ] **Step 1: 세 원천 parser의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/institutions/test_sync.py
import json
from pathlib import Path

import pytest

from app.institutions.snapshot import load_current_snapshot
from app.institutions.sources.common import SourceInstitutionRecord
from app.institutions.sources.kindergarten import parse_kindergarten_rows
from app.institutions.sources.neis import parse_neis_rows
from app.institutions.sources.sen import parse_sen_csv
from app.institutions.sync import (
    SnapshotQualityError,
    build_candidate_snapshot,
    promote_snapshot,
)


def load_json(name: str) -> dict[str, object]:
    path = Path("apps/travel-map/tests/fixtures/institutions/sources") / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_source_ids_are_namespaced_and_private_schools_are_kept() -> None:
    neis = parse_neis_rows(load_json("neis-school-info.json"))
    kinder = parse_kindergarten_rows(load_json("kindergarten-info.json"))
    sen = parse_sen_csv(Path("apps/travel-map/tests/fixtures/institutions/sources/sen-institutions.csv"))
    assert {row.institution_id for row in neis} == {
        "neis:B10:7010001",
        "neis:B10:7010002",
    }
    assert {row.foundation_type for row in neis} == {"PUBLIC", "PRIVATE"}
    assert kinder[0].institution_id == "kinder:K12345678"
    assert sen[0].institution_id == "sen:headquarters"
```

NEIS fixture는 `schoolInfo[1].row` 구조와 `ATPT_OFCDC_SC_CODE`, `SD_SCHUL_CODE`, `SCHUL_NM`, `SCHUL_KND_SC_NM`, `FOND_SC_NM`, `ORG_RDNMA`, `LOAD_DTM`을 포함한다. 유치원 fixture는 `status`, `message`, `kinderInfo[]` 아래 `kinderCode`, `officeedu`, `subofficeedu`, `kindername`, `establish`, `addr`, `lttdcdnt`, `lngtcdnt`, `pbnttmng`을 포함한다.

- [ ] **Step 2: 품질 gate와 원자적 승격 RED 테스트를 작성한다.**

```python
def test_failed_candidate_does_not_replace_current_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    records = tuple(
        SourceInstitutionRecord(
            institution_id=f"neis:B10:{index:07d}",
            official_name=f"검증학교{index}",
            institution_type="ELEMENTARY_SCHOOL",
            foundation_type="PUBLIC",
            education_office="서울특별시교육청",
            road_address=f"서울특별시 중구 검증로 {index}",
            district="중구",
            latitude=37.56,
            longitude=126.97 + index / 100_000,
            source="NEIS",
            source_region_code="B10",
            source_as_of="2026-08-10",
        )
        for index in range(10)
    )
    initial = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="initial",
    )
    promote_snapshot(initial, root)
    before = (root / "current.json").read_bytes()
    result = build_candidate_snapshot(
        records=records[:6],
        previous=load_current_snapshot(root),
        output_root=root,
        snapshot_id="candidate-with-drop",
    )
    assert result.approved is False
    with pytest.raises(SnapshotQualityError, match="record count drop"):
        promote_snapshot(result, root)
    assert (root / "current.json").read_bytes() == before
```

- [ ] **Step 3: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_sync.py -q
```

Expected: source parser와 snapshot builder 부재로 실패한다.

- [ ] **Step 4: 원천별 adapter를 구현한다.**

`NeisSource.fetch()`는 `https://open.neis.go.kr/hub/schoolInfo`에 `ATPT_OFCDC_SC_CODE=B10`, JSON, 페이지·1000행을 요청하고 `list_total_count`까지 페이지를 모두 읽는다. `KindergartenSource.fetch()`는 `https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do`에 인증키, `sidoCode=11`, `pageCnt=100`, `currentPage`, 공식 코드표의 각 `sggCode`를 보내 페이지 끝까지 읽는다. `kindergarten-region-codes.csv`는 유치원알리미가 게시한 `시도시군구코드.xlsx`에서 서울 행만 정규화하고 원본 URL·공시차수·SHA-256을 머리말에 기록한다. 25개 구 코드를 코드 상수로 하드코딩하지 않는다. `SenCsvSource`는 [서울시교육청 기관안내](https://www.sen.go.kr/www/website.jsp)에서 검수해 만든 CSV의 `source_url`, `source_as_of`, `source_sha256`, 기관명·유형·주소만 읽는다.

```python
# apps/travel-map/app/institutions/sources/common.py
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceInstitutionRecord:
    institution_id: str
    official_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    road_address: str
    district: str
    latitude: float | None
    longitude: float | None
    source: str
    source_region_code: str
    source_as_of: str
```

```python
# apps/travel-map/app/providers/kakao_local.py
@dataclass(frozen=True)
class GeocodeResult:
    road_address: str
    latitude: float
    longitude: float
    confidence: str
```

모든 HTTP adapter는 `httpx.AsyncClient`, connect 2초/read 5초 timeout, 최대 2회 제한 재시도를 사용하고 secret을 예외 메시지에 포함하지 않는다. 원천 row를 `SourceInstitutionRecord`로 바꿀 때 전화·대표자 필드는 모델에 존재하지 않게 한다.

설립구분 정규화는 NEIS `국립/공립/사립`, 유치원알리미 `국립/공립(단설)/공립(병설)/사립(법인)/사립(사인)`을 각각 `NATIONAL/PUBLIC/PRIVATE`로 매핑한다. 알려지지 않은 새 값은 PRIVATE 등으로 추정하지 않고 품질 gate를 실패시킨다. 학교유형은 `유치원`, `초등학교`, `중학교`, `고등학교`, `특수학교`, `각종학교`를 `KINDERGARTEN`, `ELEMENTARY_SCHOOL`, `MIDDLE_SCHOOL`, `HIGH_SCHOOL`, `SPECIAL_SCHOOL`, `MISC_SCHOOL`로 정규화한다. 교육청 검수 CSV는 `HEADQUARTERS`, `DISTRICT_OFFICE`, `DIRECT_AGENCY`, `LIBRARY`, `LIFELONG_LEARNING_CENTER`만 허용한다.

NEIS처럼 위경도가 없는 원천은 `KakaoLocalClient.geocode()`가 `GET https://dapi.kakao.com/v2/local/search/address.json`을 서버 배치에서 호출한다. 정확한 도로명 주소 결과 1건만 `coordinate_quality="geocoded"`로 채택하고 0건·복수 모호 결과는 `REVIEW_REQUIRED`로 격리한다. 유치원알리미 원천 좌표는 `coordinate_quality="source_coordinate"`, 사람이 정문을 확인한 좌표는 `manually_verified`로 기록한다.

- [ ] **Step 5: 서울 이중 검증과 cross-source 비병합을 구현한다.**

`build_candidate_snapshot()`은 `source_region_code`가 원천별 서울 코드인지 먼저 확인한다. 허용값은 NEIS `B10`, 유치원알리미 `11`, 서울교육청 검수 CSV `SEOUL`이며 adapter가 원문에서 그대로 채우고 builder가 source와 code 조합을 검증한다. 이어서 지오코딩된 WGS84 좌표가 `CoverageState.SEOUL`인지 다시 확인한다. 주소·코드·좌표가 불일치하거나 지오코딩 신뢰도가 낮으면 `REVIEW_REQUIRED` 격리 건수에 넣고 ACTIVE 검색에 승격하지 않는다. 동일 source ID만 snapshot 간 연결하며 이름+주소가 같은 다른 namespace는 `possibleMatches`에 기록하고 합치지 않는다.

기본 출발지 ID는 namespaced institution ID 뒤에 `:main`을 붙인다. 공식 목록에 별도 분관·분교 코드가 있으면 그 코드로 독립 institution ID와 site ID를 만들고, 한 기관 안에 여러 물리 주소만 있는 경우 `:main` 또는 `:branch-` 뒤에 공식 site code를 붙인다. 실제 fixture 값은 `neis:B10:7010001:main`, `sen:gangseo-library:gayang`처럼 고정한다.

- [ ] **Step 6: manifest 품질 gate와 원자적 current 교체를 구현한다.**

manifest에는 원천 endpoint, 요청 지역·공시차수, 라이선스·출처표시 문구, 수집시각, 원본·정규화 SHA-256, 원천별 행·페이지 수, 상태·유형·설립구분별 건수, 좌표 품질 분포, cross-source 가능 일치 건수와 이전 snapshot diff를 기록한다. candidate는 `approved=false`로 생성한다. gate는 중복 source ID 0개, 페이지 완주, 좌표 검증 성공률 98% 이상, 직전 snapshot 대비 전체 ACTIVE 건수 감소 10% 이하, 알 수 없는 설립구분 0개를 요구한다. `promote_snapshot()`은 모든 gate를 다시 확인한 뒤 manifest를 `approved=true`, `approvedAt=UTC 현재시각`, `approvedByRole="data-steward"`로 원자 교체하고 candidate를 최종 디렉터리로 rename한 다음 같은 파일시스템의 임시 `current.json`을 `os.replace()`로 교체한다. 승인 역할 문자열에는 개인 이름·계정을 넣지 않는다.

```python
temporary = output_root / ".current.json.tmp"
with temporary.open("w", encoding="utf-8") as stream:
    stream.write(current_payload)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, output_root / "current.json")
```

위 테스트는 교체 실패 시 이전 포인터가 유지되는 것도 검사한다.

- [ ] **Step 7: offline sync 테스트를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/institutions -q
uv run --project apps/travel-map ruff check apps/travel-map/app/institutions apps/travel-map/scripts/sync-institutions.py apps/travel-map/tests/institutions
uv run --project apps/travel-map mypy apps/travel-map/app/institutions apps/travel-map/scripts/sync-institutions.py
```

Expected: 공사립 포함, 병설 비병합, 서울 밖 제외, 일시 누락 보존, 급감 차단, atomic pointer 테스트가 PASS한다.

- [ ] **Step 8: 실제 원천으로 최초 snapshot을 생성하고 총량을 대조한다.**

```bash
TRAVEL_MAP_LIVE_SMOKE=1 uv run --project apps/travel-map python apps/travel-map/scripts/sync-institutions.py \
  --sen-csv apps/travel-map/resources/institution-sources/sen-institutions.csv \
  --snapshot-root apps/travel-map/resources/institution-snapshots \
  --geodata-root apps/travel-map/resources/geodata
```

Expected: manifest의 NEIS·유치원·교육청 원천별 행 수, 유형·설립구분·25개 자치구별 건수, 격리 목록이 출력된다. 서울교육청 연간 학교 현황과 총량 차이가 1%를 넘으면 승격하지 않고 원천 누락을 수정한 뒤 다시 실행한다.

- [ ] **Step 9: 동기화 코드와 승인 snapshot을 커밋한다.**

```bash
git add apps/travel-map/app/institutions/sources apps/travel-map/app/institutions/sync.py apps/travel-map/app/providers/__init__.py apps/travel-map/app/providers/kakao_local.py apps/travel-map/scripts/sync-institutions.py apps/travel-map/resources/institution-sources/sen-institutions.csv apps/travel-map/resources/institution-sources/kindergarten-region-codes.csv apps/travel-map/resources/institution-snapshots apps/travel-map/tests/institutions/test_sync.py apps/travel-map/tests/fixtures/institutions/sources
git commit -m "feat: sync Seoul education institutions"
```

## Task 5: 공통 경로 계약·복수 경로 정규화·순위

**Files:**

- Modify: `apps/travel-map/app/routing/models.py`
- Create: `apps/travel-map/app/routing/provider.py`
- Create: `apps/travel-map/app/routing/ranking.py`
- Create: `apps/travel-map/app/routing/orchestrator.py`
- Create: `apps/travel-map/app/routing/bootstrap.py`
- Test: `apps/travel-map/tests/routing/test_ranking.py`
- Test: `apps/travel-map/tests/routing/test_orchestrator.py`
- Test: `apps/travel-map/tests/routing/test_bootstrap.py`
- Test support: `apps/travel-map/tests/routing/fakes.py`

**Interfaces:**

- Produces: immutable models `Coordinate`, `TravelMode`, `CostStatus`, `RouteQuery`, `RouteOption`, `ProviderWarning`, `ProviderResult` in `app.routing.models`.
- Produces: `RouteProvider.get_routes(query: RouteQuery) -> ProviderResult` protocol.
- Produces: `RouteOrchestrator.collect(query_base, requested_modes) -> RouteCollection`.
- Produces: `build_route_providers(settings) -> dict[TravelMode, tuple[RouteProvider, ...]]`; stages B·C modify only this registry.
- Produces: 독립 확장점 `build_car_provider_chain(settings) -> tuple[RouteProvider, ...]`와 `build_walk_provider_chain(settings) -> tuple[RouteProvider, ...]`; 단계 B는 전자만, 단계 C는 후자만 수정한다.
- Produces: `build_classification_provider(settings) -> RouteProvider`; Stage A returns `KakaoCarProvider(priority="DISTANCE", alternatives=False)`.

- [ ] **Step 1: 대표 3개와 비용 미상 규칙의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/routing/test_ranking.py
from app.routing.ranking import rank_routes
from tests.routing.fakes import route


def test_rank_routes_selects_fastest_shortest_and_known_cheapest() -> None:
    routes = (
        route("fast", seconds=600, meters=5_000, cost=3_000),
        route("short", seconds=900, meters=3_000, cost=2_000),
        route("cheap", seconds=1_200, meters=4_000, cost=0),
        route("unknown", seconds=300, meters=1_000, cost=None),
    )
    best = rank_routes(routes)
    assert best.fastest_route_id == "unknown"
    assert best.shortest_route_id == "unknown"
    assert best.cheapest_route_id == "cheap"
```

- [ ] **Step 2: 제공자 부분 실패와 fallback의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/routing/test_orchestrator.py
import pytest

from app.routing.models import TravelMode
from app.routing.orchestrator import RouteOrchestrator
from tests.routing.fakes import FakeProvider, base_query, failed_result, result_with, route


@pytest.mark.asyncio
async def test_orchestrator_uses_second_provider_after_primary_failure() -> None:
    primary = FakeProvider("public", result=failed_result("UPSTREAM_TIMEOUT"))
    fallback = FakeProvider("kakao", result=result_with(route("fallback", 700, 4_000, 1_500)))
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (primary, fallback)}, max_concurrency=3
    )
    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})
    assert [item.id for item in collection.routes] == ["fallback"]
    assert collection.warnings[0].code == "UPSTREAM_TIMEOUT"
```

- [ ] **Step 3: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/routing -q
```

Expected: `app.routing` 부재로 실패한다.

- [ ] **Step 4: 이후 단계가 변경하지 않을 provider 계약을 구현한다.**

```python
# apps/travel-map/app/routing/models.py
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TravelMode(StrEnum):
    TRANSIT = "TRANSIT"
    CAR = "CAR"
    WALK = "WALK"


class CostStatus(StrEnum):
    KNOWN = "KNOWN"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


class FuelType(StrEnum):
    GASOLINE = "GASOLINE"
    DIESEL = "DIESEL"
    LPG = "LPG"


@dataclass(frozen=True)
class CarAssumptions:
    fuel_type: FuelType
    efficiency_km_per_liter: float
    parking_cost_krw: int


@dataclass(frozen=True)
class RouteCostBreakdown:
    fare_krw: int = 0
    fuel_krw: int = 0
    toll_krw: int = 0
    parking_krw: int = 0

    @property
    def total_krw(self) -> int:
        return self.fare_krw + self.fuel_krw + self.toll_krw + self.parking_krw


@dataclass(frozen=True)
class RouteQuery:
    origin: Coordinate
    destination: Coordinate
    depart_at: datetime
    mode: TravelMode
    car_assumptions: CarAssumptions | None = None


@dataclass(frozen=True)
class RouteOption:
    id: str
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None
    cost_status: CostStatus
    cost_breakdown: RouteCostBreakdown | None
    geometry: tuple[Coordinate, ...]
    source: str
    source_as_of: datetime
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderWarning:
    code: str
    message: str
    source: str


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    routes: tuple[RouteOption, ...]
    warnings: tuple[ProviderWarning, ...] = ()


@dataclass(frozen=True)
class BestRouteIds:
    fastest_route_id: str | None
    shortest_route_id: str | None
    cheapest_route_id: str | None


@dataclass(frozen=True)
class RouteCollection:
    routes: tuple[RouteOption, ...]
    best: BestRouteIds
    warnings: tuple[ProviderWarning, ...]
```

Task 2에서 만든 `Coordinate`는 이 파일의 맨 위에 그대로 유지한다. `RouteOption` 생성 시 음수 시간·거리·비용, mode 불일치, geometry 2점 미만을 `ValueError`로 거부하는 `__post_init__` 테스트를 추가한다.

```python
# apps/travel-map/app/routing/provider.py
from typing import Protocol

from app.routing.models import ProviderResult, RouteQuery, TravelMode


class RouteProvider(Protocol):
    name: str
    supported_modes: frozenset[TravelMode]

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        raise NotImplementedError
```

테스트 helper는 다음 실제 모델만 사용한다.

```python
# apps/travel-map/tests/routing/fakes.py
from datetime import datetime
from zoneinfo import ZoneInfo

from app.routing.models import (
    Coordinate,
    CostStatus,
    ProviderResult,
    ProviderWarning,
    RouteOption,
    RouteQuery,
    TravelMode,
)


def route(route_id: str, seconds: int, meters: int, cost: int | None) -> RouteOption:
    return RouteOption(
        id=route_id,
        mode=TravelMode.TRANSIT,
        duration_seconds=seconds,
        distance_meters=meters,
        mobility_cost_krw=cost,
        cost_status=CostStatus.UNKNOWN if cost is None else CostStatus.KNOWN,
        cost_breakdown=None,
        geometry=(Coordinate(37.55, 126.97), Coordinate(37.56, 126.98)),
        source="FAKE",
        source_as_of=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )


def result_with(item: RouteOption) -> ProviderResult:
    return ProviderResult(provider="FAKE", routes=(item,))


def failed_result(code: str) -> ProviderResult:
    return ProviderResult(
        provider="FAKE",
        routes=(),
        warnings=(ProviderWarning(code=code, message="fixture failure", source="FAKE"),),
    )


def base_query() -> RouteQuery:
    return RouteQuery(
        origin=Coordinate(37.55, 126.97),
        destination=Coordinate(37.56, 126.98),
        depart_at=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        mode=TravelMode.TRANSIT,
        car_assumptions=None,
    )


class FakeProvider:
    supported_modes = frozenset({TravelMode.TRANSIT})

    def __init__(self, name: str, result: ProviderResult) -> None:
        self.name = name
        self.result = result
        self.call_count = 0

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        self.call_count += 1
        return self.result
```

- [ ] **Step 5: 중복제거와 안정 정렬을 구현한다.**

동일 mode에서 거리 차이 2% 이하, 시간 차이 2% 이하, 양 끝점이 각각 20m 이내이고 EPSG:5179에서 만든 10m buffered geometry의 상호 겹침률이 95% 이상인 후보만 중복으로 보고 source 우선순위가 높은 하나를 남긴다. 시간·거리만 비슷하지만 다른 도로·보행로를 쓰는 경로는 보존하는 회귀 테스트를 넣는다. 대표 순위의 동점 기준은 주 지표, 시간, 거리, provider priority, route ID다. 비용 `UNKNOWN`은 최저비용 경쟁에서 제외하고 알려진 비용 경로가 하나도 없으면 `cheapest_route_id=None`이다.

- [ ] **Step 6: 동시성 제한과 mode별 provider chain을 구현한다.**

`RouteOrchestrator`는 mode를 병렬 조회하되 각 mode 안에서는 provider chain 순서로 호출한다. primary가 유효 경로를 반환하면 fallback을 호출하지 않고, 빈 경로·timeout·capability 누락이면 경고를 보존하고 다음 provider를 호출한다. 전체 실패를 0분·0원 경로로 만들지 않는다.

`build_route_providers()`의 단계 A 반환은 다음 순서로 고정한다. 자동차와 도보 helper를 분리해 단계 B·C를 어느 순서로 적용해도 서로의 chain을 덮어쓰지 않게 한다.

```python
def build_car_provider_chain(settings: Settings) -> tuple[RouteProvider, ...]:
    return (KakaoCarProvider.from_settings(settings),)


def build_walk_provider_chain(settings: Settings) -> tuple[RouteProvider, ...]:
    return (KakaoWalkProvider.from_settings(settings),)


def build_route_providers(
    settings: Settings,
) -> dict[TravelMode, tuple[RouteProvider, ...]]:
    seoul_transit = SeoulTransitProvider.from_settings(settings)
    kakao_transit = KakaoTransitProvider.from_settings(settings)
    return {
        TravelMode.TRANSIT: (seoul_transit, kakao_transit),
        TravelMode.CAR: build_car_provider_chain(settings),
        TravelMode.WALK: build_walk_provider_chain(settings),
    }
```

`build_classification_provider()`는 화면 자동차 후보와 별개 instance를 만들고 Kakao `DISTANCE` priority를 고정한다. 그 결과는 왕복 법적 판정용으로만 쓰고 `best` ranking에 넣지 않는다.

- [ ] **Step 7: routing 계약 테스트를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/routing -q
uv run --project apps/travel-map ruff check apps/travel-map/app/routing apps/travel-map/tests/routing
uv run --project apps/travel-map mypy apps/travel-map/app/routing
```

Expected: 순위·동점·중복·비용 미상·부분 실패·fallback·동시성 테스트가 PASS한다.

- [ ] **Step 8: 공통 경로 계약을 커밋한다.**

```bash
git add apps/travel-map/app/routing apps/travel-map/tests/routing
git commit -m "feat: add normalized route orchestration"
```

## Task 6: 장소검색과 대중교통·자동차·도보 provider

**Files:**

- Modify: `apps/travel-map/app/settings.py`
- Modify: `apps/travel-map/app/providers/kakao_local.py` (키워드 검색 추가)
- Create: `apps/travel-map/app/providers/kakao_map.py`
- Create: `apps/travel-map/app/providers/kakao_mobility.py`
- Create: `apps/travel-map/app/providers/opinet.py`
- Create: `apps/travel-map/app/providers/seoul_transit.py`
- Modify: `apps/travel-map/app/routing/orchestrator.py`
- Modify: `apps/travel-map/app/routing/bootstrap.py`
- Create: `apps/travel-map/scripts/probe-route-providers.py`
- Test: `apps/travel-map/tests/providers/test_kakao_local.py`
- Test: `apps/travel-map/tests/providers/test_kakao_map.py`
- Test: `apps/travel-map/tests/providers/test_kakao_mobility.py`
- Test: `apps/travel-map/tests/providers/test_opinet.py`
- Test: `apps/travel-map/tests/providers/test_seoul_transit.py`
- Fixture: `apps/travel-map/tests/fixtures/providers/kakao-keyword.json`
- Fixture: `apps/travel-map/tests/fixtures/providers/kakao-coord2address.json`
- Fixture: `apps/travel-map/tests/fixtures/providers/kakao-publictraffic.json`
- Fixture: `apps/travel-map/tests/fixtures/providers/kakao-walk.json`
- Fixture: `apps/travel-map/tests/fixtures/providers/kakao-car.json`
- Fixture: `apps/travel-map/tests/fixtures/providers/opinet-average.json`
- Fixture: `apps/travel-map/tests/fixtures/providers/seoul-transit.xml`

**Interfaces:**

- Consumes: Task 5 `RouteProvider` and immutable routing models.
- Produces: `KakaoLocalClient.search(query, bounds) -> tuple[PlaceCandidate, ...]` and `reverse_geocode(point: Coordinate) -> PlaceCandidate | None`.
- Produces: `SeoulTransitProvider`, `KakaoTransitProvider`, `KakaoWalkProvider`, `KakaoCarProvider` implementing `get_routes()`.
- Produces: `OpinetClient.average_price(fuel_type: FuelType) -> FuelPrice` and car `RouteCostBreakdown`.

- [ ] **Step 1: 외부 응답 정규화의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/providers/test_kakao_map.py
import json
from pathlib import Path

import httpx
import pytest

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from app.providers.kakao_map import KakaoTransitProvider
from app.routing.models import Coordinate, RouteQuery, TravelMode


@pytest.mark.asyncio
async def test_kakao_public_transit_returns_all_routes_with_fares(respx_mock) -> None:
    respx_mock.get("https://dapi.kakao.com/v2/routing/publictraffic").mock(
        return_value=httpx.Response(
            200,
            json=json.loads(
                Path("apps/travel-map/tests/fixtures/providers/kakao-publictraffic.json").read_text(
                    encoding="utf-8"
                )
            ),
        )
    )
    async with httpx.AsyncClient() as http:
        provider = KakaoTransitProvider(
            http=http,
            rest_key=SecretStr("test-rest-key"),
            now=lambda: datetime(2026, 8, 10, 9, 1, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        query = RouteQuery(
            origin=Coordinate(37.55, 126.97),
            destination=Coordinate(37.56, 126.98),
            depart_at=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            mode=TravelMode.TRANSIT,
        )
        result = await provider.get_routes(query)
    assert len(result.routes) == 3
    assert result.routes[0].duration_seconds == 2_820
    assert result.routes[0].distance_meters == 14_600
    assert result.routes[0].mobility_cost_krw == 1_550
    assert len(result.routes[0].geometry) > 2
```

Kakao walk는 `BROAD_FIRST`, `SHORTEST`, `ACCESSIBLE`을 각각 호출해 route ID가 다른 후보를 만들고, Kakao Mobility car는 `alternatives=true`, `summary=false` 응답의 모든 section geometry와 fare/toll을 정규화하는 테스트를 별도로 작성한다.

오피넷 비용 테스트는 `https://www.opinet.co.kr/api/avgAllPrice.do?out=json` mock fixture에서 `B027` 휘발유 가격을 읽고 다음 계산을 검증한다.

```python
def test_car_cost_uses_distance_efficiency_fuel_toll_and_parking() -> None:
    breakdown = estimate_car_cost(
        distance_meters=20_000,
        fuel_price_krw_per_liter=1_700.0,
        assumptions=CarAssumptions(
            fuel_type=FuelType.GASOLINE,
            efficiency_km_per_liter=10.0,
            parking_cost_krw=2_000,
        ),
        toll_krw=1_000,
    )
    assert breakdown == RouteCostBreakdown(
        fuel_krw=3_400,
        toll_krw=1_000,
        parking_krw=2_000,
    )
    assert breakdown.total_krw == 6_400
```

- [ ] **Step 2: 장소검색과 서울 transit fallback RED 테스트를 작성한다.**

```python
from app.providers.kakao_local import BoundingBox, KakaoLocalClient
from app.providers.seoul_transit import SeoulTransitProvider


def transit_query() -> RouteQuery:
    return RouteQuery(
        origin=Coordinate(37.55, 126.97),
        destination=Coordinate(37.56, 126.98),
        depart_at=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        mode=TravelMode.TRANSIT,
    )


@pytest.mark.asyncio
async def test_place_search_rejects_blank_and_limits_results(respx_mock) -> None:
    payload = json.loads(
        Path("apps/travel-map/tests/fixtures/providers/kakao-keyword.json").read_text(encoding="utf-8")
    )
    respx_mock.get("https://dapi.kakao.com/v2/local/search/keyword.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with httpx.AsyncClient() as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-rest-key"))
        bounds = BoundingBox(west=126.70, south=37.40, east=127.30, north=37.75)
        assert await client.search("   ", bounds=bounds) == ()
        assert len(await client.search("서울시청", bounds=bounds)) <= 15


@pytest.mark.asyncio
async def test_seoul_transit_reports_missing_geometry_capability(respx_mock) -> None:
    respx_mock.get(
        "http://ws.bus.go.kr/api/rest/pathinfo/getPathInfoByBusNSub"
    ).mock(
        return_value=httpx.Response(
            200,
            content=Path("apps/travel-map/tests/fixtures/providers/seoul-transit.xml").read_bytes(),
        )
    )
    async with httpx.AsyncClient() as http:
        provider = SeoulTransitProvider(
            http=http,
            service_key=SecretStr("test-seoul-key"),
            now=lambda: datetime(2026, 8, 10, 9, 1, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        result = await provider.get_routes(transit_query())
    assert result.routes
    assert any(w.code == "GEOMETRY_MISSING" for w in result.warnings)
```

- [ ] **Step 3: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/providers -q
```

Expected: provider 모듈 부재로 실패한다.

- [ ] **Step 4: secret-safe settings와 공통 HTTP 규칙을 구현한다.**

`Settings`는 `SecretStr` REST 키, exact `allowed_hosts`·`allowed_origins`, provider timeout, route concurrency, 환경 이름을 읽는다. production 환경에서 필수 키나 허용 host가 비어 있으면 startup을 실패시킨다. 어떤 `repr`, validation error, logger에도 `SecretStr.get_secret_value()`를 전달하지 않는다.

`kakao_local.py`에 외부 검색 결과 계약을 다음처럼 둔다.

```python
@dataclass(frozen=True)
class PlaceCandidate:
    place_id: str
    name: str
    road_address: str
    lot_address: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class BoundingBox:
    west: float
    south: float
    east: float
    north: float
```

`RouteCostBreakdown.total_krw`는 네 구성요소 합을 반환하는 property다. `FuelPrice`는 `fuel_type`, `krw_per_liter`, `trade_date`, `source="OPINET"`을 갖는 frozen dataclass다.

- [ ] **Step 5: Kakao Local·Map·Mobility provider를 구현한다.**

`SeoulTransitProvider`, `KakaoTransitProvider`, `KakaoWalkProvider`, `KakaoCarProvider`는 모두 `from_settings(settings: Settings)` classmethod를 제공해 Task 5의 chain helper가 같은 방식으로 조립한다. 각 factory가 만든 `httpx.AsyncClient`는 provider의 `aclose()`로 닫을 수 있어야 하며 Task 7의 `AppDependencies.aclose()`가 중복 없이 모두 종료한다.

- Local: `GET https://dapi.kakao.com/v2/local/search/keyword.json`, 검색어 2~80자, page 1, size 15, WGS84 결과만 허용.
- Reverse geocode: `GET https://dapi.kakao.com/v2/local/geo/coord2address.json`, 지도 클릭 경위도 1건을 도로명주소 우선 후보로 변환.
- Transit: `GET https://dapi.kakao.com/v2/routing/publictraffic`, `start_x/start_y/end_x/end_y`, 응답의 `routes[].properties.totalDistance`, `totalTime`, `fare.value`, `steps[].path.points`를 사용.
- Walk: `GET https://dapi.kakao.com/v2/routing/walk`, `route_mode` 3종, `route.properties.totalDistance/totalTime`, `legs[].steps[].path.points`를 사용.
- Car: `GET https://apis-navi.kakaomobility.com/v1/directions`, `alternatives=true`, `summary=false`, `priority=RECOMMEND`; summary distance/duration, fare toll, roads vertexes를 사용. 택시 예상요금은 자가 운전비에 더하지 않는다.
- Fuel: `GET https://www.opinet.co.kr/api/avgAllPrice.do?out=json`; `B027` 휘발유, `D047` 경유, `K015` 자동차부탄의 `PRICE`와 `TRADE_DT`를 사용한다.

모든 요청은 `Authorization: KakaoAK ${REST_API_KEY}`를 서버에서만 넣고 429·5xx·schema mismatch를 `ProviderWarning`으로 반환한다. 응답 좌표는 `[x, y]`에서 `Coordinate(latitude=y, longitude=x)`로 한 곳에서 변환한다. 자동차 provider는 `RouteQuery.car_assumptions`와 오피넷 가격, Kakao toll을 사용해 `RouteCostBreakdown`과 `mobility_cost_krw`를 계산한다. 유가가 없고 유효한 마지막 캐시도 없으면 비용을 꾸며내지 않고 `CostStatus.UNKNOWN`으로 반환한다.

- [ ] **Step 6: 서울 대중교통 provider와 capability 보완을 구현한다.**

`SeoulTransitProvider`는 서버에서 `http://ws.bus.go.kr/api/rest/pathinfo/getPathInfoByBusNSub`를 호출하고 `startX`, `startY`, `endX`, `endY`, service key를 전달한다. XML의 총시간·총거리·요금·환승 단계 중 존재하는 필드만 엄격히 검증해 `RouteOption`으로 만들고, geometry나 fare가 없으면 해당 capability 경고를 붙인다. `RouteOrchestrator`는 필수 geometry가 없는 공공 경로를 Kakao 동일 mode 결과로 보완하되 최종 `source`를 `SEOUL_TRANSIT+KAKAO_GEOMETRY`처럼 기록한다.

- [ ] **Step 7: 실제 키 opt-in probe로 계약을 동결한다.**

```bash
TRAVEL_MAP_LIVE_SMOKE=1 uv run --project apps/travel-map python apps/travel-map/scripts/probe-route-providers.py \
  --origin 126.9779451,37.5662952 \
  --destination 126.9910,37.5512
```

Expected: raw payload나 키를 저장하지 않고 provider별 HTTP 상태, 경로 수, total time/distance/fare/geometry 존재 여부, 응답 schema fingerprint만 `artifacts/provider-contract-report.json`에 기록한다. 문서 fixture와 실제 필드명이 다르면 parser·fixture를 같은 커밋에서 고치고 schema mismatch 테스트를 추가한다.

- [ ] **Step 8: provider 테스트와 정적 검사를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/providers apps/travel-map/tests/routing -q
uv run --project apps/travel-map ruff check apps/travel-map/app/providers apps/travel-map/app/settings.py apps/travel-map/scripts/probe-route-providers.py apps/travel-map/tests/providers
uv run --project apps/travel-map mypy apps/travel-map/app/providers apps/travel-map/app/settings.py
```

Expected: 네트워크 없이 parser·timeout·429·빈 결과·부분 capability·fallback 테스트가 PASS한다.

- [ ] **Step 9: provider adapter만 커밋한다.**

```bash
git add apps/travel-map/app/settings.py apps/travel-map/app/providers apps/travel-map/app/routing/bootstrap.py apps/travel-map/scripts/probe-route-providers.py apps/travel-map/tests/providers apps/travel-map/tests/fixtures/providers
git commit -m "feat: add route and place providers"
```

## Task 7: 공개 API 계약·trip preview·캐시·호출 제한

**Files:**

- Create: `apps/travel-map/app/contracts.py`
- Create: `apps/travel-map/app/dependencies.py`
- Create: `apps/travel-map/app/cache.py`
- Create: `apps/travel-map/app/rate_limit.py`
- Create: `apps/travel-map/app/services/__init__.py`
- Create: `apps/travel-map/app/services/trip_preview.py`
- Create: `apps/travel-map/app/api/__init__.py`
- Create: `apps/travel-map/app/api/bootstrap.py`
- Create: `apps/travel-map/app/api/institutions.py`
- Create: `apps/travel-map/app/api/geodata.py`
- Create: `apps/travel-map/app/api/places.py`
- Create: `apps/travel-map/app/api/trips.py`
- Modify: `apps/travel-map/app/main.py`
- Test: `apps/travel-map/tests/api/test_bootstrap.py`
- Test: `apps/travel-map/tests/api/test_institutions.py`
- Test: `apps/travel-map/tests/api/test_geodata.py`
- Test: `apps/travel-map/tests/api/test_places.py`
- Test: `apps/travel-map/tests/api/test_trips.py`
- Test support: `apps/travel-map/tests/api/conftest.py`
- Test: `apps/travel-map/tests/security/test_key_exposure.py`
- Test: `apps/travel-map/tests/security/test_rate_limit.py`

**Interfaces:**

- Consumes: verified `InstitutionStore`, `CoverageService`, `PolicyEngine`, `RouteOrchestrator`, place client and settings.
- Produces: `GET /api/v1/bootstrap`, `GET /api/v1/institutions`, `GET /api/v1/geodata/seoul`, `GET /api/v1/geodata/support`, `GET /api/v1/places`, `GET /api/v1/places/reverse`, `POST /api/v1/trips/preview`.
- Produces: `TripPreviewService.preview(request: TripPreviewRequest) -> TripPreviewResponse`.
- Produces: `AppDependencies` dataclass containing settings, institution store, coverage, policy, route orchestrator, `build_classification_provider()` 결과, place client, cache and rate limiter; `async AppDependencies.aclose()` closes every unique provider/client once.

- [ ] **Step 1: 외부 API 계약의 RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/api/test_trips.py
from tests.api.conftest import trip_payload


def test_trip_preview_resolves_origin_by_site_id_and_separates_costs(client) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json={
            "originSiteId": "neis:B10:7010001:main",
            "destination": {
                "name": "서울특별시청",
                "address": "서울특별시 중구 세종대로 110",
                "latitude": 37.5662952,
                "longitude": 126.9779451
            },
            "startsAt": "2026-08-10T09:00:00+09:00",
            "returnsAt": "2026-08-10T13:00:00+09:00",
            "policyProfile": "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED",
            "vehicleUse": "NONE",
            "carAssumptions": {
                "fuelType": "GASOLINE",
                "efficiencyKmPerLiter": 10.0,
                "parkingCostKrw": 0
            },
            "hasOtherLocalTripsToday": False,
            "previousAllowanceKrw": 0
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["origin"]["siteId"] == "neis:B10:7010001:main"
    assert body["best"]["fastestRouteId"]
    assert body["mobilityCost"] != body["allowance"]
    assert body["allowance"]["amountKrw"] == 20_000
```

`client` fixture는 fake institution snapshot, fake route providers와 fake place client를 app dependency에 주입한다. 외부 좌표를 origin 필드에 추가하면 422가 되는 테스트도 작성한다.

- [ ] **Step 2: 지원영역 밖과 신분 미확인의 RED 테스트를 작성한다.**

```python
def test_outside_coverage_stops_before_provider_calls(client, fake_provider) -> None:
    payload = trip_payload(
        destination={
            "name": "부산광역시청",
            "address": "부산광역시 연제구 중앙대로 1001",
            "latitude": 35.1798159,
            "longitude": 129.0750222,
        }
    )
    response = client.post("/api/v1/trips/preview", json=payload)
    assert response.status_code == 200
    assert response.json()["coverage"]["status"] == "OUT_OF_COVERAGE"
    assert response.json()["routes"] == []
    assert fake_provider.call_count == 0


def test_unknown_profile_returns_routes_but_withholds_allowance(client) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(policyProfile="NONPUBLIC_OR_UNKNOWN"),
    )
    assert response.json()["routes"]
    assert response.json()["allowance"]["status"] == "REVIEW_REQUIRED"
    assert response.json()["allowance"]["amountKrw"] is None


def test_seoul_destination_uses_two_directional_distance_for_two_km_branch(
    client, fake_classification_provider
) -> None:
    fake_classification_provider.set_directional_distances(900, 1_100)
    response = client.post("/api/v1/trips/preview", json=trip_payload())
    body = response.json()
    assert body["classification"] == "LOCAL"
    assert body["classificationDistanceMeters"] == 2_000
    assert body["allowance"]["status"] == "REVIEW_REQUIRED"
    assert body["allowance"]["amountKrw"] is None
    assert [query.origin for query in fake_classification_provider.queries] == [
        fake_classification_provider.site_coordinate,
        fake_classification_provider.destination_coordinate,
    ]
```

`tests/api/conftest.py`에 반복 요청을 완전한 JSON으로 만드는 helper를 둔다.

```python
def trip_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "originSiteId": "neis:B10:7010001:main",
        "destination": {
            "name": "서울특별시청",
            "address": "서울특별시 중구 세종대로 110",
            "latitude": 37.5662952,
            "longitude": 126.9779451,
        },
        "startsAt": "2026-08-10T09:00:00+09:00",
        "returnsAt": "2026-08-10T13:00:00+09:00",
        "policyProfile": "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED",
        "vehicleUse": "NONE",
        "carAssumptions": {
            "fuelType": "GASOLINE",
            "efficiencyKmPerLiter": 10.0,
            "parkingCostKrw": 0,
        },
        "hasOtherLocalTripsToday": False,
        "previousAllowanceKrw": 0,
    }
    payload.update(overrides)
    return payload
```

같은 파일의 `fake_provider`는 Task 5 `FakeProvider`에 서울 transit fixture route를 넣고, `fake_classification_provider`는 방향별 거리 queue와 받은 `RouteQuery`를 보존해 outbound·return 순서를 검증한다. `client`는 합성 기관 snapshot·합성 서울 polygon·실제 policy engine·fake providers를 `AppDependencies`로 조립해 `TestClient(create_app(settings, dependencies))`를 반환한다. `settings`의 secret 값은 `public-js-key`, `rest-secret`, `seoul-secret`로 고정해 노출 테스트가 실제로 탐지할 수 있게 한다.

- [ ] **Step 3: bootstrap 키 노출과 rate-limit RED 테스트를 작성한다.**

```python
def test_bootstrap_exposes_only_domain_restricted_javascript_key(client) -> None:
    body = client.get("/api/v1/bootstrap").json()
    serialized = json.dumps(body)
    assert body["map"]["javascriptKey"] == "public-js-key"
    assert "rest-secret" not in serialized
    assert "seoul-secret" not in serialized


def test_rate_limit_returns_retry_after(client) -> None:
    for _ in range(10):
        assert client.get("/api/v1/places?q=서울시청").status_code in {200, 503}
    blocked = client.get("/api/v1/places?q=서울시청")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
```

- [ ] **Step 4: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/api apps/travel-map/tests/security -q
```

Expected: API router, contracts와 service 부재로 실패한다.

- [ ] **Step 5: Pydantic 요청·응답 계약을 구현한다.**

`TripPreviewRequest`는 목적지 위경도 범위, 검색명·주소 길이, timezone-aware 날짜, 최대 24시간 간격, 이전 지급액 0~20,000원을 검증한다. 응답은 `coverage`, `origin`, `institutionSnapshotId`, `policyScope`, `classification`, `classificationDistanceMeters`, `classificationPath`, `routes`, `best`, `mobilityCost`, `allowance`, `ruleSetId`, `effectiveFrom`, `sourceRefs`, `warnings`를 모두 명시한다. `classificationPath`는 `id`, 왕복 `distanceMeters`, 출국·복귀 geometry를 이어 붙인 좌표, source, 조회시각을 가지며 일반 route ranking에는 참여하지 않는다. alias generator로 Python snake_case를 외부 camelCase에 한 번만 변환한다.

```python
class DestinationInput(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    address: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    latitude: Annotated[float, Field(ge=33.0, le=39.5)]
    longitude: Annotated[float, Field(ge=124.0, le=132.0)]


class TripPreviewRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    origin_site_id: Annotated[str, StringConstraints(pattern=r"^[a-z]+:[A-Za-z0-9:_-]+$")]
    destination: DestinationInput
    starts_at: datetime
    returns_at: datetime
    policy_profile: PolicyProfile
    vehicle_use: VehicleUse
    car_assumptions: CarAssumptionsInput
    has_other_local_trips_today: bool
    previous_allowance_krw: Annotated[int, Field(ge=0, le=20_000)]
```

`CarAssumptionsInput`은 `fuel_type: FuelType`, `efficiency_km_per_liter: 3.0~30.0`, `parking_cost_krw: 0~100,000`을 검증한다. model validator는 두 datetime의 timezone 존재와 `0 < returns_at - starts_at <= 24시간`을 검사한다. 응답 하위 모델도 `extra="forbid"`로 만들며 `mobilityCost.amountKrw`와 `allowance.amountKrw`는 각각 독립 nullable field다.

- [ ] **Step 6: preview orchestration을 구현한다.**

`TripPreviewService.preview()` 순서는 다음으로 고정한다.

1. `originSiteId`를 `InstitutionStore.require_site()`로 해석한다.
2. 목적지 coverage를 계산하고 OUTSIDE면 즉시 응답한다.
3. TRANSIT·CAR·WALK를 병렬 조회하고 CAR query에 검증된 `CarAssumptions`를 넣는다.
4. 모든 지원영역 안 목적지는 표시용 후보와 별개인 `CLASSIFICATION_MODE=CAR`, `priority=DISTANCE`로 출국·복귀 각각 조회해 거리 합을 계산한다. 서울 안은 이 값이 관내 판정을 바꾸지 않지만 왕복 `<= 2,000m` 실비 분기에 사용한다.
5. 왕복 판정경로가 없으면 직선거리로 대체하지 않는다. 서울 밖 BUFFER는 `classification=REVIEW_REQUIRED`, 서울 안은 `classification=LOCAL`을 유지하되 `allowance.status=REVIEW_REQUIRED`로 두고 둘 다 `DATA_UNAVAILABLE`을 표시한다.
6. ranking과 policy engine 결과를 합쳐 이동비와 여비를 분리한다.

선택 경로가 바뀌어도 `classificationDistanceMeters`와 2km 분기는 변하지 않는다. 판정 provider cache를 표시용 자동차 경로와 공유할 수는 있지만, outbound·return 두 방향을 각각 확보했다는 계약을 생략하거나 편도×2로 바꾸지 않는다.

- [ ] **Step 7: TTL/LRU cache와 고정창 rate limiter를 구현한다.**

cache key는 provider, mode, 소수점 5자리로 양자화한 좌표, 출발시각 bucket, provider 옵션을 SHA-256한 값이다. 장소 24시간, 도보 7일, 자동차·대중교통 5분, 유가 1일 TTL을 사용한다. rate limiter는 신뢰한 proxy가 아니면 socket client IP만 사용하며 places 10회/분, preview 20회/분의 테스트 설정을 주입할 수 있게 한다.

- [ ] **Step 8: FastAPI router와 공개 안전장치를 조립한다.**

`create_app(settings, dependencies)`가 테스트 fake를 주입할 수 있게 하고 production에서는 lifespan에서 snapshot·경계·규칙을 한 번 검증한다. geodata endpoint는 검증된 정규화 GeoJSON만 `ETag`·장기 public cache header와 함께 반환한다. exact `TrustedHostMiddleware`, exact CORS origin, 32KB 요청 제한, JSON 오류 응답, 정적 파일을 조립한다. access log formatter는 URL path·status·latency만 기록하고 query string과 body를 버린다.

- [ ] **Step 9: API·보안 전체 테스트를 통과시킨다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/api apps/travel-map/tests/security apps/travel-map/tests/policy apps/travel-map/tests/routing -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests
uv run --project apps/travel-map mypy apps/travel-map/app
```

Expected: 200/422/429/503, 키 비노출, cache hit/expiry, 지원영역 조기종료, 판정거리 독립성, 부분 route 결과가 PASS한다.

- [ ] **Step 10: 서버 API를 커밋한다.**

```bash
git add apps/travel-map/app/contracts.py apps/travel-map/app/dependencies.py apps/travel-map/app/cache.py apps/travel-map/app/rate_limit.py apps/travel-map/app/services apps/travel-map/app/api apps/travel-map/app/main.py apps/travel-map/tests/api apps/travel-map/tests/security
git commit -m "feat: expose secure trip preview API"
```

## Task 8: 카카오 지도형 단일 화면 UI

**Files:**

- Create: `apps/travel-map/app/static/index.html`
- Create: `apps/travel-map/app/static/styles.css`
- Create: `apps/travel-map/app/static/api.js`
- Create: `apps/travel-map/app/static/kakao-map.js`
- Create: `apps/travel-map/app/static/app.js`
- Modify: `apps/travel-map/app/main.py` (정적 앱 mount)
- Test: `apps/travel-map/e2e/route-preview.spec.ts`
- Test support: `apps/travel-map/e2e/helpers.ts`
- Fixture: `apps/travel-map/e2e/fixtures/bootstrap.json`
- Fixture: `apps/travel-map/e2e/fixtures/institutions.json`
- Fixture: `apps/travel-map/e2e/fixtures/places.json`
- Fixture: `apps/travel-map/e2e/fixtures/preview.json`
- Create: `apps/travel-map/playwright.config.ts`

**Interfaces:**

- Consumes: Task 7 bootstrap, institutions, places and trip preview JSON contracts.
- Produces: accessible single-page flow with institution search, destination selection, policy confirmation, three route badges and selectable map polylines.

- [ ] **Step 1: mock API 기반 핵심 E2E RED 테스트를 작성한다.**

```typescript
// apps/travel-map/e2e/route-preview.spec.ts
import { expect, test } from "@playwright/test";
import { completePublicOfficialTrip, installMockApi } from "./helpers";

test("selects a private school origin and shows route rankings", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.getByLabel("출발 기관").fill("샘물");
  await page.getByRole("option", { name: /샘물사립고등학교.*사립.*강남구/ }).click();
  await page.getByLabel("출장지").fill("서울시청");
  await page.getByRole("option", { name: /서울특별시청.*세종대로 110/ }).click();
  await page.getByLabel("적용 규정").selectOption("NONPUBLIC_OR_UNKNOWN");
  await page.getByRole("button", { name: "경로 계산" }).click();

  await expect(page.getByText("최단시간")).toBeVisible();
  await expect(page.getByText("최단거리")).toBeVisible();
  await expect(page.getByText("최저비용")).toBeVisible();
  await expect(page.getByText("여비 판정 보류")).toBeVisible();
  await expect(page.getByText("예상 이동비")).toBeVisible();
});
```

`helpers.ts`는 fixture 응답과 지도 fake를 다음처럼 설치한다.

```typescript
// apps/travel-map/e2e/helpers.ts
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

const fixtureDir = join(dirname(fileURLToPath(import.meta.url)), "fixtures");

function fixture(name: string): object {
  return JSON.parse(readFileSync(join(fixtureDir, name), "utf8"));
}

async function json(route: Route, name: string): Promise<void> {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixture(name)) });
}

export async function installMockApi(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class MapFake { setBounds(): void {} }
    class OverlayFake { setMap(): void {} }
    class LatLngFake { constructor(public lat: number, public lng: number) {} }
    class BoundsFake { extend(): void {} }
    Object.assign(window, {
      kakao: { maps: {
        load: (callback: () => void) => callback(),
        Map: MapFake,
        Marker: OverlayFake,
        Polyline: OverlayFake,
        LatLng: LatLngFake,
        LatLngBounds: BoundsFake,
      } },
    });
  });
  await page.route("**/api/v1/bootstrap", route => json(route, "bootstrap.json"));
  await page.route("**/api/v1/institutions**", route => json(route, "institutions.json"));
  await page.route("**/api/v1/places**", route => json(route, "places.json"));
  await page.route("**/api/v1/trips/preview", route => json(route, "preview.json"));
}

export async function completePublicOfficialTrip(page: Page): Promise<void> {
  await page.getByLabel("출발 기관").fill("샘물");
  await page.getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ }).click();
  await page.getByLabel("출장지").fill("서울시청");
  await page.getByRole("option", { name: /서울특별시청.*세종대로 110/ }).click();
  await page.getByLabel("적용 규정").selectOption("SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED");
  await page.getByRole("button", { name: "경로 계산" }).click();
}
```

네 JSON fixture는 설계의 camelCase 계약을 완전하게 포함한다. `preview.json`에는 TRANSIT·CAR·WALK 각 1개, 서로 다른 시간·거리·비용, `best` 3개 ID, LOCAL, 20,000원 allowance와 geometry 2점 이상을 넣는다.

- [ ] **Step 2: 지도 경로 전환과 모바일 RED 테스트를 추가한다.**

```typescript
test("selecting a route updates the emphasized polyline", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await page.getByRole("button", { name: /도보.*35분/ }).click();
  await expect(page.locator("[data-route-id='walk-1']")).toHaveAttribute("aria-current", "true");
  await expect(page.locator("#map")).toHaveAttribute("data-active-route", "walk-1");
});
```

375×812 viewport에서 입력 순서, 결과 카드, 접을 수 있는 지도가 수평 스크롤 없이 보이는 테스트를 추가한다.

- [ ] **Step 3: E2E가 실패하는지 확인한다.**

```bash
pnpm --dir apps/travel-map exec playwright install chromium
pnpm --dir apps/travel-map test:e2e
```

Expected: 정적 UI와 Playwright 설정 부재로 실패한다.

- [ ] **Step 4: 접근성 있는 입력·검색 UI를 구현한다.**

기관과 출장지 검색은 label, combobox, listbox, option 역할과 키보드 위·아래·Enter·Escape 동작을 갖는다. 기관 검색에는 기관유형·설립구분·교육지원청·자치구 filter를 제공한다. 기관 결과는 이름·기관유형·설립구분·자치구·도로명주소를 표시하고, 선택 전 자유 텍스트로 계산하지 않는다. 지도 클릭은 `/api/v1/places/reverse` 결과를 출장지 후보로 표시하고 사용자가 주소를 확인해 선택한 뒤에만 계산한다. 적용 규정 select는 빈 `선택하세요`가 기본이고 네 profile을 제공한다. 자동차 비용 가정은 유종(휘발유 기본), 연비(10.0km/L 기본), 예상 주차비(0원 기본)를 명시적으로 보여주며 사용자가 바꿀 수 있게 한다.

- [ ] **Step 5: 경로 카드·정렬·여비 상태 UI를 구현한다.**

`app.js`는 response의 `best` ID를 route ID와 조인해 같은 경로에 여러 배지를 붙인다. 정렬은 API 원본을 변경하지 않고 복사본으로 시간·거리·비용 탭을 만든다. 비용 미상은 숫자 0으로 보이지 않게 `비용 정보 없음`, 부분 실패는 해당 수단 카드에 경고를 표시한다. `mobilityCost`는 `예상 이동비`, `allowance`는 `관내출장 예상 지급액` 제목을 사용한다.

- [ ] **Step 6: Kakao 지도 loader와 polyline lifecycle을 구현한다.**

`kakao-map.js`는 bootstrap의 JavaScript key로 SDK script를 한 번만 load하고 등록 도메인 오류를 사용자 메시지로 바꾼다. 기관·출장지 마커, 모든 후보 polyline, 선택 경로 강조, bounds 맞춤을 제공한다. 서울 경계와 외곽 지원영역은 사용자가 켜는 보조 layer로 그리고 `classificationPath`는 점선 스타일로 일반 후보와 구분한다. 새 계산 전 기존 marker·polyline·polygon을 모두 `setMap(null)`로 제거해 누적하지 않는다.

- [ ] **Step 7: 반응형 스타일과 법적 경고를 구현한다.**

desktop은 입력/결과와 지도를 40/60으로, 768px 미만은 입력→결과→접이식 지도 순서로 배치한다. 색상만으로 route를 구분하지 않고 badge text와 선 패턴을 함께 사용한다. 항상 `기관 위치는 경로 기준일 뿐 적용 법규를 정하지 않습니다`와 `예상액이며 지급 확정액이 아닙니다`를 표시한다.

- [ ] **Step 8: E2E와 서버 회귀 테스트를 통과시킨다.**

```bash
pnpm --dir apps/travel-map test:e2e
uv run --project apps/travel-map pytest apps/travel-map/tests -q
```

Expected: 공립·사립 기관 선택, 신분 확인/미확인, 복수경로, 대표 3개, 지도 선택, 모바일, 부분장애 시나리오가 PASS한다.

- [ ] **Step 9: UI만 커밋한다.**

```bash
git add apps/travel-map/app/static apps/travel-map/app/main.py apps/travel-map/e2e apps/travel-map/playwright.config.ts apps/travel-map/package.json apps/travel-map/pnpm-lock.yaml
git commit -m "feat: add public travel map interface"
```

## Task 9: Docker·운영 문서·live smoke·출시 게이트

**Files:**

- Modify: `apps/travel-map/Dockerfile`
- Create: `apps/travel-map/README.md`
- Create: `apps/travel-map/scripts/smoke-live.py`
- Create: `apps/travel-map/tests/test_release.py`
- Create: `apps/travel-map/.dockerignore`

**Interfaces:**

- Consumes: 완성된 앱, 승인 기관 snapshot, geodata/rule manifests, 모든 provider.
- Produces: 재현 가능한 image와 공개 배포 전 단일 release 명령.

- [ ] **Step 1: release artifact RED 테스트를 작성한다.**

```python
# apps/travel-map/tests/test_release.py
from pathlib import Path

from app.institutions.snapshot import verify_snapshot
from app.policy.coverage import CoverageService


def test_release_resources_are_present_and_verified() -> None:
    root = Path("apps/travel-map/resources")
    snapshot = verify_snapshot(root / "institution-snapshots")
    assert snapshot.manifest.approved is True
    assert snapshot.manifest.quarantined_count >= 0
    coverage = CoverageService.from_resources(root / "geodata")
    assert coverage.manifest.source_sha256


def test_container_does_not_copy_env_or_source_payloads() -> None:
    dockerignore = Path("apps/travel-map/.dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore
    assert "artifacts/" in dockerignore
    assert "resources/geodata/source/" in dockerignore
```

- [ ] **Step 2: RED를 확인한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests/test_release.py -q
```

Expected: release 문서·dockerignore 또는 승인 manifest 검사가 없어 실패한다.

- [ ] **Step 3: production Docker image를 완성한다.**

builder는 `uv.lock`으로 dependency를 설치하고 runtime은 app, 정적 파일, rules, 정규화 geodata, 현재 승인 기관 snapshot만 복사한다. `.env`, 원본 provider payload, geodata source, tests, e2e, `.git`은 image에 넣지 않는다. 비root UID로 실행하고 `/healthz` healthcheck를 둔다.

- [ ] **Step 4: live smoke를 구현한다.**

`smoke-live.py`는 `TRAVEL_MAP_LIVE_SMOKE=1` 없이는 exit 2하고, 키가 있을 때 다음 3건만 실행한다.

1. 서울 공립학교→서울시청: LOCAL, 세 수단 중 2개 이상 성공, 대표 route ID 존재.
2. 서울 사립학교→서울 목적지 + `NONPUBLIC_OR_UNKNOWN`: route 존재, allowance 금액 없음.
3. 서울 기관→지원영역 밖: provider 추가호출 없이 OUT_OF_COVERAGE.

출력은 기관 ID, 목적지 이름 대신 case ID, provider 상태, route 수, 판정, latency만 남긴다.

- [ ] **Step 5: README에 로컬·동기화·키·배포 절차를 기록한다.**

README는 카카오 앱 도메인 등록, JavaScript/REST 키 분리, 서울·NEIS·유치원 키, snapshot 갱신, offline tests, live smoke, Docker build/run, quota 503 대응, 목적지 로그 금지, 규칙 근거 링크를 실제 명령과 함께 기록한다.

- [ ] **Step 6: 전체 release gate를 실행한다.**

```bash
uv sync --project apps/travel-map --frozen --dev
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
docker build -t seoul-education-travel-map:0.1.0 apps/travel-map
```

Expected: offline test·lint·typecheck·E2E·image build가 모두 성공한다.

- [ ] **Step 7: opt-in live smoke와 수동 표본 검토를 실행한다.**

```bash
TRAVEL_MAP_LIVE_SMOKE=1 uv run --project apps/travel-map python apps/travel-map/scripts/smoke-live.py
```

25개 자치구·기관유형·설립구분을 층화한 출발지/목적지 30쌍을 검토표로 확인한다. 주소·정문 좌표, 복수 경로, 12km 경계 인접 왕복거리, 이동비/여비 분리, 출처·조회시각을 모두 확인해야 배포 승인으로 기록한다.

- [ ] **Step 8: release 파일만 커밋한다.**

```bash
git add apps/travel-map/Dockerfile apps/travel-map/.dockerignore apps/travel-map/README.md apps/travel-map/scripts/smoke-live.py apps/travel-map/tests/test_release.py
git commit -m "release: verify public travel map MVP"
```

## Task 10: 단계 A 종료 검토와 단계 B·C 연결점 고정

**Files:**

- Modify: `apps/travel-map/README.md`
- Test: `apps/travel-map/tests/routing/test_bootstrap.py`

**Interfaces:**

- Consumes: 단계 A 전체 release gate.
- Produces: 단계 B 자동차 엔진과 단계 C 도보 엔진이 각각 독립적으로 수정할 provider chain 계약과 이를 조립하는 불변 registry 계약.

- [ ] **Step 1: registry 확장 회귀 테스트를 작성한다.**

```python
from app.routing.bootstrap import build_route_providers
from app.routing.models import TravelMode
from tests.routing.fakes import FakeProvider, result_with, route


def test_stage_a_provider_order_is_explicit(settings) -> None:
    providers = build_route_providers(settings)
    assert [p.name for p in providers[TravelMode.TRANSIT]] == ["SEOUL_TRANSIT", "KAKAO_TRANSIT"]
    assert [p.name for p in providers[TravelMode.CAR]] == ["KAKAO_CAR"]
    assert [p.name for p in providers[TravelMode.WALK]] == ["KAKAO_WALK"]


def test_car_and_walk_extension_points_are_independent(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.routing.bootstrap.build_car_provider_chain",
        lambda _settings: (
            FakeProvider(
                "PUBLIC_CAR",
                result_with(route("public-car", 600, 4_000, 1_000)),
            ),
        ),
    )
    providers = build_route_providers(settings)
    assert [p.name for p in providers[TravelMode.CAR]] == ["PUBLIC_CAR"]
    assert [p.name for p in providers[TravelMode.WALK]] == ["KAKAO_WALK"]
```

- [ ] **Step 2: 전체 검증을 다시 실행한다.**

```bash
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map test:e2e
git status --short
```

Expected: 모든 검증이 성공하고 계획에 없는 파일 변경이 없다.

- [ ] **Step 3: 후속 계획과 provider 승격 조건을 README에 연결한다.**

README에 `docs/superpowers/plans/2026-08-10-seoul-public-road-routing-engine.md`와 `docs/superpowers/plans/2026-08-10-seoul-public-walk-routing-engine.md`를 연결한다. 단계 B는 `build_car_provider_chain()`만, 단계 C는 `build_walk_provider_chain()`만 수정하며 상대 수단의 chain 회귀 테스트를 반드시 유지한다. 공공 provider는 골드 경로 검증, 누락 감지, 성능·장애 fallback 검증을 통과하기 전까지 Kakao 앞의 primary로 승격하지 않는다고 명시한다.

- [ ] **Step 4: 단계 A 종료 문서를 커밋한다.**

```bash
git add apps/travel-map/README.md apps/travel-map/tests/routing/test_bootstrap.py
git commit -m "docs: freeze travel provider extension points"
```
