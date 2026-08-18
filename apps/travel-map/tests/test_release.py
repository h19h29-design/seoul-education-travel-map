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
