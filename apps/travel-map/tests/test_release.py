import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.contracts import TripPreviewResponse
from app.institutions.snapshot import verify_snapshot
from app.institutions.sync import (
    approve_candidate_snapshot,
    bind_school_count_population_profile,
    build_candidate_review_packet,
    build_candidate_snapshot,
    reconcile_selectable_school_counts,
)
from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.policy.rules import RuleRepository
from tests.institutions.population_fixtures import (
    REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    reviewed_production_fixture,
)

ROOT = Path("apps/travel-map")
SMOKE = ROOT / "scripts/smoke-live.py"
PREPARE_CONTEXT = ROOT / "scripts/prepare-release-context.py"
SYNC = ROOT / "scripts/sync-institutions.py"
FIXTURE_SNAPSHOT = ROOT / "tests/fixtures/institutions/snapshot"


# Production break caught: a release check treating the intentionally absent
# production snapshot as an empty-but-deployable institution catalog.
def test_release_preflight_blocks_when_the_production_snapshot_is_absent(
    tmp_path: Path,
) -> None:
    completed = _run_smoke(
        {
            "TRAVEL_MAP_LIVE_SMOKE": "1",
            "KAKAO_REST_API_KEY": "test-rest",
            "SEOUL_TRANSIT_SERVICE_KEY": "test-transit",
            "OPINET_CERT_KEY": "test-opinet",
        },
        smoke=_isolated_smoke_without_snapshot(tmp_path),
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report == {"status": "BLOCKED_MISSING_APPROVED_SNAPSHOT"}


# Production break caught: release verification claiming success from a synthetic
# test fixture copied into the production resource location.
def test_verified_resource_success_uses_the_existing_test_fixture_only() -> None:
    snapshot = verify_snapshot(FIXTURE_SNAPSHOT)

    assert snapshot.manifest.approved is True
    assert snapshot.manifest.approved_by_role == "TEST_FIXTURE_REVIEWER"
    coverage = CoverageService.from_geojson(
        seoul_path=ROOT / "resources/geodata/seoul.geojson",
        buffer_distance_m=12_000,
    )
    assert coverage is not None


# Production break caught: the schema-level TEST_NEIS exception escaping into a
# Docker release merely because its synthetic snapshot is internally consistent.
def test_release_context_rejects_test_fixture_population_exception(
    tmp_path: Path,
) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")

    with pytest.raises(ValueError, match="test institution snapshot"):
        module["stage_release_context"](source_root, tmp_path / "context")

    assert not (tmp_path / "context").exists()


# Production break caught: a syntactically valid normalized boundary changed
# after review can pass JSON parsing and still alter the support area at startup.
@pytest.mark.parametrize(
    "relative_path",
    ("seoul.geojson", "seoul-plus-12km.geojson"),
)
def test_release_geodata_preflight_rejects_tampered_normalized_output(
    tmp_path: Path,
    relative_path: str,
) -> None:
    geodata_root = tmp_path / "geodata"
    shutil.copytree(ROOT / "resources/geodata", geodata_root)
    artifact = geodata_root / relative_path
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["features"][0]["properties"]["name"] = "변조된 경계"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        CoverageService.from_resources(geodata_root, verify_source=True)


# Production break caught: release provenance can claim a reviewed SGIS source
# while the local source used to make the normalized outputs has been replaced.
def test_release_geodata_preflight_rejects_tampered_recorded_source(
    tmp_path: Path,
) -> None:
    geodata_root = tmp_path / "geodata"
    shutil.copytree(ROOT / "resources/geodata", geodata_root)
    source = geodata_root / "source/seoul-boundary.geojson"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["_provenance"]["administrativeName"] = "변조된 원본"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="source sha256 mismatch"):
        CoverageService.from_resources(geodata_root, verify_source=True)


# Production break caught: a syntactically valid, materially altered rule file
# can otherwise be accepted because the version index does not bind its bytes.
def test_rule_manifest_rejects_tampered_rule_payload(tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    shutil.copytree(ROOT / "resources/rules", rules_root)
    rule_path = rules_root / "local-travel-2026-07-01.json"
    payload = json.loads(rule_path.read_text(encoding="utf-8"))
    payload["fourHoursOrMoreKrw"] = 20_001
    rule_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="rule sha256 mismatch"):
        RuleRepository.from_directory(rules_root)


# Production break caught: an unpinned rule index can parse a reviewed-looking
# payload but leaves its exact policy bytes unbound during release startup.
def test_production_rule_preflight_requires_a_hash_for_every_rule(
    tmp_path: Path,
) -> None:
    rules_root = tmp_path / "rules"
    shutil.copytree(ROOT / "resources/rules", rules_root)
    index_path = rules_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["rules"][0]["sha256"]
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ValueError, match="rule index must pin every rule sha256"):
        RuleRepository.from_directory(rules_root, require_hashes=True)


# Production break caught: a Docker build context that transfers every prior
# approved snapshot even though the image copies only the one selected today.
def test_release_context_contains_only_the_current_verified_snapshot(
    tmp_path: Path,
) -> None:
    source_root = _copy_release_source(tmp_path)
    snapshots = source_root / "resources/institution-snapshots"
    snapshots.mkdir()
    shutil.copytree(
        FIXTURE_SNAPSHOT / "fixture-001",
        snapshots / "fixture-001",
    )
    (snapshots / "current.json").write_text(
        json.dumps({"snapshotId": "fixture-001"}),
        encoding="utf-8",
    )
    shutil.copytree(snapshots / "fixture-001", snapshots / "historical-001")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    stage_release_context = module["stage_release_context"]
    context_root = tmp_path / "context"
    staged_snapshot_id = stage_release_context(
        source_root,
        context_root,
        allow_test_fixture=True,
    )

    assert staged_snapshot_id == "fixture-001"
    context_snapshots = context_root / "resources/institution-snapshots"
    assert sorted(
        path.relative_to(context_snapshots).as_posix()
        for path in context_snapshots.rglob("*")
    ) == [
        "current.json",
        "fixture-001",
        "fixture-001/institutions.jsonl",
        "fixture-001/manifest.json",
        "fixture-001/sites.jsonl",
    ]
    assert not (context_root / "resources/geodata/source").exists()
    assert not (context_root / "resources/institution-sources").exists()
    assert not (context_root / "tests").exists()
    assert not (context_root / "e2e").exists()


def test_release_context_blocks_candidate_until_exact_digest_approval(
    tmp_path: Path,
) -> None:
    source_root = _copy_release_source(tmp_path)
    snapshot_root = source_root / "resources/institution-snapshots"
    snapshot_root.mkdir()
    profile, benchmark, records, provenance = reviewed_production_fixture()
    bound = bind_school_count_population_profile(provenance, profile=profile)
    reconciliation = reconcile_selectable_school_counts(
        tuple(
            record
            for record in records
            if record.source in {"NEIS", "KINDERGARTEN_INFO"}
        ),
        benchmark=benchmark,
        population_profile=profile,
        source_provenance=bound,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    coverage = CoverageService.from_geojson(
        seoul_path=source_root / "resources/geodata/seoul.geojson",
        buffer_distance_m=12_000,
    )
    build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=snapshot_root,
        snapshot_id="release-review-candidate",
        coverage=coverage,
        source_provenance=bound,
        school_count_reconciliation=reconciliation,
    )
    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    blocked_context = tmp_path / "blocked-context"

    with pytest.raises(ValueError, match="snapshot pointer|current"):
        module["stage_release_context"](source_root, blocked_context)

    assert not blocked_context.exists()
    packet = build_candidate_review_packet(
        snapshot_id="release-review-candidate",
        snapshot_root=snapshot_root,
        coverage=coverage,
    )
    digest = packet["reviewDigest"]
    assert isinstance(digest, str)
    assert packet["unclassifiedSchoolKindCounts"] == dict(
        REVIEWED_NEIS_UNCLASSIFIED_POLICY.counts
    )
    assert (
        packet["unclassifiedSchoolPolicySha256"]
        == REVIEWED_NEIS_UNCLASSIFIED_POLICY.sha256
    )
    approve_candidate_snapshot(
        snapshot_id="release-review-candidate",
        review_digest=digest,
        reviewer_role="data-steward",
        snapshot_root=snapshot_root,
        coverage=coverage,
    )

    staged_id = module["stage_release_context"](
        source_root,
        tmp_path / "approved-context",
    )

    assert staged_id == "release-review-candidate"


# Production break caught: a validly reviewed NEIS/KGI-only snapshot can otherwise
# stage for production while silently omitting all reviewed SEN institutions.
def test_release_context_rejects_approved_snapshot_missing_a_production_source(
    tmp_path: Path,
) -> None:
    source_root = _copy_release_source(tmp_path)
    snapshot_root = source_root / "resources/institution-snapshots"
    snapshot_root.mkdir()
    profile, benchmark, records, provenance = reviewed_production_fixture()
    bound = bind_school_count_population_profile(provenance, profile=profile)
    reconciliation = reconcile_selectable_school_counts(
        tuple(
            record
            for record in records
            if record.source in {"NEIS", "KINDERGARTEN_INFO"}
        ),
        benchmark=benchmark,
        population_profile=profile,
        source_provenance=bound,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    coverage = CoverageService.from_geojson(
        seoul_path=source_root / "resources/geodata/seoul.geojson",
        buffer_distance_m=12_000,
    )
    candidate = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=snapshot_root,
        snapshot_id="release-missing-sen",
        coverage=coverage,
        source_provenance=bound,
        school_count_reconciliation=reconciliation,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=snapshot_root,
        coverage=coverage,
    )
    approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=packet["reviewDigest"],
        reviewer_role="data-steward",
        snapshot_root=snapshot_root,
        coverage=coverage,
    )
    manifest_path = snapshot_root / candidate.snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"] = [
        source
        for source in manifest["sources"]
        if source["source"] != "SEN_REVIEWED_CSV"
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")

    with pytest.raises(ValueError, match="production source set"):
        module["stage_release_context"](
            source_root,
            tmp_path / "missing-source-context",
        )

    assert not (tmp_path / "missing-source-context").exists()


def test_release_context_omits_unlisted_files_from_selected_snapshot_and_rules(
    tmp_path: Path,
) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    snapshot = source_root / "resources/institution-snapshots/fixture-001"
    (snapshot / "secrets.txt").write_text("not for Docker", encoding="utf-8")
    (snapshot / "review-notes.md").write_text("not for Docker", encoding="utf-8")
    rules = source_root / "resources/rules"
    (rules / "unlisted-rule.json").write_text("not for Docker", encoding="utf-8")
    (rules / "review-notes.md").write_text("not for Docker", encoding="utf-8")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    context_root = tmp_path / "context"
    module["stage_release_context"](
        source_root,
        context_root,
        allow_test_fixture=True,
    )

    staged_snapshot = context_root / "resources/institution-snapshots/fixture-001"
    staged_rules = context_root / "resources/rules"
    assert sorted(path.name for path in staged_snapshot.iterdir()) == [
        "institutions.jsonl",
        "manifest.json",
        "sites.jsonl",
    ]
    assert sorted(path.name for path in staged_rules.iterdir()) == [
        "index.json",
        "local-travel-2026-07-01.json",
    ]


# Production break caught: a broad application-tree copy leaks nested dotenvs,
# raw inputs, test assets, caches, or Git data into a Docker build context.
def test_release_context_allowlists_only_production_app_files(tmp_path: Path) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    app_root = source_root / "app"
    malicious_paths = (
        ".env",
        "nested/.env",
        "nested/.env.production",
        "raw/provider-response.json",
        "source/archive.geojson",
        "artifacts/report.json",
        "tests/test_hidden.py",
        "e2e/trace.zip",
        "__pycache__/main.cpython-312.pyc",
        ".git/config",
    )
    for relative_path in malicious_paths:
        path = app_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not for Docker", encoding="utf-8")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    context_root = tmp_path / "context"
    module["stage_release_context"](
        source_root,
        context_root,
        allow_test_fixture=True,
    )

    staged_app = context_root / "app"
    assert (context_root / ".dockerignore").is_file()
    assert (staged_app / "main.py").is_file()
    assert (staged_app / "static/index.html").is_file()
    assert all(
        not (staged_app / relative_path).exists() for relative_path in malicious_paths
    )
    assert all(
        path.suffix == ".py" or path.relative_to(staged_app).parts[0] == "static"
        for path in staged_app.rglob("*")
        if path.is_file()
    )


# Production break caught: suffix-only application allowlisting can stage a
# hidden Python module or static payload, including ones beneath a hidden path.
def test_release_context_omits_every_hidden_application_path(tmp_path: Path) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    app_root = source_root / "app"
    hidden_paths = (
        ".secret.py",
        ".private/module.py",
        "static/.secret.js",
        "static/.private/bundle.css",
    )
    for relative_path in hidden_paths:
        path = app_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not for Docker", encoding="utf-8")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    context_root = tmp_path / "context"
    module["stage_release_context"](
        source_root,
        context_root,
        allow_test_fixture=True,
    )

    staged_app = context_root / "app"
    assert (staged_app / "main.py").is_file()
    assert (staged_app / "static/index.html").is_file()
    assert all(
        not (staged_app / relative_path).exists() for relative_path in hidden_paths
    )
    assert all(
        not any(part.startswith(".") for part in path.relative_to(staged_app).parts)
        for path in staged_app.rglob("*")
    )


# Production break caught: a symlink in a copied application path can resolve
# outside the reviewed source tree while bypassing the context allowlist.
def test_release_context_rejects_an_application_symlink(tmp_path: Path) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    link = source_root / "app/nested-link.py"
    link.symlink_to(source_root / "app/main.py")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")

    with pytest.raises(ValueError, match="symlink"):
        module["stage_release_context"](
            source_root,
            tmp_path / "context",
            allow_test_fixture=True,
        )


# Production break caught: enabling a billed live check by accident or allowing
# a credential/snapshot error to disclose a secret or institution identifier.
def test_live_smoke_refuses_unapproved_execution_with_a_safe_report() -> None:
    completed = _run_smoke({})

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report == {"status": "REFUSED_NOT_OPTED_IN"}
    assert "KAKAO" not in completed.stdout
    assert "institution" not in completed.stdout.lower()


# Production break caught: entering a live provider path with one or more runtime
# credentials absent, which could turn an operator mistake into partial traffic.
def test_live_smoke_blocks_missing_runtime_credentials_without_naming_them() -> None:
    completed = _run_smoke({"TRAVEL_MAP_LIVE_SMOKE": "1"})

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {"status": "BLOCKED_MISSING_CREDENTIALS"}
    assert "KAKAO" not in completed.stdout
    assert "OPINET" not in completed.stdout


# Production break caught: following the documented local .env workflow from
# the repository root leaves smoke credentials unread and falsely blocks it.
def test_live_smoke_reads_an_explicit_env_file_without_echoing_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "local.env"
    secret = "never-print-this-runtime-secret"
    env_file.write_text(
        "\n".join(
            (
                f"KAKAO_REST_API_KEY={secret}",
                "SEOUL_TRANSIT_SERVICE_KEY=transit-secret",
                "OPINET_CERT_KEY=opinet-secret",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _run_smoke(
        {"TRAVEL_MAP_LIVE_SMOKE": "1"},
        arguments=("--env-file", str(env_file)),
        smoke=_isolated_smoke_without_snapshot(tmp_path),
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "status": "BLOCKED_MISSING_APPROVED_SNAPSHOT"
    }
    assert secret not in completed.stdout + completed.stderr


# Production break caught: the sync command's documented env file is ignored,
# causing a present NEIS key to be reported missing or printed in a diagnostic.
def test_sync_reads_an_explicit_env_file_without_echoing_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "sync.env"
    secret = "never-print-this-sync-secret"
    env_file.write_text(f"NEIS_API_KEY={secret}\n", encoding="utf-8")
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "apps/travel-map"}

    completed = subprocess.run(
        [
            sys.executable,
            str(SYNC),
            "--env-file",
            str(env_file),
        ],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "NEIS_API_KEY" not in completed.stderr
    assert "KINDERGARTEN_API_KEY" in completed.stderr
    assert "KAKAO_REST_API_KEY" in completed.stderr
    assert secret not in completed.stdout + completed.stderr


# Production break caught: a smoke report retaining route IDs, destination labels,
# coordinates, or allowance amounts instead of the narrowly approved telemetry.
def test_live_case_report_only_emits_approved_operational_fields() -> None:
    module = runpy.run_path(str(SMOKE), run_name="release_smoke_test")
    report_case = module["_case_report"]
    response = TripPreviewResponse.model_validate(
        _trip_response_payload(
            routes=[
                {
                    "id": "sensitive-route-id",
                    "mode": "CAR",
                    "durationSeconds": 100,
                    "distanceMeters": 1000,
                    "mobilityCostKrw": 200,
                    "costStatus": "KNOWN",
                    "costBreakdown": None,
                    "geometry": [
                        {"latitude": 37.55, "longitude": 126.98},
                        {"latitude": 37.56, "longitude": 126.99},
                    ],
                    "source": "KAKAO_CAR",
                    "sourceAsOf": "2026-08-10T00:00:00Z",
                    "warnings": [],
                }
            ],
            warnings=[],
        )
    )

    report = report_case("NONPUBLIC", response, latency_ms=12)

    assert report == {
        "caseId": "NONPUBLIC",
        "providerStatus": "ROUTES_AVAILABLE",
        "routeCount": 1,
        "decision": "LOCAL",
        "latencyMs": 12,
    }
    assert "sensitive" not in json.dumps(report)


# Production break caught: reporting a generic no-route state after all route
# providers failed, which hides an upstream outage from release review.
@pytest.mark.parametrize(
    ("warning", "expected_status"),
    (
        ("UPSTREAM_UNAVAILABLE", "UPSTREAM_UNAVAILABLE"),
        ("UPSTREAM_RATE_LIMIT", "UPSTREAM_RATE_LIMITED"),
        ("UPSTREAM_REJECTED", "UPSTREAM_REJECTED"),
        ("UPSTREAM_TIMEOUT", "UPSTREAM_TIMEOUT"),
        ("UPSTREAM_ERROR", "UPSTREAM_ERROR"),
        ("SCHEMA_MISMATCH", "RESPONSE_SCHEMA_MISMATCH"),
        ("RESPONSE_TOO_LARGE", "RESPONSE_TOO_LARGE"),
        ("RESPONSE_LIMIT_EXCEEDED", "RESPONSE_LIMIT_EXCEEDED"),
        ("INVALID_PROVIDER_RESULT", "INVALID_PROVIDER_RESPONSE"),
    ),
)
def test_live_case_report_maps_provider_failures_to_safe_statuses(
    warning: str,
    expected_status: str,
) -> None:
    module = runpy.run_path(str(SMOKE), run_name="release_smoke_test")
    report_case = module["_case_report"]
    response = TripPreviewResponse.model_validate(
        _trip_response_payload(routes=[], warnings=[warning])
    )

    assert report_case("PUBLIC_LOCAL", response, latency_ms=12) == {
        "caseId": "PUBLIC_LOCAL",
        "providerStatus": expected_status,
        "routeCount": 0,
        "decision": "LOCAL",
        "latencyMs": 12,
    }


def _release_source_with_current_snapshot(tmp_path: Path) -> Path:
    source_root = _copy_release_source(tmp_path)
    snapshots = source_root / "resources/institution-snapshots"
    snapshots.mkdir()
    shutil.copytree(
        FIXTURE_SNAPSHOT / "fixture-001",
        snapshots / "fixture-001",
    )
    (snapshots / "current.json").write_text(
        json.dumps({"snapshotId": "fixture-001"}),
        encoding="utf-8",
    )
    return source_root


def _copy_release_source(tmp_path: Path) -> Path:
    """Copy tracked release inputs without the ignored live snapshot runtime."""

    source_root = tmp_path / "travel-map"
    shutil.copytree(
        ROOT,
        source_root,
        ignore=shutil.ignore_patterns("institution-snapshots"),
    )
    return source_root


# Production break caught: using a closer headquarters, library, or other
# foundation-matched institution as a smoke origin in a broader snapshot.
def test_live_smoke_selects_a_seoul_active_school_not_any_foundation_match() -> None:
    module = runpy.run_path(str(SMOKE), run_name="release_smoke_test")
    select_origin = module["_nearest_active_school_site"]

    class BroaderStore:
        def search(self, **filters: object) -> tuple[SimpleNamespace, ...]:
            if filters["institution_type"] == "ELEMENTARY_SCHOOL":
                return (SimpleNamespace(site_id="test:school"),)
            return ()

        def require_site(self, site_id: str) -> SimpleNamespace:
            if site_id == "test:school":
                return SimpleNamespace(
                    routing_anchor_latitude=37.57,
                    routing_anchor_longitude=126.98,
                )
            raise AssertionError("non-school institution was selected")

    dependencies = SimpleNamespace(
        institutions=BroaderStore(),
        coverage=SimpleNamespace(classify=lambda _coordinate: CoverageState.SEOUL),
    )

    assert select_origin(dependencies, "PUBLIC") == "test:school"


def _trip_response_payload(
    *,
    routes: list[dict[str, object]],
    warnings: list[str],
) -> dict[str, object]:
    route_id = "sensitive-route-id"
    return {
        "coverage": {"status": "SEOUL"},
        "origin": {
            "siteId": "test-neis:B10:private-origin",
            "name": "sensitive origin",
            "address": "sensitive origin address",
            "coordinate": {"latitude": 37.55, "longitude": 126.98},
        },
        "institutionSnapshotId": "fixture-001",
        "tripPattern": "OUTBOUND_ONLY_END_AFTER_SCHEDULE",
        "routeLegs": [
            {
                "direction": "OUTBOUND",
                "departAt": "2026-08-10T09:00:00Z",
                "routes": routes,
                "best": {
                    "fastestRouteId": route_id if routes else None,
                    "shortestRouteId": route_id if routes else None,
                    "cheapestRouteId": route_id if routes else None,
                },
                "mobilityCost": {
                    "status": "KNOWN" if routes else "UNKNOWN",
                    "amountKrw": 200 if routes else None,
                },
            }
        ],
        "policyScope": "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED",
        "classification": "LOCAL",
        "classificationDistanceMeters": 1200,
        "classificationDistanceBasis": "ONE_WAY_LOWER_BOUND",
        "classificationPath": None,
        "mobilityCost": {
            "status": "KNOWN" if routes else "UNKNOWN",
            "amountKrw": 200 if routes else None,
        },
        "allowance": {"status": "REVIEW_REQUIRED", "amountKrw": None},
        "ruleSetId": "fixture-rule",
        "effectiveFrom": "2026-08-01",
        "sourceRefs": [],
        "warnings": warnings,
    }


# Production break caught: a container context including credentials, raw inputs,
# or test-only resources even when Docker is unavailable for an integration build.
def test_release_container_artifacts_exclude_non_runtime_payloads() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for forbidden in (
        ".env",
        ".git",
        "**/._*",
        "**/.DS_Store",
        "tests/",
        "e2e/",
        "resources/geodata/source/",
        "resources/institution-sources/",
        "artifacts/",
    ):
        assert forbidden in dockerignore
    assert "USER appuser" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "resources/rules" in dockerfile
    assert "resources/geodata/seoul.geojson" in dockerfile
    assert "resources/geodata/seoul-plus-12km.geojson" in dockerfile
    assert "resources/institution-snapshots" in dockerfile


def test_nas_runtime_has_one_writable_mount_and_hardened_migration() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "install -d -m 0700 -o appuser -g appuser /data" in dockerfile
    assert "VOLUME" not in dockerfile
    assert "umask 077; exec uvicorn" in dockerfile
    assert "--no-proxy-headers" in dockerfile

    compose = (ROOT / "deploy/nas/compose.example.yml").read_text(encoding="utf-8")
    migration = (ROOT / "deploy/nas/migrate-user-database.sh").read_text(
        encoding="utf-8"
    )
    assert (
        "image: ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
        "${TRAVEL_MAP_MANIFEST_DIGEST:"
    ) in compose
    assert "/volume2/docker-1/seoul-education-travel-map/data:/data:rw" in compose
    assert "read_only: true" in compose
    assert 'user: "10001:10001"' in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
    ):
        assert flag in migration
    assert "runtime.env" not in migration
    assert os.access(ROOT / "deploy/nas/migrate-user-database.sh", os.X_OK)


def test_nas_compose_is_fixed_to_the_private_data_mount_and_reviewed_image() -> None:
    compose = (ROOT / "deploy/nas/compose.example.yml").read_text(encoding="utf-8")

    assert "image: ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" in compose
    assert "/volume1/docker/seoul-education-travel-map/runtime.env" in compose
    assert compose.count("/volume2/") == 1
    assert "/volume2/docker-1/seoul-education-travel-map/data:/data:rw" in compose
    assert "init: true" in compose
    assert "restart: unless-stopped" in compose
    assert "read_only: true" in compose
    assert 'user: "10001:10001"' in compose
    assert '- "127.0.0.1:18080:8080"' in compose
    assert "max-size: 10m" in compose
    assert 'max-file: "5"' in compose
    assert "no-new-privileges:true" in compose
    assert "size=16m,mode=0700,uid=10001,gid=10001" in compose
    assert "travel.h19h19.com" not in compose


def test_migration_script_rejects_injected_or_noncanonical_image_references() -> None:
    migration = ROOT / "deploy/nas/migrate-user-database.sh"
    invalid_references = (
        "--network=host",
        "-ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64,
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 63,
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "A" * 64,
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
        + "a" * 64
        + ";not-a-command",
        "ghcr.io/other/repository@sha256:" + "a" * 64,
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
        + "a" * 64
        + "@sha256:"
        + "b" * 64,
    )
    for image in invalid_references:
        completed = subprocess.run(
            [str(migration), image],
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr in {
            "BLOCKED_INVALID_IMAGE_DIGEST\n",
            "BLOCKED_INVALID_IMAGE_REPOSITORY\n",
        }


def test_migration_script_uses_a_fixed_quoted_private_path_and_mode_checks() -> None:
    migration = (ROOT / "deploy/nas/migrate-user-database.sh").read_text(
        encoding="utf-8"
    )

    assert "set -eu" in migration
    assert "data_dir=/volume2/docker-1/seoul-education-travel-map/data" in migration
    assert 'CDPATH= cd -- "$data_dir" && pwd -P' in migration
    assert "10001:10001:700" in migration
    assert '--mount "type=bind,src=$data_dir,dst=/data"' in migration
    assert "umask 077" in migration
    assert "migrate --database /data/travel-map.sqlite3" in migration
    assert "verify --database /data/travel-map.sqlite3" in migration
    assert "eval" not in migration
    assert "source " not in migration
    assert ". runtime.env" not in migration
    assert "docker compose" not in migration


def test_root_gitignore_excludes_all_sqlite_user_database_artifacts() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert "*.sqlite3" in gitignore
    assert "*.sqlite3-wal" in gitignore
    assert "*.sqlite3-shm" in gitignore


def test_backup_assets_and_admin_runbook_define_private_data_boundary() -> None:
    excludes = (
        (ROOT / "deploy/nas/backup-excludes.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    verifier = (ROOT / "deploy/nas/verify-backup-exclusion.sh").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert excludes == [
        "/volume2/docker-1/seoul-education-travel-map/data/",
        "*.sqlite3",
        "*.sqlite3-wal",
        "*.sqlite3-shm",
    ]
    assert "--job-config" in verifier and "--backup-root" in verifier
    assert os.access(ROOT / "deploy/nas/verify-backup-exclusion.sh", os.X_OK)
    assert (
        "/volume2/docker-1/seoul-education-travel-map/data/travel-map.sqlite3" in readme
    )
    assert all(
        name in readme
        for name in (
            "KAKAO_SUBJECT_HMAC_KEY",
            "DATA_ENCRYPTION_KEY_V1",
            "SESSION_HMAC_KEY",
        )
    )
    assert "168시간" in readme and "travel.h19h19.com" in readme
    assert "synology.me" not in readme.lower()


def test_backup_verifier_rejects_malformed_paths_and_option_injection(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    job_config.write_text("not used for malformed arguments\n", encoding="utf-8")
    backup_root.mkdir()

    cases = (
        (
            ("--job-config", "relative-export", "--backup-root", str(backup_root)),
            "BLOCKED_INVALID_JOB_CONFIG_PATH",
        ),
        (
            ("--job-config", str(job_config), "--backup-root", "/"),
            "BLOCKED_INVALID_BACKUP_ROOT",
        ),
        (
            ("--job-config", str(job_config), "--unexpected", str(backup_root)),
            "BLOCKED_INVALID_ARGUMENTS",
        ),
    )
    for arguments, expected_error in cases:
        completed = subprocess.run(
            [str(verifier), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == f"{expected_error}\n"


def test_backup_verifier_requires_the_active_job_to_reference_checked_out_excludes(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    job_config.write_text(
        "ACTIVE_EXCLUSION_FILE="
        "/volume1/docker/seoul-education-travel-map/other-excludes.txt\n",
        encoding="utf-8",
    )
    backup_root.mkdir()

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_MISSING_ACTIVE_EXCLUSION_REFERENCE\n"


def test_backup_verifier_rejects_a_job_reference_with_a_checked_out_path_suffix(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").resolve()
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    job_config.write_text(f"active excludes: {excludes}.disabled\n", encoding="utf-8")
    backup_root.mkdir()

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_MISSING_ACTIVE_EXCLUSION_REFERENCE\n"


def test_backup_verifier_rejects_stale_or_disabled_exact_exclusion_reference(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").resolve()
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    job_config.write_text(
        "# retired ACTIVE_EXCLUSION_FILE=" + str(excludes) + "\n"
        "ACTIVE_EXCLUSION_FILE=/volume1/docker/other/backup-excludes.txt\n",
        encoding="utf-8",
    )
    backup_root.mkdir()

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_MISSING_ACTIVE_EXCLUSION_REFERENCE\n"


def test_backup_verifier_rejects_duplicate_active_exclusion_reference(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").resolve()
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    job_config.write_text(
        f"ACTIVE_EXCLUSION_FILE={excludes}\n" * 2,
        encoding="utf-8",
    )
    backup_root.mkdir()

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_MISSING_ACTIVE_EXCLUSION_REFERENCE\n"


def test_backup_verifier_rejects_another_active_exclusion_field(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").resolve()
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    job_config.write_text(
        f"ACTIVE_EXCLUSION_FILE={excludes}\n"
        "ACTIVE_EXCLUSION_FILE=/volume1/docker/other/backup-excludes.txt\n",
        encoding="utf-8",
    )
    backup_root.mkdir()

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_MISSING_ACTIVE_EXCLUSION_REFERENCE\n"


def test_backup_verifier_rejects_database_artifacts_in_destination(
    tmp_path: Path,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").resolve()
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    nested = backup_root / "incremental" / "data"
    nested.mkdir(parents=True)
    job_config.write_text(
        f"ACTIVE_EXCLUSION_FILE={excludes}\n",
        encoding="utf-8",
    )
    (nested / "travel-map.sqlite3-wal").write_bytes(b"test-only-database-marker")

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_DATABASE_ARTIFACT_IN_BACKUP\n"


@pytest.mark.parametrize(
    "artifact_name",
    ("other.sqlite3", "other.sqlite3-wal", "other.sqlite3-shm"),
)
def test_backup_verifier_rejects_every_sqlite_artifact_matching_exclusion_policy(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    verifier = ROOT / "deploy/nas/verify-backup-exclusion.sh"
    excludes = (ROOT / "deploy/nas/backup-excludes.txt").resolve()
    job_config = tmp_path / "job-export.txt"
    backup_root = tmp_path / "backup-root"
    backup_root.mkdir()
    job_config.write_text(
        f"ACTIVE_EXCLUSION_FILE={excludes}\n",
        encoding="utf-8",
    )
    (backup_root / artifact_name).write_bytes(b"test-only-database-marker")

    completed = subprocess.run(
        [
            str(verifier),
            "--job-config",
            str(job_config),
            "--backup-root",
            str(backup_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_DATABASE_ARTIFACT_IN_BACKUP\n"


def test_release_gate_uses_bounded_helpers_and_real_encrypted_storage() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert "--cap-drop ALL --cap-add CHOWN --cap-add FOWNER" in gate
    assert "--user 10001:10001" in gate
    assert "--network none" in gate and "--read-only" in gate
    assert all(
        name in gate
        for name in (
            "PayloadCipher",
            "UserSettingsRepository",
            "HistoryRepository",
            "ENCRYPTED_STORAGE_SMOKE_OK",
            "BLOCKED_PLAINTEXT_IN_STORAGE",
            "RELEASE_GATE_IMAGE_RECORD",
            "imageId=",
            "gitSha=",
        )
    )
    assert "sudo" not in gate


def test_release_gate_attestation_is_canonical_atomic_and_platform_bound() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert 'case "${NAS_PLATFORM:-}" in' in gate
    assert "linux/amd64|linux/arm64" in gate
    assert 'docker buildx build --platform "$NAS_PLATFORM"' in gate
    assert '--build-arg SNAPSHOT_ID="$snapshot_id"' in gate
    assert '--load --tag "$gate_image"' in gate
    assert "RELEASE_GATE_IMAGE_RECORD" in gate
    assert "gated-image.record" in gate
    assert "os.fsync" in gate and "os.replace" in gate and "0o600" in gate
    assert "imageTag={image_tag}" in gate
    assert "imageId=sha256:" in gate
    assert "platform={platform}" in gate
    assert "gitSha={git_sha}" in gate


def test_release_gate_cleanup_and_storage_probe_are_narrow_and_secret_safe() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert "gate_data=$gate_parent/data" in gate
    assert '[ "$gate_data" = "$gate_parent/data" ]' in gate
    assert "find /data -mindepth 1 -depth -delete" in gate
    assert (
        "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001"
        in gate
    )
    assert "http://127.0.0.1:8080/healthz" in gate
    assert "storage_sentinel=" in gate
    assert 'docker logs "$gate_container"' in gate
    assert 'grep -aF -q -- "$value"' in gate
    assert "BLOCKED_PLAINTEXT_IN_STORAGE" in gate
    assert "rm -rf" not in gate
    assert "sudo" not in gate
    assert "eval " not in gate


def test_release_gate_executes_its_health_and_file_mode_heredocs() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert gate.count("docker exec -i \"$gate_container\" python - <<'PY'") == 2


def test_release_gate_refuses_to_attest_a_dirty_source_tree() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert "git diff --quiet --ignore-submodules --" in gate
    assert "git diff --cached --quiet --ignore-submodules --" in gate
    assert "git ls-files --others --exclude-standard" in gate
    assert "BLOCKED_DIRTY_RELEASE_SOURCE" in gate


def test_release_gate_encrypts_the_sentinel_through_settings_and_history() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert "replace(DEFAULT_USER_SETTINGS, default_origin_site_id=sentinel)" in gate
    assert "assert await settings.get(user_id=user.id) == settings_value" in gate
    assert "destination_address=sentinel" in gate
    assert "BLOCKED_PLAINTEXT_IN_STORAGE" in gate


def test_release_gate_removes_an_uncommitted_attestation_on_interruption() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert "gate_completed=0" in gate
    assert "remove_unfinished_record" in gate
    assert '[ "$gate_completed" -ne 1 ]' in gate


def test_release_gate_treats_a_post_completion_signal_as_interrupted() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")

    assert "interrupted=0" in gate
    assert "interrupted_cleanup()" in gate
    assert "trap cleanup EXIT" in gate
    assert "trap interrupted_cleanup HUP INT TERM" in gate
    assert '[ "$interrupted" -eq 1 ]' in gate


def test_ci_runs_every_warning_strict_release_check() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    publish = Path("apps/travel-map/deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    deploy = Path("apps/travel-map/deploy/nas/deploy-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(workflow.split())

    assert "PYTHONWARNINGS: error" in workflow
    assert "pytest apps/travel-map/tests -q" in normalized
    assert "ruff check apps/travel-map" in normalized
    assert (
        "ruff format --check apps/travel-map/app apps/travel-map/tests "
        "apps/travel-map/scripts" in normalized
    )
    assert "mypy apps/travel-map/app apps/travel-map/scripts" in normalized
    assert "pnpm --dir apps/travel-map test:e2e" in normalized
    assert "ghcr.io/h19h29-design/seoul-education-travel-map" in publish
    assert "docker build" not in publish
    assert "imagetools inspect --format" in publish
    assert "imagetools inspect --raw" in publish
    assert "release-gate.sh" in publish and "RELEASE_GATE_IMAGE_RECORD" in publish
    assert "TRAVEL_MAP_MANIFEST_DIGEST" in deploy
    assert "docker pull" in deploy and "migrate-user-database.sh" in deploy
    assert "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" in deploy
    assert "docker build" not in deploy
    assert os.access(
        Path("apps/travel-map/deploy/nas/publish-reviewed-image.sh"), os.X_OK
    )
    assert os.access(
        Path("apps/travel-map/deploy/nas/deploy-reviewed-image.sh"), os.X_OK
    )


@pytest.mark.parametrize(
    "reference",
    (
        "latest",
        "ghcr.io/h19h29-design/seoul-education-travel-map:latest",
        "docker.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64,
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "A" * 64,
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 63,
        "-ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64,
    ),
)
def test_deploy_wrapper_rejects_tag_other_registry_and_malformed_digest(
    reference: str,
) -> None:
    deploy = ROOT / "deploy/nas/deploy-reviewed-image.sh"

    completed = subprocess.run(
        [str(deploy), reference],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_IMAGE_REFERENCE\n"


def test_reviewed_image_handoff_binds_all_attestation_fields_without_rebuild() -> None:
    publish = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )

    assert '[ "$#" -eq 2 ]' in publish
    assert "RELEASE_GATE_IMAGE_RECORD=$record" in publish
    assert '"$travel_root/scripts/release-gate.sh" >&2' in publish
    assert "splitlines(keepends=True)" in publish
    assert 'set(values) != {"imageTag", "imageId", "platform", "gitSha"}' in publish
    assert "docker image inspect" in publish
    assert "image_id_hex=${image_id#sha256:}" in publish
    assert "publish_tag=$git_sha-sha256-$image_id_hex" in publish
    assert '[ "${#publish_tag}" -le 128 ]' in publish
    assert "tagged=$registry:$publish_tag" in publish
    assert 'docker tag "$image_id" "$tagged"' in publish
    assert "imagetools inspect --format" in publish
    assert "imagetools inspect --raw" in publish
    assert "BLOCKED_REMOTE_IMAGE_MISMATCH" in publish
    assert "docker build" not in publish


def test_publish_reviewed_image_uses_one_validated_canonical_lock_root() -> None:
    publish = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )

    assert "lock_parent=/tmp/travel-map-publish-locks-$publisher_uid" in publish
    assert "publisher_uid=$(/usr/bin/id -u 2>/dev/null)" in publish
    assert "/usr/bin/python3 -I -S" in publish
    assert '/bin/mkdir -m 0700 "$lock_parent"' in publish
    assert '(umask 077 && /bin/mkdir "$lock_directory")' in publish
    assert '/bin/rmdir "$lock_directory"' in publish
    assert 'validate_private_directory "$lock_parent"' in publish
    assert "details = Path(sys.argv[1]).lstat()" in publish
    assert "not stat.S_ISDIR(details.st_mode)" in publish
    assert "stat.S_IMODE(details.st_mode) != 0o700" in publish
    assert "details.st_uid != os.getuid()" in publish
    assert "${TMPDIR:-/tmp}/travel-map-publish-locks" not in publish


PUBLISH_GIT_SHA = "1" * 40
PUBLISH_REGISTRY = "ghcr.io/h19h29-design/seoul-education-travel-map"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
IN_TOTO_LAYER = "application/vnd.in-toto+json"


def _remote_descriptor(digest: str, media_type: str) -> dict[str, object]:
    return {"mediaType": media_type, "digest": digest, "size": 1024}


def _image_manifest(
    config_digest: str,
    *,
    attestation: bool = False,
) -> dict[str, object]:
    layer_media_type = (
        IN_TOTO_LAYER if attestation else "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    return {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": OCI_CONFIG,
            "digest": config_digest,
            "size": 256,
        },
        "layers": [
            {
                "mediaType": layer_media_type,
                "digest": "sha256:" + ("9" if attestation else "8") * 64,
                "size": 512,
            }
        ],
    }


def _index_manifest(
    runnable_descriptors: list[tuple[str, str]],
    *,
    attestation_digest: str | None = None,
    attestation_link: str | None = None,
) -> dict[str, object]:
    manifests: list[dict[str, object]] = [
        {
            **_remote_descriptor(digest, OCI_MANIFEST),
            "platform": {
                "os": platform.partition("/")[0],
                "architecture": platform.partition("/")[2],
            },
        }
        for digest, platform in runnable_descriptors
    ]
    if attestation_digest is not None:
        if attestation_link is None:
            raise ValueError("an attestation digest requires its runnable link")
        manifests.append(
            {
                **_remote_descriptor(attestation_digest, OCI_MANIFEST),
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.digest": attestation_link,
                    "vnd.docker.reference.type": "attestation-manifest",
                },
            }
        )
    elif attestation_link is not None:
        raise ValueError("an attestation link requires its descriptor")
    return {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests}


def _write_publish_test_double(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

scenario_path = Path(os.environ["FAKE_DOCKER_SCENARIO"])
state_path = Path(os.environ["FAKE_DOCKER_STATE"])
scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
if state_path.exists():
    state = json.loads(state_path.read_text(encoding="utf-8"))
else:
    state = {
        "image_inspects": {},
        "image_configs": [],
        "tag_lookup_attempts": 0,
        "tag_resolutions": 0,
        "immutable_resolutions": {},
        "raw_digests": [],
        "tag_created": False,
        "tag_overwrites": 0,
        "tagged": False,
        "current_tag_id": None,
        "pushed": False,
    }


def persist() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(91)


args = sys.argv[1:]
if len(args) == 6 and args[:2] == ["image", "ls"]:
    if args[2:5] != ["--quiet", "--no-trunc", "--filter"]:
        fail("unexpected local tag preflight")
    reference = args[5].removeprefix("reference=")
    if reference != scenario["tagged_reference"]:
        fail("publisher checked an unexpected local tag")
    if scenario["local_tag_exists"]:
        print(scenario["expected_tag_source"])
    elif state["tagged"]:
        print(state["current_tag_id"])
elif len(args) == 5 and args[:3] == ["image", "inspect", "--format"]:
    reference = args[4]
    sequence = scenario["local_inspects"].get(reference)
    if not sequence:
        fail("unexpected image inspect reference")
    index = state["image_inspects"].get(reference, 0)
    if index >= len(sequence):
        fail("unexpected repeated image inspect")
    state["image_inspects"][reference] = index + 1
    persist()
    inspected = sequence[index]
    print(f"{inspected['id']} {inspected['platform']}")
elif len(args) == 3 and args[0] == "tag":
    if scenario["forbid_tag"]:
        fail("publisher attempted to overwrite an existing tag")
    if args[1] != scenario["expected_tag_source"]:
        fail("publisher did not tag the captured image id")
    if args[2] != scenario["tagged_reference"]:
        fail("publisher used an unexpected destination tag")
    if scenario["local_tag_race"]:
        state["tagged"] = True
        state["current_tag_id"] = "sha256:" + "0" * 64
        state["tag_overwrites"] += 1
        persist()
    state["tag_created"] = True
    state["tagged"] = True
    state["current_tag_id"] = scenario["expected_tag_source"]
    persist()
elif len(args) == 2 and args[0] == "push":
    if scenario["forbid_push"]:
        fail("publisher attempted a forbidden push")
    if args[1] != scenario["tagged_reference"] or not state["tagged"]:
        fail("publisher pushed an unverified tag")
    state["pushed"] = True
    persist()
elif len(args) >= 4 and args[:3] == ["buildx", "imagetools", "inspect"]:
    if args[3] == "--raw" and len(args) == 5:
        reference = args[4]
        if "@" not in reference:
            fail("raw inspection did not use an immutable digest")
        digest = reference.partition("@")[2]
        payload = scenario["raw_by_digest"].get(digest)
        if payload is None:
            fail("unexpected raw digest")
        state["raw_digests"].append(digest)
        persist()
        print(json.dumps(payload, separators=(",", ":")))
    elif len(args) == 6 and args[3:5] == ["--format", "{{json .Image}}"]:
        reference = args[5]
        if "@" not in reference:
            fail("image config inspection did not use an immutable digest")
        digest = reference.partition("@")[2]
        payload = scenario["image_by_digest"].get(digest)
        if payload is None:
            fail("unexpected image config digest")
        state["image_configs"].append(digest)
        persist()
        print(json.dumps(payload, separators=(",", ":")))
    elif (
        len(args) == 6
        and args[3] == "--format"
        and args[4] == "{{json .Manifest}}"
    ):
        reference = args[5]
        if "@" in reference:
            digest = reference.partition("@")[2]
            payload = scenario["immutable_descriptors"].get(digest)
            if payload is None:
                fail("unexpected immutable descriptor")
            count = state["immutable_resolutions"].get(digest, 0)
            state["immutable_resolutions"][digest] = count + 1
        else:
            state["tag_lookup_attempts"] += 1
            lookup_mode = scenario["remote_lookup_mode"]
            if lookup_mode == "missing" and not state["pushed"]:
                persist()
                print(f"ERROR: {reference}: not found", file=sys.stderr)
                raise SystemExit(1)
            if lookup_mode == "race-before-push":
                if state["tag_lookup_attempts"] == 1:
                    persist()
                    print(f"ERROR: {reference}: not found", file=sys.stderr)
                    raise SystemExit(1)
                if not state["tag_created"]:
                    fail("publisher did not recheck the remote tag immediately before push")
            if lookup_mode == "ambiguous":
                persist()
                print("ERROR: request failed: network timeout", file=sys.stderr)
                raise SystemExit(1)
            index = state["tag_resolutions"]
            descriptors = scenario["tag_descriptors"]
            if index >= len(descriptors):
                fail("unexpected repeated tag resolution")
            payload = descriptors[index]
            state["tag_resolutions"] = index + 1
        persist()
        print(json.dumps(payload, separators=(",", ":")))
    else:
        fail("unexpected imagetools inspection")
elif len(args) == 3 and args[:2] == ["image", "rm"]:
    reference = args[2]
    if reference == scenario["tagged_reference"]:
        if scenario["local_tag_exists"] or (
            state["current_tag_id"] is not None
            and state["current_tag_id"] != scenario["expected_tag_source"]
        ):
            fail("publisher removed a pre-existing local tag")
        state["tagged"] = False
        state["current_tag_id"] = None
        persist()
    if reference == scenario["image_tag_reference"] and scenario["expect_success"]:
        required_raw = set(scenario["required_raw_digests"])
        required_images = set(scenario["required_image_digests"])
        if (
            state["tag_created"] != scenario["expect_push"]
            or state["tag_overwrites"] != int(scenario["local_tag_race"])
            or state["pushed"] != scenario["expect_push"]
            or state["tag_lookup_attempts"]
            != scenario["expected_tag_lookup_attempts"]
            or state["tag_resolutions"] != 2
            or state["immutable_resolutions"].get(scenario["root_digest"], 0) != 1
            or not required_raw.issubset(state["raw_digests"])
            or not required_images.issubset(state["image_configs"])
        ):
            fail("publisher skipped a required identity recheck")
else:
    fail("unexpected docker command")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_publish_reviewed_image(
    tmp_path: Path,
    *,
    image_id: str,
    remote_digest: str,
    root_manifest: dict[str, object],
    raw_children: dict[str, dict[str, object]] | None = None,
    image_configs: dict[str, dict[str, object]] | None = None,
    local_tag_ids: tuple[str, ...] | None = None,
    tag_descriptors: list[dict[str, object]] | None = None,
    immutable_descriptor: dict[str, object] | None = None,
    remote_lookup_mode: str = "missing",
    local_tag_exists: bool = False,
    local_tag_race: bool = False,
    ambient_lock_stale: bool = False,
    git_sha: str = PUBLISH_GIT_SHA,
    expect_success: bool = False,
) -> subprocess.CompletedProcess[str]:
    test_root = tmp_path / "travel-map"
    publisher = test_root / "deploy/nas/publish-reviewed-image.sh"
    publisher.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/nas/publish-reviewed-image.sh", publisher)

    release_gate = test_root / "scripts/release-gate.sh"
    release_gate.parent.mkdir(parents=True)
    release_gate.write_text(
        """#!/bin/sh
set -eu
(umask 077 && printf 'imageTag=seoul-education-travel-map:release-gate-%s\\nimageId=%s\\nplatform=%s\\ngitSha=%s\\n' "$EXPECTED_PUBLISH_SHA" "$FAKE_IMAGE_ID" "$NAS_PLATFORM" "$EXPECTED_PUBLISH_SHA" > "$RELEASE_GATE_IMAGE_RECORD")
chmod 0600 "$RELEASE_GATE_IMAGE_RECORD"
""",
        encoding="utf-8",
    )
    release_gate.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    _write_publish_test_double(fake_bin / "docker")

    image_tag = f"seoul-education-travel-map:release-gate-{git_sha}"
    image_id_hex = image_id.removeprefix("sha256:")
    tagged_reference = f"{PUBLISH_REGISTRY}:{git_sha}-sha256-{image_id_hex}"
    platform = "linux/amd64"
    inspect = lambda value: {"id": value, "platform": platform}
    root_media_type = str(root_manifest["mediaType"])
    raw_by_digest = {remote_digest: root_manifest, **(raw_children or {})}
    descriptors = tag_descriptors or [
        _remote_descriptor(remote_digest, root_media_type),
        _remote_descriptor(remote_digest, root_media_type),
    ]
    tag_id_sequence = local_tag_ids or (image_id, image_id)
    scenario = {
        "local_inspects": {
            image_tag: [inspect(value) for value in tag_id_sequence],
            image_id: [inspect(image_id)],
            tagged_reference: [inspect(image_id)],
        },
        "image_tag_reference": image_tag,
        "local_tag_exists": local_tag_exists,
        "local_tag_race": local_tag_race,
        "forbid_tag": local_tag_exists
        or remote_lookup_mode in {"existing", "ambiguous"},
        "forbid_push": remote_lookup_mode != "missing",
        "remote_lookup_mode": remote_lookup_mode,
        "expected_tag_source": image_id,
        "tagged_reference": tagged_reference,
        "tag_descriptors": descriptors,
        "immutable_descriptors": {
            remote_digest: immutable_descriptor
            or _remote_descriptor(remote_digest, root_media_type)
        },
        "raw_by_digest": raw_by_digest,
        "image_by_digest": image_configs or {},
        "root_digest": remote_digest,
        "required_raw_digests": list(raw_by_digest),
        "required_image_digests": list(image_configs or {}),
        "expect_push": remote_lookup_mode == "missing",
        "expected_tag_lookup_attempts": 4 if remote_lookup_mode == "missing" else 2,
        "expect_success": expect_success,
    }
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "EXPECTED_PUBLISH_SHA": git_sha,
            "FAKE_IMAGE_ID": image_id,
            "FAKE_DOCKER_SCENARIO": str(scenario_path),
            "FAKE_DOCKER_STATE": str(tmp_path / "docker-state.json"),
        }
    )
    ambient_lock_directory = None
    if ambient_lock_stale:
        ambient_tmpdir = Path(environment.get("TMPDIR", str(tmp_path)))
        environment["TMPDIR"] = str(ambient_tmpdir)
        ambient_lock_parent = ambient_tmpdir / f"travel-map-publish-locks-{os.getuid()}"
        ambient_lock_parent.mkdir(mode=0o700, exist_ok=True)
        ambient_lock_directory = ambient_lock_parent / git_sha
        ambient_lock_directory.mkdir(mode=0o700)
    try:
        return subprocess.run(
            [str(publisher), git_sha, platform],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        if ambient_lock_directory is not None:
            ambient_lock_directory.rmdir()


def test_publish_reviewed_image_accepts_classic_config_digest_identity(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{remote_digest}\n"
    assert completed.stderr == ""


def test_publish_reviewed_image_accepts_containerd_index_with_linked_attestation(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "c" * 64
    runnable_digest = "sha256:" + "d" * 64
    attestation_digest = "sha256:" + "e" * 64
    runnable_config = "sha256:" + "f" * 64
    attestation_config = "sha256:" + "7" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=image_id,
        root_manifest=_index_manifest(
            [(runnable_digest, "linux/amd64")],
            attestation_digest=attestation_digest,
            attestation_link=runnable_digest,
        ),
        raw_children={
            runnable_digest: _image_manifest(runnable_config),
            attestation_digest: _image_manifest(
                attestation_config,
                attestation=True,
            ),
        },
        image_configs={runnable_digest: {"architecture": "amd64", "os": "linux"}},
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{image_id}\n"
    assert completed.stderr == ""


def test_publish_reviewed_image_accepts_containerd_index_without_attestation(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "c" * 64
    runnable_digest = "sha256:" + "d" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=image_id,
        root_manifest=_index_manifest([(runnable_digest, "linux/amd64")]),
        raw_children={
            runnable_digest: _image_manifest("sha256:" + "f" * 64),
        },
        image_configs={runnable_digest: {"architecture": "amd64", "os": "linux"}},
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{image_id}\n"
    assert completed.stderr == ""


def test_publish_reviewed_image_accepts_identical_remote_without_push(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        remote_lookup_mode="existing",
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{remote_digest}\n"
    assert completed.stderr == ""


def test_publish_reviewed_image_rejects_preexisting_local_publish_tag(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        local_tag_exists=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_PUBLISH_TAG_EXISTS\n"


def test_publish_reviewed_image_uses_content_addressed_tag_when_docker_overwrites_race(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        local_tag_race=True,
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{remote_digest}\n"
    assert completed.stderr == ""


def test_publish_reviewed_image_ignores_ambient_tmpdir_for_lock_identity(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    ambient_only_sha = "3" * 40

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        ambient_lock_stale=True,
        git_sha=ambient_only_sha,
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{remote_digest}\n"
    assert completed.stderr == ""


def test_publish_reviewed_image_blocks_stale_canonical_lock_without_removing_it(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    stale_sha = "2" * 40
    lock_parent = Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")
    lock_parent.mkdir(mode=0o700, exist_ok=True)
    lock_directory = lock_parent / stale_sha
    lock_directory.mkdir(mode=0o700)
    try:
        completed = _run_publish_reviewed_image(
            tmp_path,
            image_id=image_id,
            remote_digest="sha256:" + "b" * 64,
            root_manifest=_image_manifest(image_id),
            git_sha=stale_sha,
        )

        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == "BLOCKED_PUBLISH_LOCKED\n"
        assert lock_directory.is_dir()
    finally:
        lock_directory.rmdir()


def test_publish_reviewed_image_rejects_ambiguous_remote_lookup_before_push(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        remote_lookup_mode="ambiguous",
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_TAG_UNVERIFIED\n"


def test_publish_reviewed_image_rejects_different_existing_remote_before_push(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest("sha256:" + "c" * 64),
        remote_lookup_mode="existing",
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_remote_tag_created_before_push(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest("sha256:" + "c" * 64),
        remote_lookup_mode="race-before-push",
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_child_image_platform_mismatch(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "c" * 64
    runnable_digest = "sha256:" + "d" * 64
    attestation_digest = "sha256:" + "e" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=image_id,
        root_manifest=_index_manifest(
            [(runnable_digest, "linux/amd64")],
            attestation_digest=attestation_digest,
            attestation_link=runnable_digest,
        ),
        raw_children={
            runnable_digest: _image_manifest("sha256:" + "f" * 64),
            attestation_digest: _image_manifest(
                "sha256:" + "7" * 64,
                attestation=True,
            ),
        },
        image_configs={runnable_digest: {"architecture": "arm64", "os": "linux"}},
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


@pytest.mark.parametrize("identity_mode", ("classic-config", "containerd-root"))
def test_publish_reviewed_image_rejects_root_or_config_identity_mismatch(
    tmp_path: Path,
    identity_mode: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    if identity_mode == "classic-config":
        root_manifest = _image_manifest("sha256:" + "c" * 64)
    else:
        runnable_digest = "sha256:" + "d" * 64
        attestation_digest = "sha256:" + "e" * 64
        root_manifest = _index_manifest(
            [(runnable_digest, "linux/amd64")],
            attestation_digest=attestation_digest,
            attestation_link=runnable_digest,
        )

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=root_manifest,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


@pytest.mark.parametrize(
    "runnable_descriptors",
    (
        [("sha256:" + "d" * 64, "linux/arm64")],
        [
            ("sha256:" + "d" * 64, "linux/amd64"),
            ("sha256:" + "6" * 64, "linux/amd64"),
        ],
    ),
    ids=("wrong-platform", "duplicate-platform"),
)
def test_publish_reviewed_image_rejects_wrong_or_duplicate_runnable_platform(
    tmp_path: Path,
    runnable_descriptors: list[tuple[str, str]],
) -> None:
    image_id = "sha256:" + "c" * 64
    attestation_digest = "sha256:" + "e" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=image_id,
        root_manifest=_index_manifest(
            runnable_descriptors,
            attestation_digest=attestation_digest,
            attestation_link=runnable_descriptors[0][0],
        ),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_unlinked_attestation(tmp_path: Path) -> None:
    image_id = "sha256:" + "c" * 64
    runnable_digest = "sha256:" + "d" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=image_id,
        root_manifest=_index_manifest(
            [(runnable_digest, "linux/amd64")],
            attestation_digest="sha256:" + "e" * 64,
            attestation_link="sha256:" + "f" * 64,
        ),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_local_tag_identity_change(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        local_tag_ids=(image_id, "sha256:" + "0" * 64),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_GATE_ATTESTATION\n"


def test_publish_reviewed_image_rejects_remote_tag_identity_change(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    changed_digest = "sha256:" + "c" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        tag_descriptors=[
            _remote_descriptor(remote_digest, OCI_MANIFEST),
            _remote_descriptor(changed_digest, OCI_MANIFEST),
        ],
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_remote_descriptor_size_change(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    changed_size = _remote_descriptor(remote_digest, OCI_MANIFEST)
    changed_size["size"] = 2048

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        tag_descriptors=[
            _remote_descriptor(remote_digest, OCI_MANIFEST),
            changed_size,
        ],
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_remote_descriptor_media_type_change(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        tag_descriptors=[
            _remote_descriptor(remote_digest, OCI_MANIFEST),
            _remote_descriptor(remote_digest, DOCKER_MANIFEST),
        ],
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_rejects_immutable_descriptor_tuple_change(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    changed_immutable = _remote_descriptor(remote_digest, OCI_MANIFEST)
    changed_immutable["size"] = 2048

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        immutable_descriptor=changed_immutable,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_deploy_wrapper_preserves_a_valid_rollback_before_any_mutation() -> None:
    deploy = (ROOT / "deploy/nas/deploy-reviewed-image.sh").read_text(encoding="utf-8")

    assert "validate_image_env()" in deploy
    assert 'validate_image_env "$image_env"' in deploy
    assert 'cp -p "$image_env" "$previous_tmp"' in deploy
    assert 'validate_image_env "$previous_env"' in deploy
    assert '"$migration" "$reference"' in deploy
    assert "TRAVEL_MAP_MANIFEST_DIGEST=%s" in deploy
    assert 'docker compose --env-file "$image_env" -f "$compose" up -d' in deploy
    assert "runtime.env" not in deploy
    assert "docker build" not in deploy
    assert deploy.index('cp -p "$image_env" "$previous_tmp"') < deploy.index(
        '"$migration" "$reference"'
    )


def test_deploy_wrapper_treats_interruption_during_env_swap_as_failure() -> None:
    deploy = (ROOT / "deploy/nas/deploy-reviewed-image.sh").read_text(encoding="utf-8")

    assert "interrupted=0" in deploy
    assert "interrupted_cleanup()" in deploy
    assert "trap cleanup_tmp EXIT" in deploy
    assert "trap interrupted_cleanup HUP INT TERM" in deploy
    assert '[ "$interrupted" -eq 1 ]' in deploy


def _run_smoke(
    extra_environment: dict[str, str],
    *,
    arguments: tuple[str, ...] = (),
    smoke: Path = SMOKE,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for name in (
        "TRAVEL_MAP_LIVE_SMOKE",
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
    ):
        environment.pop(name, None)
    environment.update(extra_environment)
    return subprocess.run(
        [sys.executable, str(smoke.resolve()), *arguments],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _isolated_smoke_without_snapshot(tmp_path: Path) -> Path:
    smoke = tmp_path / "travel-map/scripts/smoke-live.py"
    smoke.parent.mkdir(parents=True)
    shutil.copy2(SMOKE, smoke)
    return smoke
