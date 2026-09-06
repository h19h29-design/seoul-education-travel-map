import hashlib
import json
import os
import pwd
import re
import runpy
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
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
ROLLBACK_PUBLISH = ROOT / "deploy/nas/publish-rollback-baseline.sh"
# The original review commit was restored onto main by this reachable commit;
# both Git objects contain the exact immutable publisher blob asserted below.
ROLLBACK_ORIGINAL_REVIEW_COMMIT = "3d4d25dd249e69aaf8a25e2bcb7267b3f296c0c6"
ROLLBACK_REVIEW_COMMIT = "b550c010da5754154fa11b7ebfecf70e064282c6"
ROLLBACK_PUBLISH_BLOB_SHA = "0227f8a202464dc0874b1ba64c71d504a57ee5cd"
ROLLBACK_SHA = "469c13f5afbc13af3ed9e91eaf43c20825163c6e"
ROLLBACK_IMAGE_ID = "sha256:" + "1" * 64
ROLLBACK_MANIFEST = "sha256:" + "2" * 64
ROLLBACK_PLATFORM_MANIFEST = "sha256:" + "4" * 64
ROLLBACK_CONFIG_DIGEST = "sha256:" + "5" * 64
ROLLBACK_ATTESTATION_MANIFEST = "sha256:" + "6" * 64
ROLLBACK_EXTRA_PLATFORM_MANIFEST = "sha256:" + "7" * 64
ROLLBACK_SECOND_ATTESTATION_MANIFEST = "sha256:" + "8" * 64
ROLLBACK_ATTESTATION_CONFIG = "sha256:" + "9" * 64
ROLLBACK_ATTESTATION_LAYER = "sha256:" + "a" * 64
ROLLBACK_CONTAINER_ID = "b" * 64
ROLLBACK_REGISTRY = "ghcr.io/h19h29-design/seoul-education-travel-map"
ROLLBACK_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
ROLLBACK_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
ROLLBACK_TAG = (
    f"{ROLLBACK_REGISTRY}:rollback-baseline-{ROLLBACK_SHA}-"
    f"{ROLLBACK_IMAGE_ID.removeprefix('sha256:')}"
)
ROLLBACK_LEGACY_TAG = "seoul-education-travel-map:0.1.0"


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
    assert 'run_buildx build --platform "$NAS_PLATFORM"' in gate
    assert (
        "buildx_tool=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin/docker-buildx" in gate
    )
    assert '"$buildx_tool" "$@"' in gate
    assert 'verify_release_docker_socket "$release_docker_host"' in gate
    assert 'docker buildx build --platform "$NAS_PLATFORM"' not in gate
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


def test_release_gate_refuses_dirty_and_hidden_index_state() -> None:
    gate = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")
    normalized = " ".join(gate.split())

    # The subprocess tests in test_release_hardening.py pressure-test these
    # branches with dirty, assume-unchanged, and skip-worktree repositories.
    assert '"status", "--porcelain=v1", "-z"' in normalized
    assert 'git("ls-files", "-v", "-z")' in normalized
    assert 'entry.startswith(b"H ")' in normalized
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


@pytest.mark.parametrize("existing", [False, True])
def test_ci_prepares_owned_rollback_caches_without_removing_contents(
    tmp_path: Path, existing: bool
) -> None:
    """A fresh Linux runner must reach the publisher's mocked release gate."""
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    step = re.search(
        r"(?m)^      - name: Prepare rollback test caches\n"
        r"        run: \|\n((?:          .*\n)+)",
        workflow,
    )
    caches = [tmp_path / ".cache/uv", tmp_path / "Library/Caches/ms-playwright"]
    if existing:
        for cache in caches:
            cache.mkdir(parents=True, mode=0o755)
            (cache / "retained").write_text("keep", encoding="ascii")
    script = textwrap.dedent(step[1]) if step else ""
    # Execute only this CI step against disposable paths, never the real home.
    script = script.replace('"$HOME/', '"' + str(tmp_path) + "/")
    completed = subprocess.run(
        ["/bin/sh", "-eu", "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    for cache in caches:
        assert cache.is_dir(), f"Missing rollback prerequisite: {cache.name}"
        assert cache.resolve() == cache
        assert cache.stat().st_uid == os.getuid()
        assert stat.S_IMODE(cache.stat().st_mode) == 0o700
        if existing:
            assert (cache / "retained").read_text(encoding="ascii") == "keep"
    assert step is not None
    assert step.start() < workflow.index("      - name: Test with warnings as errors")


def test_ci_runs_every_warning_strict_release_check() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    operations = Path("apps/travel-map/README.md").read_text(encoding="utf-8")
    publish = Path("apps/travel-map/deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    deploy = Path("apps/travel-map/deploy/nas/deploy-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(workflow.split())
    python_job_match = re.search(
        r"(?ms)^  python:\n.*?(?=^  [a-zA-Z0-9_-]+:\n|\Z)", workflow
    )
    macos_job_match = re.search(
        r"(?ms)^  macos-release-gate:\n.*?(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )

    assert "python: timeout-minutes: 20" in normalized
    assert python_job_match is not None
    python_job = " ".join(python_job_match.group().split())
    assert "PYTHONWARNINGS: error" in python_job
    assert "actions/checkout@v7.0.1 with: fetch-depth: 0" in python_job
    assert "astral-sh/setup-uv@v10.0.1 id: uv" in python_job
    trusted_bin = 'mkdir -p "$HOME/.local/bin"'
    trusted_uv_install = (
        '/usr/bin/install -m 0755 "${{ steps.uv.outputs.uv-path }}" '
        '"$HOME/.local/bin/uv"'
    )
    trusted_uv_check = '"$HOME/.local/bin/uv" --version'
    trusted_node_capture = "node_path=$(command -v node)"
    trusted_node_install = (
        '/usr/bin/install -m 0755 "$node_path" "$HOME/.local/bin/node"'
    )
    trusted_node_check = '"$HOME/.local/bin/node" --version'
    trusted_path_export = 'export PATH="$HOME/.local/bin:$PATH"'
    trusted_pnpm_install = 'npm install --global --prefix "$HOME/.local" pnpm@10'
    trusted_pnpm_check = '"$HOME/.local/bin/pnpm" --version'
    assert "pnpm/action-setup" not in python_job
    trusted_setup = (
        trusted_bin,
        trusted_uv_install,
        trusted_uv_check,
        trusted_node_capture,
        trusted_node_install,
        trusted_node_check,
        trusted_path_export,
        trusted_pnpm_install,
        trusted_pnpm_check,
    )
    assert all(command in python_job for command in trusted_setup)
    assert [python_job.index(command) for command in trusted_setup] == sorted(
        python_job.index(command) for command in trusted_setup
    )
    assert (
        'pytest apps/travel-map/tests -vv -k "not test_release_gate_" '
        "-o faulthandler_timeout=120" in python_job
    )
    assert macos_job_match is not None
    macos_job = " ".join(macos_job_match.group().split())
    assert "PYTHONWARNINGS: error" in macos_job
    assert "timeout-minutes: 35" in macos_job
    assert "runs-on: macos-latest" in macos_job
    assert (
        'pytest apps/travel-map/tests -vv -k "test_release_gate_" '
        "-o faulthandler_timeout=120" in macos_job
    )
    assert "continue-on-error" not in python_job + macos_job
    assert "macOS release host" in operations
    assert "/usr/bin/sandbox-exec" in operations
    assert "fclonefileat" in operations
    assert "ruff check apps/travel-map" in normalized
    assert (
        "ruff format --check apps/travel-map/app apps/travel-map/tests "
        "apps/travel-map/scripts" in normalized
    )
    assert "mypy apps/travel-map/app apps/travel-map/scripts" in normalized
    assert "pnpm --dir apps/travel-map test:e2e" in normalized
    assert "ghcr.io/h19h29-design/seoul-education-travel-map" in publish
    assert "docker build" not in publish
    assert "docker_tool=$(resolve_publish_tool docker)" in publish
    assert '"$docker_tool" "$@"' in publish
    assert "run_buildx imagetools inspect" in publish
    assert '--raw "$repo_digest"' in publish
    assert "release-gate.sh" not in publish
    assert "RELEASE_GATE_IMAGE_RECORD" not in publish
    assert "travel_root" not in publish
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


@pytest.mark.parametrize(
    ("nas_architecture", "expected_platform"),
    (
        ("amd64", "linux/amd64"),
        ("x86_64", "linux/amd64"),
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
    ),
)
def test_deploy_wrapper_maps_exact_nas_platform_architecture_before_mutation(
    tmp_path: Path,
    nas_architecture: str,
    expected_platform: str,
) -> None:
    reference = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    deploy, environment, events_path, migration_events_path, base = (
        _deploy_wrapper_fixture(
            tmp_path,
            nas_architecture=nas_architecture,
            image_platform=expected_platform,
        )
    )

    completed = subprocess.run(
        [str(deploy), reference],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert completed.stdout == "DEPLOYED_REVIEWED_IMAGE\n"
    assert completed.stderr == ""
    events = _read_test_event_log(events_path)
    migration_events = _read_test_event_log(migration_events_path)
    assert events.index("docker info --format {{.Architecture}}") < events.index(
        f"docker pull {reference}"
    )
    assert events.index(f"docker pull {reference}") < events.index(
        "docker image inspect --format {{.Os}}/{{.Architecture}} " + reference
    )
    assert events.index(
        "docker image inspect --format {{.Os}}/{{.Architecture}} " + reference
    ) < events.index(
        "docker compose --env-file "
        + str(base / "image.env")
        + " -f "
        + str(base / "compose.yml")
        + " up -d"
    )
    assert migration_events == [f"migration {reference}"]
    assert (base / "image.env").read_text(encoding="utf-8") == (
        "TRAVEL_MAP_MANIFEST_DIGEST=" + "a" * 64 + "\n"
    )


def test_deploy_wrapper_stateless_beta_skips_private_database_migration(
    tmp_path: Path,
) -> None:
    reference = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    deploy, environment, _events_path, migration_events_path, base = (
        _deploy_wrapper_fixture(
            tmp_path,
            nas_architecture="amd64",
            image_platform="linux/amd64",
            stateless_beta=True,
            include_migration=False,
        )
    )

    completed = subprocess.run(
        [str(deploy), reference],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert completed.stdout == "DEPLOYED_REVIEWED_IMAGE\n"
    assert completed.stderr == ""
    assert (base / "runtime.env").read_text(encoding="ascii") == "STATELESS_BETA=1\n"
    assert _read_test_event_log(migration_events_path) == []


def test_deploy_wrapper_rejects_a_stateless_user_data_mount_before_mutation(
    tmp_path: Path,
) -> None:
    reference = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    deploy, environment, events_path, migration_events_path, base = (
        _deploy_wrapper_fixture(
            tmp_path,
            nas_architecture="amd64",
            image_platform="linux/amd64",
            stateless_beta=True,
            include_migration=False,
        )
    )
    (base / "compose.yml").write_text(
        "services:\n  app:\n    volumes:\n      - /volume2/docker-1/seoul-education-travel-map/data:/data:rw\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(deploy), reference],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_STATELESS_DATA_MOUNT\n"
    assert _read_test_event_log(events_path) == []
    assert _read_test_event_log(migration_events_path) == []


def test_stateless_compose_example_has_no_user_data_bind_mount() -> None:
    compose = Path("apps/travel-map/deploy/nas/compose.stateless.example.yml").read_text(
        encoding="utf-8"
    )

    assert "/volume2/docker-1/seoul-education-travel-map/data" not in compose
    assert "/data:rw" not in compose
    assert "/volume1/docker/seoul-education-travel-map/runtime.env" in compose


@pytest.mark.parametrize(
    "nas_architecture",
    (
        "",
        "AMD64",
        "x86",
        "i386",
        "arm",
        "armv7l",
        " ",
        "\t",
        " amd64 ",
        "\tamd64",
        "amd64\t",
        "$(touch should-not-run)",
        "amd64; touch should-not-run",
        "`touch should-not-run`",
    ),
)
def test_deploy_wrapper_rejects_unverified_nas_platform_before_mutation(
    tmp_path: Path,
    nas_architecture: str,
) -> None:
    reference = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    deploy, environment, events_path, migration_events_path, base = (
        _deploy_wrapper_fixture(
            tmp_path,
            nas_architecture=nas_architecture,
            image_platform="linux/amd64",
        )
    )
    original_image_env = (base / "image.env").read_text(encoding="utf-8")
    should_not_run = tmp_path / "should-not-run"
    assert not should_not_run.exists()

    completed = subprocess.run(
        [str(deploy), reference],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_NAS_PLATFORM_UNVERIFIED\n"
    events = _read_test_event_log(events_path)
    migration_events = _read_test_event_log(migration_events_path)
    assert events == ["docker info --format {{.Architecture}}"]
    assert migration_events == []
    assert not (base / "previous-image.env").exists()
    assert not list(base.glob(".previous-image.env.*"))
    assert not list(base.glob(".image.env.*"))
    assert not should_not_run.exists()
    assert (base / "image.env").read_text(encoding="utf-8") == original_image_env


@pytest.mark.parametrize(
    ("nas_architecture", "image_platform"),
    (
        ("amd64", "linux/amd64"),
        ("x86_64", "linux/amd64"),
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
    ),
)
def test_deploy_wrapper_rejects_accepted_alias_with_trailing_blank_record(
    tmp_path: Path,
    nas_architecture: str,
    image_platform: str,
) -> None:
    reference = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    deploy, environment, events_path, migration_events_path, base = (
        _deploy_wrapper_fixture(
            tmp_path,
            nas_architecture=nas_architecture,
            image_platform=image_platform,
            docker_info_output=nas_architecture + "\n\n",
        )
    )
    original_image_env = (base / "image.env").read_text(encoding="utf-8")

    completed = subprocess.run(
        [str(deploy), reference],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_NAS_PLATFORM_UNVERIFIED\n"
    assert _read_test_event_log(events_path) == [
        "docker info --format {{.Architecture}}"
    ]
    assert _read_test_event_log(migration_events_path) == []
    assert not (base / "previous-image.env").exists()
    assert not list(base.glob(".previous-image.env.*"))
    assert not list(base.glob(".image.env.*"))
    assert (base / "image.env").read_text(encoding="utf-8") == original_image_env


def test_reviewed_image_handoff_binds_all_attestation_fields_without_rebuild() -> None:
    publish = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )

    assert '[ "$#" -eq 6 ]' in publish
    assert "record=$1" in publish
    assert "expected_image_tag=$2" in publish
    assert "expected_image_id=$3" in publish
    assert "nas_platform=$4" in publish
    assert "git_sha=$5" in publish
    assert "expected_record_sha256=$6" in publish
    assert "validate_approved_record" in publish
    assert "hashlib.sha256(payload).hexdigest() != expected_hash" in publish
    assert "release-gate.sh" not in publish
    assert "RELEASE_GATE_IMAGE_RECORD" not in publish
    assert "travel_root" not in publish
    assert "docker image inspect" in publish
    assert "image_id_hex=${image_id#sha256:}" in publish
    assert "publish_tag=$git_sha-sha256-$image_id_hex" in publish
    assert '[ "${#publish_tag}" -le 128 ]' in publish
    assert "tagged=$registry:$publish_tag" in publish
    assert 'docker tag "$image_id" "$tagged"' in publish
    assert "docker_tool=$(resolve_publish_tool docker)" in publish
    assert '"$docker_tool" "$@"' in publish
    assert "run_buildx imagetools inspect" in publish
    assert '--raw "$repo_digest"' in publish
    assert "BLOCKED_REMOTE_IMAGE_MISMATCH" in publish
    assert "docker build" not in publish


def test_publish_reviewed_image_uses_one_validated_canonical_lock_root() -> None:
    publish = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )

    assert "lock_parent=/tmp/travel-map-publish-locks-$publisher_uid" in publish
    assert "publisher_uid=$(/usr/bin/id -u 2>/dev/null)" in publish
    assert "/usr/bin/python3 -I -S" in publish
    assert "def create_broker_lock()" in publish
    arm_body = publish.split("def arm_resource_broker(", 1)[1].split(
        "\ndef poll_resource_broker", 1
    )[0]
    assert "create_broker_lock()" in arm_body
    assert "precreated_lock_path" in publish
    assert '/bin/mkdir -m 0700 "$lock_parent"' not in publish
    assert '(umask 077 && /bin/mkdir "$lock_directory")' not in publish
    assert '/bin/rmdir "$lock_directory"' not in publish
    assert 'validate_private_directory "$lock_parent"' in publish
    assert "details = Path(sys.argv[1]).lstat()" in publish
    assert "not stat.S_ISDIR(details.st_mode)" in publish
    assert "stat.S_IMODE(details.st_mode) != 0o700" in publish
    assert "details.st_uid != os.getuid()" in publish
    assert "${TMPDIR:-/tmp}/travel-map-publish-locks" not in publish


PUBLISH_GIT_SHA = "1" * 40
PUBLISH_REGISTRY = "ghcr.io/h19h29-design/seoul-education-travel-map"
PUBLISH_TOOL_SEARCH_PATH_ASSIGNMENT = (
    "tool_search_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
    "$trusted_path"
)
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
IN_TOTO_LAYER = "application/vnd.in-toto+json"


def _short_system_tmp_root() -> Path:
    for candidate in (Path("/private/tmp"), Path("/tmp")):
        if candidate.is_dir():
            return candidate.resolve(strict=True)
    raise AssertionError("no supported short system temporary directory")


def _publisher_socket_path(tmp_path: Path) -> Path:
    suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    return _short_system_tmp_root() / f"tm-publisher-{suffix}.sock"


def _safe_recorded_root(raw: str, prefixes: tuple[str, ...]) -> Path:
    root = Path(raw.strip())
    assert root.is_absolute()
    assert root.parent in {Path("/tmp"), Path("/private/tmp")}
    assert root.name.startswith(prefixes)
    return root


def _cleanup_exact_owned_test_root(
    root: Path,
    expected: tuple[int, int],
    *,
    allowed_parents: set[Path],
    prefix: str,
) -> None:
    assert root.is_absolute()
    assert root.parent in allowed_parents
    assert prefix and root.name.startswith(prefix)
    assert len(expected) == 2
    try:
        details = root.lstat()
    except FileNotFoundError:
        return
    if (details.st_dev, details.st_ino) != expected:
        return
    assert stat.S_ISDIR(details.st_mode)
    assert stat.S_IMODE(details.st_mode) == 0o700
    assert details.st_uid == os.getuid()
    assert not root.is_symlink()
    shutil.rmtree(root)


def _remote_descriptor(digest: str, media_type: str) -> dict[str, object]:
    return {"mediaType": media_type, "digest": digest, "size": 1024}


def _encoded_publish_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _publish_payload_descriptor(
    payload: bytes,
    media_type: str,
) -> dict[str, object]:
    return {
        "mediaType": media_type,
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _bind_publish_payloads(
    *,
    image_id: str,
    remote_digest: str,
    root_manifest: dict[str, object],
    raw_children: dict[str, dict[str, object]] | None,
    image_configs: dict[str, dict[str, object]] | None,
) -> tuple[str, str, dict[str, str], dict[str, str], int]:
    bound_root = json.loads(json.dumps(root_manifest))
    root_media_type = str(bound_root["mediaType"])
    raw_payloads: dict[str, str] = {}
    config_payloads: dict[str, str] = {}

    if root_media_type in {OCI_MANIFEST, DOCKER_MANIFEST}:
        config = bound_root["config"]
        assert isinstance(config, dict)
        if config.get("digest") == image_id:
            config_value = (image_configs or {}).get(
                remote_digest,
                {"architecture": "amd64", "os": "linux"},
            )
            config_payload = _encoded_publish_json(config_value)
            bound_config = _publish_payload_descriptor(
                config_payload,
                str(config["mediaType"]),
            )
            bound_root["config"] = bound_config
            image_id = str(bound_config["digest"])
        else:
            config_payload = None
        root_payload = _encoded_publish_json(bound_root)
        root_descriptor = _publish_payload_descriptor(root_payload, root_media_type)
        remote_digest = str(root_descriptor["digest"])
        raw_payloads[remote_digest] = root_payload.hex()
        if config_payload is not None:
            config_payloads[remote_digest] = config_payload.hex()
        return (
            image_id,
            remote_digest,
            raw_payloads,
            config_payloads,
            len(root_payload),
        )

    assert root_media_type == OCI_INDEX
    original_image_id = image_id
    original_remote_digest = remote_digest
    manifests = bound_root["manifests"]
    assert isinstance(manifests, list)
    child_digests: dict[str, str] = {}
    runnable_image_id: str | None = None
    children = raw_children or {}
    configs = image_configs or {}
    for candidate in manifests:
        assert isinstance(candidate, dict)
        logical_digest = str(candidate["digest"])
        child = children.get(logical_digest)
        if child is None:
            continue
        bound_child = json.loads(json.dumps(child))
        child_config = bound_child["config"]
        assert isinstance(child_config, dict)
        platform = candidate.get("platform")
        attestation = isinstance(platform, dict) and platform.get("os") == "unknown"
        config_value = configs.get(
            logical_digest,
            {
                "architecture": "unknown" if attestation else "amd64",
                "os": "unknown" if attestation else "linux",
            },
        )
        config_payload = _encoded_publish_json(config_value)
        bound_child["config"] = _publish_payload_descriptor(
            config_payload,
            str(child_config["mediaType"]),
        )
        if (
            not attestation
            and isinstance(platform, dict)
            and platform.get("os") == "linux"
            and platform.get("architecture") == "amd64"
        ):
            runnable_image_id = str(bound_child["config"]["digest"])
        child_payload = _encoded_publish_json(bound_child)
        child_descriptor = _publish_payload_descriptor(
            child_payload,
            str(candidate["mediaType"]),
        )
        actual_digest = str(child_descriptor["digest"])
        child_digests[logical_digest] = actual_digest
        candidate["digest"] = actual_digest
        candidate["size"] = child_descriptor["size"]
        raw_payloads[actual_digest] = child_payload.hex()
        config_payloads[actual_digest] = config_payload.hex()

    for candidate in manifests:
        assert isinstance(candidate, dict)
        annotations = candidate.get("annotations")
        if not isinstance(annotations, dict):
            continue
        reference = annotations.get("vnd.docker.reference.digest")
        if isinstance(reference, str) and reference in child_digests:
            annotations["vnd.docker.reference.digest"] = child_digests[reference]

    root_payload = _encoded_publish_json(bound_root)
    root_descriptor = _publish_payload_descriptor(root_payload, root_media_type)
    remote_digest = str(root_descriptor["digest"])
    raw_payloads[remote_digest] = root_payload.hex()
    if original_remote_digest == original_image_id and runnable_image_id is not None:
        image_id = runnable_image_id
    return (
        image_id,
        remote_digest,
        raw_payloads,
        config_payloads,
        len(root_payload),
    )


def _bind_remote_descriptor(
    descriptor: dict[str, object],
    *,
    original_digest: str,
    actual_digest: str,
    actual_size: int,
) -> dict[str, object]:
    bound = dict(descriptor)
    if bound.get("digest") == original_digest:
        bound["digest"] = actual_digest
    if bound.get("size") == 1024:
        bound["size"] = actual_size
    return bound


def _published_reference(tmp_path: Path) -> str:
    scenario = json.loads((tmp_path / "scenario.json").read_text(encoding="utf-8"))
    return f"{PUBLISH_REGISTRY}@{scenario['root_digest']}\n"


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


def _write_publish_test_double(
    path: Path,
    *,
    scenario_path: Path,
    state_path: Path,
    environment_log: Path,
) -> None:
    source = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

scenario_path = Path("__SCENARIO_PATH__")
state_path = Path("__STATE_PATH__")
scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
with Path("__ENVIRONMENT_LOG__").open("a", encoding="utf-8") as environment_output:
    environment_output.write(
        json.dumps(
            {
                "names": sorted(os.environ),
                "docker_host": os.environ.get("DOCKER_HOST"),
                "docker_config": os.environ.get("DOCKER_CONFIG"),
            }
        )
        + "\\n"
    )
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
if Path(sys.argv[0]).name == "docker-buildx":
    args.insert(0, "buildx")
if args == ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]:
    print(scenario["docker_host"])
    raise SystemExit(0)
if os.environ.get("DOCKER_HOST") != scenario["docker_host"]:
    fail("publisher did not bind the validated local Docker endpoint")
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
        sys.stdout.buffer.write(bytes.fromhex(payload))
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
        sys.stdout.buffer.write(bytes.fromhex(payload))
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
"""
    replacements = {
        "__SCENARIO_PATH__": str(scenario_path),
        "__STATE_PATH__": str(state_path),
        "__ENVIRONMENT_LOG__": str(environment_log),
    }
    for marker, replacement in replacements.items():
        assert source.count(marker) == 1
        source = source.replace(marker, replacement, 1)
    path.write_text(source, encoding="utf-8")
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
    canonical_lock_stale: bool = False,
    git_sha: str | None = None,
    expect_success: bool = False,
    record_payload: str | None = None,
    publisher_attack: str | None = None,
    cleanup_replacement_attack: bool = False,
    creation_replacement_attack: str | None = None,
    cleanup_child_replacement_attack: str | None = None,
    rename_without_replacement_attack: str | None = None,
    setup_failure: str | None = None,
    shared_parent_disappearance: Path | None = None,
    publisher_source_transform: Callable[[str], str] | None = None,
    publisher_runner: Callable[
        [list[str], Path, dict[str, str], Path], subprocess.CompletedProcess[str]
    ]
    | None = None,
) -> subprocess.CompletedProcess[str]:
    ambient_lock_name = git_sha
    repository = tmp_path / "repository"
    test_root = repository / "apps/travel-map"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(mode=0o700)

    publisher = test_root / "deploy/nas/publish-reviewed-image.sh"
    publisher.parent.mkdir(parents=True)
    publisher_source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    publisher_mode_log = tmp_path / "publisher-modes.jsonl"
    mode_probe = f"""umask 077
/usr/bin/python3 -I -S - "$0" <<'PY'
import json
import stat
import sys
from pathlib import Path

with Path({str(publisher_mode_log)!r}).open("a", encoding="utf-8") as output:
    launcher = Path(sys.argv[1])
    launcher_details = launcher.stat()
    output.write(
        json.dumps(
            {{
                "path": str(launcher),
                "mode": stat.S_IMODE(launcher_details.st_mode),
                "device": launcher_details.st_dev,
                "inode": launcher_details.st_ino,
            }}
        )
        + "\\n"
    )
PY"""
    launcher_anchor = "#!/bin/sh\nset -eu\n\numask 077\n"
    assert publisher_source.count(launcher_anchor) == 1
    publisher_source = publisher_source.replace(
        launcher_anchor,
        "#!/bin/sh\nset -eu\n\n" + mode_probe + "\n",
        1,
    )
    replacements = {
        PUBLISH_TOOL_SEARCH_PATH_ASSIGNMENT: (
            f"tool_search_path={fake_bin}:$trusted_path"
        ),
    }
    for original, replacement in replacements.items():
        assert publisher_source.count(original) == 1
        publisher_source = publisher_source.replace(original, replacement, 1)
    cleanup_replacements = tmp_path / "cleanup-replacements.jsonl"
    if cleanup_replacement_attack:
        record_attack = (
            "    /usr/bin/python3 -I -S - \"$record_parent\" <<'PY'\n"
            "import json\nimport sys\nfrom pathlib import Path\n\n"
            "root = Path(sys.argv[1])\n"
            "owned = root.with_name(root.name + '.owned')\n"
            "root.rename(owned)\nroot.mkdir(mode=0o700)\n"
            "(root / 'replacement-marker').write_text('replacement\\n', encoding='ascii')\n"
            "root_details = root.lstat()\nowned_details = owned.lstat()\n"
            f"with Path({str(cleanup_replacements)!r}).open('a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps({'record': str(root), 'record_identity': [root_details.st_dev, root_details.st_ino], 'owned': str(owned), 'owned_identity': [owned_details.st_dev, owned_details.st_ino]}) + '\\n')\n"
            "PY\n"
        )
        record_anchor = '    if [ -n "$record_parent" ]; then\n'
        assert publisher_source.count(record_anchor) == 1
        publisher_source = publisher_source.replace(
            record_anchor, record_attack + record_anchor, 1
        )
        outer_attack = (
            '    if [ -n "$private_environment" ] && [ -n "$launcher_root" ]; then\n'
            '    /usr/bin/python3 -I -S - "$private_environment" "$launcher_root" <<\'PY\'\n'
            "import json\nimport sys\nfrom pathlib import Path\n\n"
            "records = []\n"
            "for kind, raw in (('private', sys.argv[1]), ('launcher', sys.argv[2])):\n"
            "    if not raw:\n        continue\n"
            "    root = Path(raw)\n"
            "    owned = root.with_name(root.name + '.owned')\n"
            "    root.rename(owned)\n    root.mkdir(mode=0o700)\n"
            "    (root / 'replacement-marker').write_text('replacement\\n', encoding='ascii')\n"
            "    root_details = root.lstat()\n    owned_details = owned.lstat()\n"
            "    records.append({'kind': kind, 'root': str(root), 'root_identity': [root_details.st_dev, root_details.st_ino], 'owned': str(owned), 'owned_identity': [owned_details.st_dev, owned_details.st_ino]})\n"
            f"with Path({str(cleanup_replacements)!r}).open('a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps({'outer': records}) + '\\n')\n"
            "PY\n"
            "    fi\n"
        )
        if "        private_clean = (\n" in publisher_source:
            python_outer_attack = (
                "        import json\n"
                "        records = []\n"
                "        for kind, attacked_root in (('private', private_root), ('launcher', root)):\n"
                "            owned = attacked_root.with_name(attacked_root.name + '.owned')\n"
                "            attacked_root.rename(owned)\n"
                "            attacked_root.mkdir(mode=0o700)\n"
                "            (attacked_root / 'replacement-marker').write_text('replacement\\n', encoding='ascii')\n"
                "            root_details = attacked_root.lstat()\n"
                "            owned_details = owned.lstat()\n"
                "            records.append({'kind': kind, 'root': str(attacked_root), 'root_identity': [root_details.st_dev, root_details.st_ino], 'owned': str(owned), 'owned_identity': [owned_details.st_dev, owned_details.st_ino]})\n"
                f"        with Path({str(cleanup_replacements)!r}).open('a', encoding='utf-8') as output:\n"
                "            output.write(json.dumps({'outer': records}) + '\\n')\n"
            )
            publisher_source = publisher_source.replace(
                "        private_clean = (\n",
                python_outer_attack + "        private_clean = (\n",
                1,
            )
        else:
            outer_anchor = "cleanup_outer_launcher() {\n    cleanup_status=0\n"
            assert publisher_source.count(outer_anchor) == 1
            publisher_source = publisher_source.replace(
                outer_anchor, outer_anchor + outer_attack, 1
            )
    child_replacements = tmp_path / "child-replacements.jsonl"
    if cleanup_child_replacement_attack is not None:
        conditions = {
            "launcher": 'root.name.startswith("travel-map-publish-launcher.") and name == "publish-reviewed-image.sh"',
            "record": 'root.name.startswith("travel-map-publish.") and name == "docker"',
        }
        try:
            child_condition = conditions[cleanup_child_replacement_attack]
        except KeyError as error:
            raise ValueError("unknown cleanup child replacement attack") from error
        if cleanup_child_replacement_attack == "record" and (
            "def remove_broker_entry(" in publisher_source
        ):
            record_root_marker = tmp_path / "record-child-root"
            arm_anchor = (
                "    (\n"
                "        broker_record_root,\n"
                "        broker_record_identity,\n"
                "        broker_lock_path,\n"
                "        broker_lock_identity,\n"
                "        broker_lock_parent_identity,\n"
                "        broker_lock_parent_created,\n"
                "    ) = arm_resource_broker()\n"
            )
            assert publisher_source.count(arm_anchor) == 1
            publisher_source = publisher_source.replace(
                arm_anchor,
                arm_anchor
                + f"    Path({str(record_root_marker)!r}).write_text(broker_record_root + '\\n', encoding='ascii')\n",
                1,
            )
            removal_start = publisher_source.index("def remove_broker_entry(")
            removal_end = publisher_source.index(
                "\n\ndef remove_broker_contents", removal_start
            )
            removal_body = publisher_source[removal_start:removal_end]
            child_anchor = (
                "        details = os.stat(name, dir_fd=quarantine_fd, "
                "follow_symlinks=False)\n"
            )
            child_attack = textwrap.indent(
                (
                    f"if name == 'docker' and not Path({str(child_replacements)!r}).exists():\n"
                    "    import json\n"
                    f"    root = Path(Path({str(record_root_marker)!r}).read_text(encoding='ascii').strip())\n"
                    "    displaced_name = '.child-displaced-' + name\n"
                    "    os.rename(name, displaced_name, src_dir_fd=quarantine_fd, dst_dir_fd=quarantine_fd)\n"
                    "    replacement = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=quarantine_fd)\n"
                    "    try:\n        os.write(replacement, b'replacement\\n')\n"
                    "    finally:\n        os.close(replacement)\n"
                    f"    with Path({str(child_replacements)!r}).open('a', encoding='utf-8') as output:\n"
                    "        output.write(json.dumps({'root': str(root), 'name': name, 'displaced': displaced_name}) + '\\n')\n"
                ),
                " " * 8,
            )
            assert removal_body.count(child_anchor) == 1
            publisher_source = (
                publisher_source[:removal_start]
                + removal_body.replace(child_anchor, child_anchor + child_attack, 1)
                + publisher_source[removal_end:]
            )
        elif cleanup_child_replacement_attack == "launcher" and (
            "        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)\n"
            in publisher_source
        ):
            child_anchor = "        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)\n"
            child_attack = textwrap.indent(
                (
                    f"if name == 'publish-reviewed-image.sh' and not Path({str(child_replacements)!r}).exists():\n"
                    "    import json\n"
                    "    displaced_name = '.child-displaced-' + name\n"
                    "    os.rename(name, displaced_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)\n"
                    "    replacement = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)\n"
                    "    try:\n        os.write(replacement, b'replacement\\n')\n"
                    "    finally:\n        os.close(replacement)\n"
                    f"    with Path({str(child_replacements)!r}).open('a', encoding='utf-8') as output:\n"
                    "        output.write(json.dumps({'root': str(root), 'name': name, 'displaced': displaced_name}) + '\\n')\n"
                ),
                " " * 8,
            )
        else:
            child_anchor = (
                "        details = os.stat(name, dir_fd=descriptor, "
                "follow_symlinks=False)\n"
            )
            child_attack = textwrap.indent(
                (
                    f"if {child_condition} and not Path({str(child_replacements)!r}).exists():\n"
                    "    import json\n"
                    "    displaced_name = '.child-displaced-' + name\n"
                    "    os.rename(name, displaced_name, src_dir_fd=descriptor, dst_dir_fd=descriptor)\n"
                    "    replacement = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=descriptor)\n"
                    "    try:\n        os.write(replacement, b'replacement\\n')\n"
                    "    finally:\n        os.close(replacement)\n"
                    f"    with Path({str(child_replacements)!r}).open('a', encoding='utf-8') as output:\n"
                    "        output.write(json.dumps({'root': str(root), 'name': name, 'displaced': displaced_name}) + '\\n')\n"
                ),
                " " * 8,
            )
        assert publisher_source.count(child_anchor) >= 1
        publisher_source = publisher_source.replace(
            child_anchor, child_anchor + child_attack, 1
        )
    creation_replacements = tmp_path / "creation-replacements.jsonl"
    if creation_replacement_attack is not None:
        anchors = {
            "launcher": (
                (
                    '                [ -n "$launcher_root" ] && [ -n "$launcher_root_identity" ] \\\n'
                    "                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'\n"
                ),
                '"$launcher_root"',
                "                ",
            ),
            "environment": (
                (
                    '        [ -n "$private_environment" ] && [ -n "$private_environment_identity" ] \\\n'
                    "            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
                ),
                '"$private_environment"',
                "        ",
            ),
            "record": (
                (
                    '[ -n "$record_parent" ] && [ -n "$record_parent_identity" ] \\\n'
                    "    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
                ),
                '"$record_parent"',
                "",
            ),
        }
        try:
            creation_anchor, root_variable, indentation = anchors[
                creation_replacement_attack
            ]
        except KeyError as error:
            raise ValueError("unknown creation replacement attack") from error
        assert publisher_source.count(creation_anchor) == 1
        creation_attack = (
            f"{indentation}/usr/bin/python3 -I -S - {root_variable} <<'PY'\n"
            "import json\nimport sys\nfrom pathlib import Path\n\n"
            "root = Path(sys.argv[1])\n"
            "displaced = root.with_name(root.name + '.displaced')\n"
            "root.rename(displaced)\nroot.mkdir(mode=0o700)\n"
            "(root / 'replacement-marker').write_text('replacement\\n', encoding='ascii')\n"
            f"with Path({str(creation_replacements)!r}).open('a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps({'root': str(root), 'displaced': str(displaced)}) + '\\n')\n"
            "PY\n"
        )
        publisher_source = publisher_source.replace(
            creation_anchor, creation_anchor + creation_attack, 1
        )
    rename_records = tmp_path / "rename-without-replacement.jsonl"
    if rename_without_replacement_attack is not None:
        rename_anchors = {
            "launcher": (
                (
                    '                [ -n "$launcher_root" ] && [ -n "$launcher_root_identity" ] \\\n'
                    "                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'\n"
                ),
                "$launcher_root",
            ),
            "environment": (
                (
                    '        [ -n "$private_environment" ] && [ -n "$private_environment_identity" ] \\\n'
                    "            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
                ),
                "$private_environment",
            ),
            "record": (
                (
                    '[ -n "$record_parent" ] && [ -n "$record_parent_identity" ] \\\n'
                    "    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
                ),
                "$record_parent",
            ),
        }
        try:
            rename_anchor, rename_variable = rename_anchors[
                rename_without_replacement_attack
            ]
        except KeyError as error:
            raise ValueError("unknown rename attack") from error
        assert publisher_source.count(rename_anchor) == 1
        rename_attack = (
            f"/bin/mv {rename_variable} {rename_variable}.renamed\n"
            f"/usr/bin/printf '%s\\n' {rename_variable}.renamed >> {str(rename_records)!r}\n"
        )
        publisher_source = publisher_source.replace(
            rename_anchor, rename_anchor + rename_attack, 1
        )
    setup_records = tmp_path / "setup-failure-roots.jsonl"
    if setup_failure is not None:
        if setup_failure == "record":
            setup_anchor = (
                "    (\n"
                "        broker_record_root,\n"
                "        broker_record_identity,\n"
                "        broker_lock_path,\n"
                "        broker_lock_identity,\n"
                "        broker_lock_parent_identity,\n"
                "        broker_lock_parent_created,\n"
                "    ) = arm_resource_broker()\n"
            )
            setup_attack = (
                f"    Path({str(setup_records)!r}).write_text(broker_record_root + '\\n', encoding='ascii')\n"
                "    raise OSError\n"
            )
        elif setup_failure == "environment":
            setup_anchor = (
                '            "$private_xdg_data" \\\n'
                "            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
            )
            setup_attack = (
                f"/usr/bin/printf '%s\\n' $private_environment > {str(setup_records)!r}\n"
                "exit 2\n"
            )
        else:
            raise ValueError("unknown setup failure")
        assert publisher_source.count(setup_anchor) == 1
        publisher_source = publisher_source.replace(
            setup_anchor, setup_anchor + setup_attack, 1
        )
    if shared_parent_disappearance is not None:
        matching_start = publisher_source.index(
            "def matching_identity(parent_fd: int, expected: tuple[int, int]) -> list[str]:\n"
        )
        matching_end = publisher_source.index("\n\ndef ", matching_start + 1)
        matching_body = publisher_source[matching_start:matching_end]
        shared_parent_anchor = (
            "def matching_identity(parent_fd: int, expected: tuple[int, int]) -> list[str]:\n"
            "    matches = []\n"
        )
        assert matching_body.count(shared_parent_anchor) == 1
        shared_parent_attack = (
            "def matching_identity(parent_fd: int, expected: tuple[int, int]) -> list[str]:\n"
            f"    probe = Path({str(shared_parent_disappearance)!r})\n"
            "    matches = []\n"
        )
        matching_body = matching_body.replace(
            shared_parent_anchor, shared_parent_attack, 1
        )
        loop_anchor = "    for candidate in os.listdir(parent_fd):\n"
        assert matching_body.count(loop_anchor) == 1
        matching_body = matching_body.replace(
            loop_anchor,
            (
                loop_anchor + "        if candidate == probe.name:\n"
                "            os.rmdir(candidate, dir_fd=parent_fd)\n"
            ),
            1,
        )
        publisher_source = (
            publisher_source[:matching_start]
            + matching_body
            + publisher_source[matching_end:]
        )
    if publisher_source_transform is not None:
        publisher_source = publisher_source_transform(publisher_source)
    publisher.write_text(publisher_source, encoding="utf-8")
    publisher.chmod(0o755)
    subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "init", "-q"],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "config",
            "user.name",
            "Publisher Test",
        ],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "publisher-test@example.invalid",
        ],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "commit",
            "-qm",
            "reviewed publisher",
        ],
        check=True,
    )
    git_sha = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    publisher_relative = "apps/travel-map/deploy/nas/publish-reviewed-image.sh"
    if publisher_attack == "dirty":
        with publisher.open("a", encoding="utf-8") as output:
            output.write("\n# accidental unreviewed publisher edit\n")
    elif publisher_attack == "mode":
        publisher.chmod(0o777)
    elif publisher_attack == "assume":
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repository),
                "update-index",
                "--assume-unchanged",
                publisher_relative,
            ],
            check=True,
        )
    elif publisher_attack == "skip":
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repository),
                "update-index",
                "--skip-worktree",
                publisher_relative,
            ],
            check=True,
        )
    elif publisher_attack is not None:
        raise ValueError(f"unknown publisher attack: {publisher_attack}")

    original_image_id = image_id
    original_remote_digest = remote_digest
    (
        image_id,
        remote_digest,
        raw_by_digest,
        image_by_digest,
        root_size,
    ) = _bind_publish_payloads(
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=root_manifest,
        raw_children=raw_children,
        image_configs=image_configs,
    )
    valid_record = (
        f"imageTag=seoul-education-travel-map:release-gate-{git_sha}\n"
        f"imageId={image_id}\n"
        "platform=linux/amd64\n"
        f"gitSha={git_sha}\n"
    )
    approved_record_parent = tmp_path / "approved-record"
    approved_record_parent.mkdir(mode=0o700)
    approved_record_parent.chmod(0o700)
    approved_record = approved_record_parent / "gated-image.record"
    approved_payload = (
        record_payload if record_payload is not None else valid_record
    ).encode("ascii")
    approved_record.write_bytes(approved_payload)
    approved_record.chmod(0o600)
    approved_record_sha256 = hashlib.sha256(approved_payload).hexdigest()

    image_tag = f"seoul-education-travel-map:release-gate-{git_sha}"
    image_id_hex = image_id.removeprefix("sha256:")
    tagged_reference = f"{PUBLISH_REGISTRY}:{git_sha}-sha256-{image_id_hex}"
    platform = "linux/amd64"
    inspect = lambda value: {"id": value, "platform": platform}
    root_media_type = str(root_manifest["mediaType"])
    if tag_descriptors is None:
        descriptors = [
            {
                "digest": remote_digest,
                "mediaType": root_media_type,
                "size": root_size,
            },
            {
                "digest": remote_digest,
                "mediaType": root_media_type,
                "size": root_size,
            },
        ]
    else:
        descriptors = [
            _bind_remote_descriptor(
                descriptor,
                original_digest=original_remote_digest,
                actual_digest=remote_digest,
                actual_size=root_size,
            )
            for descriptor in tag_descriptors
        ]
    if local_tag_ids is None:
        tag_id_sequence = (image_id, image_id, image_id)
    else:
        tag_id_sequence = tuple(
            image_id if value == original_image_id else value for value in local_tag_ids
        )
    publisher_socket = _publisher_socket_path(tmp_path)
    scenario = {
        "docker_host": "unix://" + str(publisher_socket),
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
            remote_digest: (
                {
                    "digest": remote_digest,
                    "mediaType": root_media_type,
                    "size": root_size,
                }
                if immutable_descriptor is None
                else _bind_remote_descriptor(
                    immutable_descriptor,
                    original_digest=original_remote_digest,
                    actual_digest=remote_digest,
                    actual_size=root_size,
                )
            )
        },
        "raw_by_digest": raw_by_digest,
        "image_by_digest": image_by_digest,
        "root_digest": remote_digest,
        "required_raw_digests": list(raw_by_digest),
        "required_image_digests": list(image_by_digest),
        "expect_push": remote_lookup_mode == "missing",
        "expected_tag_lookup_attempts": 4 if remote_lookup_mode == "missing" else 2,
        "expect_success": expect_success,
    }
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    docker_state = tmp_path / "docker-state.json"
    publisher_environment_log = tmp_path / "publisher-environment.jsonl"
    _write_publish_test_double(
        fake_bin / "docker",
        scenario_path=scenario_path,
        state_path=docker_state,
        environment_log=publisher_environment_log,
    )
    shutil.copy2(fake_bin / "docker", fake_bin / "docker-buildx")
    (fake_bin / "docker-buildx").chmod(0o755)

    docker_config = tmp_path / "protected-docker"
    docker_config.mkdir(mode=0o700)
    docker_json = docker_config / "config.json"
    docker_json.write_text(
        '{"auths":{"ghcr.io":{"auth":"dGVzdA=="}},"currentContext":"release-test"}\n',
        encoding="utf-8",
    )
    docker_json.chmod(0o600)
    context_id = hashlib.sha256(b"release-test").hexdigest()
    context_root = docker_config / "contexts/meta" / context_id
    context_root.mkdir(mode=0o700, parents=True)
    for directory in (
        docker_config / "contexts",
        docker_config / "contexts/meta",
        context_root,
    ):
        directory.chmod(0o700)
    context_metadata = context_root / "meta.json"
    context_metadata.write_text(
        json.dumps(
            {
                "Name": "release-test",
                "Metadata": {},
                "Endpoints": {
                    "docker": {
                        "Host": f"unix://{publisher_socket}",
                        "SkipTLSVerify": False,
                    }
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    context_metadata.chmod(0o600)
    ambient_tmp = tmp_path / "ambient-tmp"
    ambient_tmp.mkdir()
    ambient_python = tmp_path / "ambient-python"
    ambient_python.mkdir()
    (ambient_python / "sitecustomize.py").write_text(
        "raise SystemExit('ambient publisher sitecustomize')\n",
        encoding="utf-8",
    )

    # Keep failure tracebacks deterministic and incapable of reproducing host
    # credentials while still exercising explicit ambient-injection inputs.
    environment: dict[str, str] = {}
    environment.update(
        {
            "PATH": "/nonexistent",
            "HOME": str(tmp_path / "ambient-home"),
            "TMPDIR": str(ambient_tmp),
            "PYTHONPATH": str(ambient_python),
            "PYTHONWARNINGS": "ignore",
            "PYTEST_ADDOPTS": "--collect-only",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "ambient.gitconfig"),
            "GIT_DIR": str(tmp_path / "ambient-git-dir"),
            "DOCKER_CONFIG": str(docker_config),
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "DOCKER_CONTEXT": "ambient-context",
            "BUILDKIT_HOST": "tcp://attacker.invalid:1234",
            "KAKAO_REST_API_KEY": "ambient-rest-secret",
            "SEOUL_TRANSIT_SERVICE_KEY": "ambient-transit-secret",
            "OPINET_CERT_KEY": "ambient-opinet-secret",
            "KAKAO_OIDC_CLIENT_ID": "ambient-oidc-id",
            "KAKAO_OIDC_CLIENT_SECRET": "ambient-oidc-secret",
        }
    )
    ambient_lock_directory = None
    if ambient_lock_stale:
        ambient_tmpdir = Path(environment.get("TMPDIR", str(tmp_path)))
        environment["TMPDIR"] = str(ambient_tmpdir)
        ambient_lock_parent = ambient_tmpdir / f"travel-map-publish-locks-{os.getuid()}"
        ambient_lock_parent.mkdir(mode=0o700, exist_ok=True)
        ambient_lock_directory = ambient_lock_parent / (ambient_lock_name or git_sha)
        ambient_lock_directory.mkdir(mode=0o700)
    if canonical_lock_stale:
        canonical_lock_parent = Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")
        canonical_lock_parent.mkdir(mode=0o700, exist_ok=True)
        (canonical_lock_parent / git_sha).mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(publisher_socket))
    publisher_socket.chmod(0o600)
    listener.listen(1)
    try:
        command = [
            str(publisher),
            str(approved_record.resolve(strict=True)),
            image_tag,
            image_id,
            platform,
            git_sha,
            approved_record_sha256,
        ]
        if publisher_runner is not None:
            return publisher_runner(
                command, publisher.parents[4], environment, docker_state
            )
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        listener.close()
        publisher_socket.unlink(missing_ok=True)
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
    assert completed.stdout == _published_reference(tmp_path)
    assert completed.stderr == ""


def test_publish_reviewed_image_runs_approved_private_launcher_copy(
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

    modes = [
        json.loads(line)
        for line in (tmp_path / "publisher-modes.jsonl").read_text().splitlines()
    ]
    assert completed.returncode == 0
    assert [event["mode"] for event in modes] == [0o755, 0o500, 0o500]
    source_launcher = Path(modes[0]["path"])
    private_launcher = Path(modes[1]["path"])
    assert source_launcher == (
        tmp_path / "repository/apps/travel-map/deploy/nas/publish-reviewed-image.sh"
    )
    assert private_launcher.parent.parent == _short_system_tmp_root()
    assert private_launcher.parent.name.startswith("travel-map-publish-launcher.")
    assert private_launcher.name == "publish-reviewed-image.sh"
    assert modes[2]["path"] == modes[1]["path"]
    assert (modes[2]["device"], modes[2]["inode"]) == (
        modes[1]["device"],
        modes[1]["inode"],
    )
    assert (modes[1]["device"], modes[1]["inode"]) != (
        modes[0]["device"],
        modes[0]["inode"],
    )


def test_publish_reviewed_image_preserves_replaced_owned_cleanup_roots(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        expect_success=True,
        cleanup_replacement_attack=True,
    )
    replacements = [
        json.loads(line)
        for line in (tmp_path / "cleanup-replacements.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(replacements) == 2
    record = next(item for item in replacements if "record" in item)
    outer = next(item for item in replacements if "outer" in item)
    roots = [Path(record["record"])] + [Path(item["root"]) for item in outer["outer"]]
    cleanup_roots = [
        (Path(record["record"]), tuple(record["record_identity"])),
        (Path(record["owned"]), tuple(record["owned_identity"])),
    ] + [
        (Path(item[field]), tuple(item[f"{field}_identity"]))
        for item in outer["outer"]
        for field in ("root", "owned")
    ]
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == ""
        assert all(
            root.is_dir()
            and not root.is_symlink()
            and (root / "replacement-marker").read_text(encoding="ascii")
            == "replacement\n"
            for root in roots
        )
    finally:
        for root, expected in cleanup_roots:
            prefix = next(
                candidate
                for candidate in (
                    "travel-map-publish.",
                    "travel-map-publish-environment.",
                    "travel-map-publish-launcher.",
                )
                if root.name.startswith(candidate)
            )
            _cleanup_exact_owned_test_root(
                root,
                expected,
                allowed_parents={Path("/tmp"), Path("/private/tmp")},
                prefix=prefix,
            )


@pytest.mark.parametrize("root_kind", ("launcher", "environment", "record"))
def test_publish_reviewed_image_binds_private_root_identity_at_creation(
    tmp_path: Path,
    root_kind: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        expect_success=True,
        creation_replacement_attack=root_kind,
    )
    replacement = json.loads(
        (tmp_path / "creation-replacements.jsonl").read_text(encoding="utf-8")
    )
    root = Path(replacement["root"])
    displaced = Path(replacement["displaced"])
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert (root / "replacement-marker").read_text(
            encoding="ascii"
        ) == "replacement\n"
        assert not displaced.exists()
    finally:
        for cleanup_root in (root, displaced):
            assert cleanup_root.is_absolute()
            assert cleanup_root.parent in {Path("/tmp"), Path("/private/tmp")}
            assert cleanup_root.name.startswith(
                (
                    "travel-map-publish-launcher.",
                    "travel-map-publish-environment.",
                    "travel-map-publish.",
                )
            )
            if cleanup_root.exists():
                assert cleanup_root.is_dir() and not cleanup_root.is_symlink()
                shutil.rmtree(cleanup_root)
            assert not cleanup_root.exists() and not cleanup_root.is_symlink()


@pytest.mark.parametrize("root_kind", ("launcher", "record"))
def test_publish_reviewed_image_preserves_replaced_cleanup_child(
    tmp_path: Path,
    root_kind: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        expect_success=True,
        cleanup_child_replacement_attack=root_kind,
    )
    replacement = json.loads(
        (tmp_path / "child-replacements.jsonl").read_text(encoding="utf-8")
    )
    root = Path(replacement["root"])
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        preserved = [
            child
            for child in root.rglob(replacement["name"])
            if child.read_text(encoding="ascii") == "replacement\n"
        ]
        assert len(preserved) == 1
    finally:
        assert root.is_absolute() and root.parent in {
            Path("/tmp"),
            Path("/private/tmp"),
        }
        assert root.name.startswith(
            ("travel-map-publish.", "travel-map-publish-launcher.")
        )
        if root.exists():
            assert root.is_dir() and not root.is_symlink()
            shutil.rmtree(root)
        assert not root.exists() and not root.is_symlink()


def test_publish_reviewed_image_delimits_negative_cleanup_group_operands(
    tmp_path: Path,
) -> None:
    kill_log = tmp_path / "kill-arguments.jsonl"
    fake_kill = tmp_path / "safe-kill"
    fake_kill.write_text(
        f"""#!/usr/bin/python3
import json
import os
import signal
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path({str(kill_log)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments, separators=(",", ":")) + "\\n")
if (
    len(arguments) == 2
    and arguments[0] in {{"-TERM", "-0"}}
    and arguments[1].isdigit()
):
    raise SystemExit(0)
if (
    len(arguments) == 2
    and arguments[0] == "-KILL"
    and arguments[1].isdigit()
):
    target = int(arguments[1])
    if target <= 1 or target in {{os.getpid(), os.getppid()}}:
        raise SystemExit(97)
    os.kill(target, signal.SIGKILL)
    raise SystemExit(0)
if (
    len(arguments) in {{2, 3}}
    and arguments[0] in {{"-TERM", "-KILL"}}
    and arguments[-1].startswith("-")
    and arguments[-1][1:].isdigit()
):
    raise SystemExit(0)
raise SystemExit(97)
""",
        encoding="utf-8",
    )
    fake_kill.chmod(0o755)

    def transform(source: str) -> str:
        assert source.count("/bin/kill") == 5
        source = source.replace("/bin/kill", shlex.quote(str(fake_kill)))
        timeout_anchor = '[ "$signal_ticks" -ge 1200 ]'
        assert source.count(timeout_anchor) == 1
        return source.replace(timeout_anchor, '[ "$signal_ticks" -ge 0 ]', 1)

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        cleanup_child_replacement_attack="record",
        publisher_source_transform=transform,
    )

    calls = [
        json.loads(line) for line in kill_log.read_text(encoding="utf-8").splitlines()
    ]
    signal_pid = calls[0][1]
    assert signal_pid.isdigit() and int(signal_pid) > 1
    assert calls == [
        ["-TERM", signal_pid],
        ["-TERM", "--", f"-{signal_pid}"],
        ["-0", signal_pid],
        ["-KILL", signal_pid],
        ["-KILL", "--", f"-{signal_pid}"],
    ]
    assert completed.returncode == 2
    assert completed.stdout == ""


@pytest.mark.parametrize("root_kind", ("launcher", "environment", "record"))
def test_publish_reviewed_image_cleans_renamed_owned_root_without_replacement(
    tmp_path: Path,
    root_kind: str,
) -> None:
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        rename_without_replacement_attack=root_kind,
    )
    renamed = Path(
        (tmp_path / "rename-without-replacement.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert not renamed.exists()
    finally:
        root = _safe_recorded_root(
            str(renamed),
            (
                "travel-map-publish-launcher.",
                "travel-map-publish-environment.",
                "travel-map-publish.",
            ),
        )
        if root.exists():
            assert root.is_dir() and not root.is_symlink()
            shutil.rmtree(root)
        assert not root.exists() and not root.is_symlink()


@pytest.mark.parametrize("setup_failure", ("environment", "record"))
def test_publish_reviewed_image_cleans_root_when_setup_fails(
    tmp_path: Path,
    setup_failure: str,
) -> None:
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        setup_failure=setup_failure,
    )
    root = _safe_recorded_root(
        (tmp_path / "setup-failure-roots.jsonl").read_text(encoding="utf-8"),
        ("travel-map-publish-environment.", "travel-map-publish."),
    )
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert not root.exists()
    finally:
        if root.exists():
            assert root.is_dir() and not root.is_symlink()
            shutil.rmtree(root)
        assert not root.exists() and not root.is_symlink()


def test_publish_reviewed_image_tolerates_unrelated_parent_entry_disappearing(
    tmp_path: Path,
) -> None:
    probe = _short_system_tmp_root() / f"travel-map-publish-unrelated-{os.getpid()}"
    probe.mkdir(mode=0o700)
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        shared_parent_disappearance=probe,
        expect_success=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == _published_reference(tmp_path)
    assert completed.stderr == ""
    assert not probe.exists() and not probe.is_symlink()


@pytest.mark.parametrize("publisher_attack", ("dirty", "mode", "assume", "skip"))
def test_publish_reviewed_image_blocks_unreviewed_launcher_before_docker(
    tmp_path: Path,
    publisher_attack: str,
) -> None:
    image_id = "sha256:" + "a" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        publisher_attack=publisher_attack,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_PUBLISH_CONTEXT\n"
    assert not (tmp_path / "publisher-environment.jsonl").exists()


def test_publish_reviewed_image_removes_ambient_injection_environment(
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
    expected_docker_host = "unix://" + str(_publisher_socket_path(tmp_path))
    docker_environments = [
        json.loads(line)
        for line in (tmp_path / "publisher-environment.jsonl").read_text().splitlines()
    ]
    banned = {
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "GIT_DIR",
        "DOCKER_CONTEXT",
        "BUILDKIT_HOST",
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
        "KAKAO_OIDC_CLIENT_ID",
        "KAKAO_OIDC_CLIENT_SECRET",
    }
    assert docker_environments
    assert all(
        not set(environment["names"]).intersection(banned)
        for environment in docker_environments
    )
    assert all(
        environment["docker_host"] is not None for environment in docker_environments
    )
    assert all(
        environment["docker_host"] == expected_docker_host
        for environment in docker_environments
    )
    assert all(
        environment["docker_config"] == str(tmp_path / "protected-docker")
        for environment in docker_environments
    )


@pytest.mark.parametrize(
    "record_payload",
    (
        "imageTag=seoul-education-travel-map:release-gate-" + "1" * 40 + "\n"
        "imageId=sha256:" + "a" * 64 + "\n"
        "platform=linux/amd64\n"
        "gitSha=" + "1" * 40 + "\n"
        "extra=ambient\n",
        "imageTag=seoul-education-travel-map:release-gate-" + "1" * 40 + "\n"
        "imageId=sha256:" + "a" * 64 + "\n"
        "imageId=sha256:" + "a" * 64 + "\n"
        "platform=linux/amd64\n",
        "imageTag=$(touch ambient)\n"
        "imageId=sha256:" + "a" * 64 + "\n"
        "platform=linux/amd64\n"
        "gitSha=" + "1" * 40,
    ),
    ids=("extra-field", "duplicate-field", "shell-and-missing-newline"),
)
def test_publish_reviewed_image_rejects_malformed_approved_record_injection(
    tmp_path: Path,
    record_payload: str,
) -> None:
    image_id = "sha256:" + "a" * 64

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        git_sha=PUBLISH_GIT_SHA,
        record_payload=record_payload,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_GATE_ATTESTATION\n"
    assert not (tmp_path / "ambient").exists()


def test_publish_reviewed_image_rejects_malformed_descriptor_scalar_types(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    malformed = _remote_descriptor(remote_digest, OCI_MANIFEST)
    malformed["size"] = True

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        tag_descriptors=[malformed],
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_REMOTE_IMAGE_MISMATCH\n"


def test_publish_reviewed_image_accepts_containerd_index_with_linked_attestation(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "c" * 64
    runnable_digest = "sha256:" + "d" * 64
    attestation_digest = "sha256:" + "e" * 64
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
            runnable_digest: _image_manifest(image_id),
            attestation_digest: _image_manifest(
                attestation_config,
                attestation=True,
            ),
        },
        image_configs={runnable_digest: {"architecture": "amd64", "os": "linux"}},
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == _published_reference(tmp_path)
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
            runnable_digest: _image_manifest(image_id),
        },
        image_configs={runnable_digest: {"architecture": "amd64", "os": "linux"}},
        expect_success=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == _published_reference(tmp_path)
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
    assert completed.stdout == _published_reference(tmp_path)
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
    assert completed.stdout == _published_reference(tmp_path)
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
    assert completed.stdout == _published_reference(tmp_path)
    assert completed.stderr == ""


def test_publish_reviewed_image_blocks_stale_canonical_lock_without_removing_it(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + "a" * 64
    lock_directory: Path | None = None
    try:
        completed = _run_publish_reviewed_image(
            tmp_path,
            image_id=image_id,
            remote_digest="sha256:" + "b" * 64,
            root_manifest=_image_manifest(image_id),
            canonical_lock_stale=True,
        )
        git_sha = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(tmp_path / "repository"),
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        lock_directory = Path(f"/tmp/travel-map-publish-locks-{os.getuid()}") / git_sha

        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == "BLOCKED_PUBLISH_LOCKED\n"
        assert lock_directory.is_dir()
    finally:
        if lock_directory is not None:
            lock_directory.rmdir()


def _assert_publish_not_mutated(tmp_path: Path) -> None:
    state_path = tmp_path / "docker-state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {}
    )
    assert state.get("tag_created", False) is False
    assert state.get("pushed", False) is False


def test_publish_reviewed_image_rejects_renamed_canonical_lock_leaf_after_validation(
    tmp_path: Path,
) -> None:
    """A lock leaf displaced after validation must not authorize publication."""
    marker = tmp_path / "canonical-lock-leaf-rename.json"

    def transform(source: str) -> str:
        anchor = (
            "validate_owned_private_directory \\\n"
            '    "$lock_directory" "$lock_directory_identity" "$lock_parent" "$git_sha" \\\n'
            "    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
        )
        assert source.count(anchor) == 1
        attack = (
            '/bin/mv "$lock_directory" "$lock_directory.renamed"\n'
            f'/usr/bin/printf \'%s\\n\' "$lock_directory" "$lock_directory.renamed" > {str(marker)!r}\n'
        )
        return source.replace(anchor, anchor + attack, 1)

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        publisher_source_transform=transform,
    )

    _canonical_raw, renamed_raw = marker.read_text(encoding="ascii").splitlines()
    renamed = Path(renamed_raw)
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        _assert_publish_not_mutated(tmp_path)
        # The displaced inode is still broker-owned and is reclaimed by identity.
        assert not renamed.exists()
    finally:
        if renamed.exists():
            renamed.rmdir()


def test_publish_reviewed_image_rejects_replaced_canonical_lock_parent_after_validation(
    tmp_path: Path,
) -> None:
    """Replacing the canonical lock parent after its initial check must fail closed."""
    marker = tmp_path / "canonical-lock-parent-replace.json"
    canonical_parent = Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")
    parent_preexisted = canonical_parent.is_dir()
    preexisting_children = (
        {child.name for child in canonical_parent.iterdir()}
        if parent_preexisted
        else set()
    )

    def transform(source: str) -> str:
        anchor = (
            "validate_owned_private_directory \\\n"
            '    "$lock_directory" "$lock_directory_identity" "$lock_parent" "$git_sha" \\\n'
            "    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
        )
        assert source.count(anchor) == 1
        attack = (
            '/bin/mv "$lock_parent" "$lock_parent.displaced"\n'
            '(umask 077 && /bin/mkdir "$lock_parent")\n'
            '(umask 077 && /bin/mkdir "$lock_parent/$git_sha")\n'
            f'/usr/bin/printf \'%s\\n\' "$lock_parent" "$lock_parent.displaced" > {str(marker)!r}\n'
        )
        return source.replace(anchor, anchor + attack, 1)

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        publisher_source_transform=transform,
    )

    replacement_raw, displaced_raw = marker.read_text(encoding="ascii").splitlines()
    replacement = Path(replacement_raw)
    displaced = Path(displaced_raw)
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        _assert_publish_not_mutated(tmp_path)
        assert replacement.is_dir() and not replacement.is_symlink()
        replacement_children = list(replacement.iterdir())
        assert len(replacement_children) == 1
        assert replacement_children[0].is_dir()
        if parent_preexisted:
            assert displaced.is_dir() and not displaced.is_symlink()
            assert {child.name for child in displaced.iterdir()} == preexisting_children
        else:
            assert not displaced.exists()
    finally:
        if replacement.exists():
            shutil.rmtree(replacement)
        if parent_preexisted and displaced.exists():
            displaced.rename(replacement)
        elif displaced.exists():
            shutil.rmtree(displaced)


def test_publish_reviewed_image_binds_record_creation_to_first_opened_descriptor() -> (
    None
):
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(encoding="utf-8")
    body = source.split("def create_broker_directory(", 1)[1].split(
        "\n\ndef create_broker_lock", 1
    )[0]

    mkdir_at = body.index("os.mkdir(name, 0o700, dir_fd=parent_fd)")
    open_at = body.index("descriptor = os.open(", mkdir_at)
    first_path_stat = body.index("path_details = os.stat(", mkdir_at)
    assert mkdir_at < open_at < first_path_stat
    assert "created_expected = descriptor_identity(os.fstat(descriptor))" in body


def test_publish_reviewed_image_publishes_preopened_random_lock_parent_no_replace() -> (
    None
):
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(encoding="utf-8")
    body = source.split("def create_broker_lock()", 1)[1].split(
        "\n\ndef descriptor_identity", 1
    )[0]

    assert ".travel-map-lock-parent." in body
    assert "rename_no_replace(" in body
    assert "SAME_UID_CREATION_BOUNDARY" in body


@pytest.mark.parametrize("broker_kill_phase", ("arm", "tag-armed", "tag-created"))
def test_publish_reviewed_image_reclaims_resources_when_broker_dies_at_each_phase(
    tmp_path: Path,
    broker_kill_phase: str,
) -> None:
    """A dead resource broker must revoke all publication authority and resources."""
    marker = tmp_path / f"broker-killed-{broker_kill_phase}.json"

    def transform(source: str) -> str:
        arm_anchor = (
            "    (\n"
            "        broker_record_root,\n"
            "        broker_record_identity,\n"
            "        broker_lock_path,\n"
            "        broker_lock_identity,\n"
            "        broker_lock_parent_identity,\n"
            "        broker_lock_parent_created,\n"
            "    ) = arm_resource_broker()\n"
        )
        assert source.count(arm_anchor) == 1
        arm_probe = (
            f"    Path({str(marker)!r}).write_text(\n"
            "        f'{broker_pid}\\n{broker_record_root}\\n{broker_record_identity}\\n'\n"
            "        f'{broker_lock_path}\\n{broker_lock_identity}\\n',\n"
            "        encoding='ascii',\n"
            "    )\n"
        )
        if broker_kill_phase == "arm":
            arm_probe += "    os.kill(broker_pid, signal.SIGKILL)\n    raise OSError\n"
        source = source.replace(arm_anchor, arm_anchor + arm_probe, 1)
        if broker_kill_phase == "tag-armed":
            tag_anchor = (
                "    exec 9>&-\n"
                "    fallback_tag_arm_fd=\n"
                "    exec 8>&-\n"
                "    tag_arm_fd=\n"
            )
            assert source.count(tag_anchor) == 1
            kill_code = (
                f"    /usr/bin/python3 -I -S - {str(marker)!r} <<'PY'\n"
                "import os\n"
                "import signal\n"
                "import sys\n"
                "from pathlib import Path\n"
                "broker_pid = int(Path(sys.argv[1]).read_text(encoding='ascii').splitlines()[0])\n"
                "os.kill(broker_pid, signal.SIGKILL)\n"
                "PY\n"
                "    return 1\n"
            )
            source = source.replace(
                tag_anchor,
                tag_anchor + kill_code,
                1,
            )
        elif broker_kill_phase == "tag-created":
            tag_anchor = '    run_docker tag "$image_id" "$tagged" || blocked \'BLOCKED_IMAGE_TAGGING\'\n'
            assert source.count(tag_anchor) == 1
            kill_code = (
                f"    /usr/bin/python3 -I -S - {str(marker)!r} <<'PY'\n"
                "import os\n"
                "import signal\n"
                "import sys\n"
                "from pathlib import Path\n"
                "broker_pid = int(Path(sys.argv[1]).read_text(encoding='ascii').splitlines()[0])\n"
                "os.kill(broker_pid, signal.SIGKILL)\n"
                "PY\n"
                "    exit 2\n"
            )
            source = source.replace(tag_anchor, tag_anchor + kill_code, 1)
        return source

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        publisher_source_transform=transform,
    )

    broker_pid_raw, record_raw, record_identity_raw, lock_raw, lock_identity_raw = (
        marker.read_text(encoding="ascii").splitlines()
    )
    broker_pid = int(broker_pid_raw)
    record_path = Path(record_raw)
    record_identity = tuple(int(part) for part in record_identity_raw.split(":"))
    lock_path = Path(lock_raw)
    lock_identity = tuple(int(part) for part in lock_identity_raw.split(":"))
    assert re.fullmatch(r"[0-9a-f]{40}", lock_path.name) is not None
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        state_path = tmp_path / "docker-state.json"
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {}
        )
        assert state.get("pushed", False) is False
        assert state.get("tagged", False) is False
        assert state.get("tag_created", False) is (broker_kill_phase == "tag-created")
        assert not record_path.exists()
        assert not lock_path.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(broker_pid, 0)
    finally:
        _cleanup_exact_owned_test_root(
            record_path,
            record_identity,
            allowed_parents={Path("/tmp"), Path("/private/tmp")},
            prefix="travel-map-publish.",
        )
        _cleanup_exact_owned_test_root(
            lock_path,
            lock_identity,
            allowed_parents={Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")},
            prefix=lock_path.name,
        )


@pytest.mark.parametrize("broker_prearm_kill_point", ("record", "lock"))
def test_publish_reviewed_image_reclaims_prearm_broker_resources(
    tmp_path: Path,
    broker_prearm_kill_point: str,
) -> None:
    """A broker dying before ARM must not strand either resource it created."""
    marker = tmp_path / f"broker-prearm-{broker_prearm_kill_point}.txt"

    def transform(source: str) -> str:
        record_anchor = (
            "    record_root = precreated_record_root\n"
            "    record_expected = precreated_record_expected\n"
        )
        assert source.count(record_anchor) == 1
        if broker_prearm_kill_point == "record":
            probe = (
                f"    Path({str(marker)!r}).write_text(\n"
                "        f'{record_root}\\n{record_expected[0]}:{record_expected[1]}\\n',\n"
                "        encoding='ascii',\n"
                "    )\n"
                "    os._exit(79)\n"
            )
            return source.replace(record_anchor, record_anchor + probe, 1)

        lock_anchor = (
            "    lock_path = precreated_lock_path\n"
            "    lock_expected = precreated_lock_expected\n"
            "    lock_parent_expected = precreated_lock_parent_expected\n"
            "    lock_parent_created = precreated_lock_parent_created\n"
        )
        assert source.count(lock_anchor) == 1
        probe = (
            f"    Path({str(marker)!r}).write_text(\n"
            "        f'{record_root}\\n{record_expected[0]}:{record_expected[1]}\\n'\n"
            "        f'{lock_path}\\n{lock_expected[0]}:{lock_expected[1]}\\n',\n"
            "        encoding='ascii',\n"
            "    )\n"
            "    os._exit(79)\n"
        )
        return source.replace(lock_anchor, lock_anchor + probe, 1)

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        publisher_source_transform=transform,
    )

    fields = marker.read_text(encoding="ascii").splitlines()
    assert len(fields) in {2, 4}
    record_path = Path(fields[0])
    record_identity = tuple(int(part) for part in fields[1].split(":"))
    lock_path = Path(fields[2]) if len(fields) == 4 else None
    lock_identity = (
        tuple(int(part) for part in fields[3].split(":")) if len(fields) == 4 else None
    )

    def identity_exists(parent: Path, expected: tuple[int, ...]) -> bool:
        try:
            entries = parent.iterdir()
        except FileNotFoundError:
            return False
        for entry in entries:
            try:
                details = entry.lstat()
            except FileNotFoundError:
                continue
            if (details.st_dev, details.st_ino) == expected:
                return True
        return False

    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert not record_path.exists()
        assert not identity_exists(record_path.parent, record_identity)
        with pytest.raises(FileNotFoundError):
            os.stat(record_path, follow_symlinks=False)
        if lock_path is not None and lock_identity is not None:
            assert not lock_path.exists()
            assert not identity_exists(lock_path.parent, lock_identity)
            with pytest.raises(FileNotFoundError):
                os.stat(lock_path, follow_symlinks=False)
    finally:
        for path in (record_path, lock_path):
            if path is not None and path.exists() and path.is_dir():
                shutil.rmtree(path)


def test_publish_reviewed_image_rejects_broker_record_hardlink_cleanup(
    tmp_path: Path,
) -> None:
    external_link = tmp_path / "broker-record-hardlink"
    record_marker = tmp_path / "broker-record-root"

    def transform(source: str) -> str:
        arm_anchor = (
            "    (\n"
            "        broker_record_root,\n"
            "        broker_record_identity,\n"
            "        broker_lock_path,\n"
            "        broker_lock_identity,\n"
            "        broker_lock_parent_identity,\n"
            "        broker_lock_parent_created,\n"
            "    ) = arm_resource_broker()\n"
        )
        assert source.count(arm_anchor) == 1
        source = source.replace(
            arm_anchor,
            arm_anchor
            + f"    Path({str(record_marker)!r}).write_text(broker_record_root + '\\n', encoding='ascii')\n",
            1,
        )
        start = source.index("def remove_broker_entry(")
        end = source.index("\n\ndef remove_broker_contents", start)
        body = source[start:end]
        anchor = (
            "        else:\n"
            "            final = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)\n"
        )
        assert body.count(anchor) == 1
        injection = (
            "        else:\n"
            "            if name == 'docker' and not "
            f"Path({str(external_link)!r}).exists():\n"
            "                os.link(\n"
            "                    name,\n"
            f"                    {str(external_link)!r},\n"
            "                    src_dir_fd=quarantine_fd,\n"
            "                    follow_symlinks=False,\n"
            "                )\n"
            "            final = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)\n"
        )
        return source[:start] + body.replace(anchor, injection, 1) + source[end:]

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        expect_success=True,
        publisher_source_transform=transform,
    )
    record_root = Path(record_marker.read_text(encoding="ascii").strip())

    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert external_link.is_file() and not external_link.is_symlink()
        assert record_root.is_dir() and not record_root.is_symlink()
    finally:
        external_link.unlink(missing_ok=True)
        if record_root.exists():
            shutil.rmtree(record_root)


@pytest.mark.parametrize(
    "observation_fault",
    ("observer-construction", "ps-nonzero", "ps-malformed"),
)
def test_publish_reviewed_image_quiesces_descendant_before_broker_fault_cleanup(
    tmp_path: Path,
    observation_fault: str,
) -> None:
    child_marker = tmp_path / f"broker-{observation_fault}.child"
    cleanup_probe = tmp_path / f"broker-{observation_fault}.cleanup"

    def transform(source: str) -> str:
        tag_anchor = "    owns_tagged=1\n"
        assert source.count(tag_anchor) == 1
        child_body = textwrap.dedent(
            f"""
            /usr/bin/python3 -I -S - <<'PY' >/dev/null 2>&1 &
            import os
            import signal
            import time
            from pathlib import Path

            for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(handled, signal.SIG_IGN)
            Path({str(child_marker)!r}).write_text(str(os.getpid()) + "\\n", encoding="ascii")
            while True:
                time.sleep(1)
            PY
            child_ticks=0
            while [ ! -s {str(child_marker)!r} ]; do
                [ "$child_ticks" -lt 1000 ] || exit 2
                /bin/sleep 0.01
                child_ticks=$((child_ticks + 1))
            done
            """
        )
        source = source.replace(tag_anchor, tag_anchor + child_body, 1)

        cleanup_start = source.index("def cleanup_broker_directory(")
        cleanup_end = source.index("\n\ndef cleanup_broker_lock", cleanup_start)
        cleanup_body = source[cleanup_start:cleanup_end]
        cleanup_anchor = ") -> bool:\n    if path.parent != parent or not path.name.startswith(prefix):\n"
        assert cleanup_body.count(cleanup_anchor) == 1
        cleanup_injection = (
            ") -> bool:\n"
            "    if prefix == 'travel-map-publish.' and not "
            f"Path({str(cleanup_probe)!r}).exists() and "
            f"Path({str(child_marker)!r}).is_file():\n"
            f"        child_pid = int(Path({str(child_marker)!r}).read_text(encoding='ascii'))\n"
            "        try:\n"
            "            os.kill(child_pid, 0)\n"
            "        except ProcessLookupError:\n"
            "            child_state = 'gone'\n"
            "        else:\n"
            "            child_state = 'alive'\n"
            f"        Path({str(cleanup_probe)!r}).write_text(child_state + '\\n', encoding='ascii')\n"
            "    if path.parent != parent or not path.name.startswith(prefix):\n"
        )
        source = (
            source[:cleanup_start]
            + cleanup_body.replace(cleanup_anchor, cleanup_injection, 1)
            + source[cleanup_end:]
        )

        if observation_fault == "observer-construction":
            observer_start = source.index("class BrokerExitObserver:")
            observer_end = source.index(
                "\n\ndef extend_broker_owned_tree", observer_start
            )
            observer_body = source[observer_start:observer_end]
            observer_anchor = (
                "    def __init__(self, pid: int):\n        self.pid = pid\n"
            )
            assert observer_body.count(observer_anchor) == 1
            observer_injection = (
                "    def __init__(self, pid: int):\n"
                "        self.pid = pid\n"
                "        deadline = time.monotonic() + 60\n"
                f"        while not Path({str(child_marker)!r}).is_file():\n"
                "            if time.monotonic() >= deadline:\n"
                "                raise OSError\n"
                "            time.sleep(0.01)\n"
                "        raise OSError\n"
            )
            source = (
                source[:observer_start]
                + observer_body.replace(observer_anchor, observer_injection, 1)
                + source[observer_end:]
            )
        else:
            table_start = source.index("def broker_process_table()")
            table_end = source.index("\n\ndef broker_stable_identity", table_start)
            table_body = source[table_start:table_end]
            table_anchor = (
                "            if completed.returncode != 0:\n"
                "                raise OSError\n"
            )
            assert table_body.count(table_anchor) == 1
            replacement = (
                "            fault_attempts = globals().get(\n"
                "                'broker_observation_fault_attempts', 3\n"
                "            )\n"
                f"            if Path({str(child_marker)!r}).is_file() and fault_attempts:\n"
                "                globals()['broker_observation_fault_attempts'] = (\n"
                "                    fault_attempts - 1\n"
                "                )\n"
                + (
                    "                completed = subprocess.CompletedProcess(completed.args, 1, b'')\n"
                    if observation_fault == "ps-nonzero"
                    else "                completed = subprocess.CompletedProcess(completed.args, 0, b'malformed\\n')\n"
                )
                + table_anchor
            )
            source = (
                source[:table_start]
                + table_body.replace(table_anchor, replacement, 1)
                + source[table_end:]
            )
        return source

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        publisher_source_transform=transform,
    )

    child_pid = int(child_marker.read_text(encoding="ascii"))
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert cleanup_probe.read_text(encoding="ascii") == "gone\n"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_publish_reviewed_image_rejects_empty_broker_process_snapshot(
    tmp_path: Path,
) -> None:
    """An empty successful ps snapshot cannot authorize broker cleanup."""
    child_marker = tmp_path / "broker-empty-ps.child"
    fault_disable_marker = tmp_path / "broker-empty-ps.disable"
    observation_marker = tmp_path / "broker-empty-ps.observed"
    cleanup_probe = tmp_path / "broker-empty-ps.cleanup"
    resource_marker = tmp_path / "broker-empty-ps.resources"

    def transform(source: str) -> str:
        tag_anchor = "    owns_tagged=1\n"
        assert source.count(tag_anchor) == 1
        child_body = textwrap.dedent(
            f"""
            /usr/bin/python3 -I -S - <<'PY' >/dev/null 2>&1 &
            import os
            import signal
            import time
            from pathlib import Path

            for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(handled, signal.SIG_IGN)
            Path({str(child_marker)!r}).write_text(str(os.getpid()) + "\\n", encoding="ascii")
            while True:
                time.sleep(1)
            PY
            child_ticks=0
            while [ ! -s {str(child_marker)!r} ]; do
                [ "$child_ticks" -lt 1000 ] || exit 2
                /bin/sleep 0.01
                child_ticks=$((child_ticks + 1))
            done
            """
        )
        source = source.replace(tag_anchor, tag_anchor + child_body, 1)

        resource_anchor = (
            "    lock_path = precreated_lock_path\n"
            "    lock_expected = precreated_lock_expected\n"
            "    lock_parent_expected = precreated_lock_parent_expected\n"
            "    lock_parent_created = precreated_lock_parent_created\n"
        )
        assert source.count(resource_anchor) == 1
        resource_probe = (
            f"    Path({str(resource_marker)!r}).write_text(\n"
            "        f'{record_root}\\n{record_expected[0]}:{record_expected[1]}\\n'\n"
            "        f'{lock_path}\\n{lock_expected[0]}:{lock_expected[1]}\\n',\n"
            "        encoding='ascii',\n"
            "    )\n"
        )
        source = source.replace(resource_anchor, resource_anchor + resource_probe, 1)

        table_anchor = (
            "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        )
        assert source.count(table_anchor) == 2
        ps_override = (
            "real_subprocess_run = subprocess.run\n"
            "def run_empty_ps_after_child(*args, **kwargs):\n"
            "    command = args[0] if args else kwargs.get('args', [])\n"
            f"    if command[:2] == ['/bin/ps', '-axo'] and Path({str(child_marker)!r}).is_file() and not Path({str(fault_disable_marker)!r}).is_file():\n"
            f"        Path({str(observation_marker)!r}).touch()\n"
            "        return subprocess.CompletedProcess(command, 0, b'', b'')\n"
            "    return real_subprocess_run(*args, **kwargs)\n"
            "subprocess.run = run_empty_ps_after_child\n\n"
        )
        source = (
            source[: source.rfind(table_anchor)]
            + ps_override
            + source[source.rfind(table_anchor) :]
        )

        cleanup_start = source.index("def cleanup_broker_directory(")
        cleanup_end = source.index("\n\ndef cleanup_broker_lock", cleanup_start)
        cleanup_body = source[cleanup_start:cleanup_end]
        cleanup_anchor = ") -> bool:\n    if path.parent != parent or not path.name.startswith(prefix):\n"
        assert cleanup_body.count(cleanup_anchor) == 1
        cleanup_probe_code = (
            ") -> bool:\n"
            "    if prefix == 'travel-map-publish.' and not "
            f"Path({str(cleanup_probe)!r}).exists() and Path({str(child_marker)!r}).is_file():\n"
            f"        child_pid = int(Path({str(child_marker)!r}).read_text(encoding='ascii'))\n"
            "        try:\n"
            "            os.kill(child_pid, 0)\n"
            "        except ProcessLookupError:\n"
            "            child_state = 'gone'\n"
            "        else:\n"
            "            child_state = 'alive'\n"
            f"        Path({str(cleanup_probe)!r}).write_text(child_state + '\\n', encoding='ascii')\n"
            "    if path.parent != parent or not path.name.startswith(prefix):\n"
        )
        return (
            source[:cleanup_start]
            + cleanup_body.replace(cleanup_anchor, cleanup_probe_code, 1)
            + source[cleanup_end:]
        )

    def process_identity(pid: int) -> tuple[int, int, int, str]:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart="],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 8 and fields[0].isdigit() and int(fields[0]) == pid:
                return (
                    int(fields[0]),
                    int(fields[1]),
                    int(fields[2]),
                    " ".join(fields[3:]),
                )
        raise AssertionError(f"process identity disappeared for pid {pid}")

    def process_identity_alive(identity: tuple[int, int, int, str]) -> bool:
        try:
            return process_identity(identity[0]) == identity
        except AssertionError:
            return False

    def resource_identity_alive(path: Path, expected: tuple[int, int]) -> bool:
        try:
            entries = tuple(path.parent.iterdir())
        except FileNotFoundError:
            return False
        for entry in entries:
            try:
                details = entry.lstat()
            except FileNotFoundError:
                continue
            if (
                (details.st_dev, details.st_ino) == expected
                and entry.is_dir()
                and not entry.is_symlink()
            ):
                return True
        return False

    boundary: dict[str, bool] = {}

    def runner(
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        state_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        root_identity = process_identity(process.pid)
        child_identity: tuple[int, int, int, str] | None = None
        child_pid: int | None = None
        try:
            deadline = time.monotonic() + 120
            while (
                not resource_marker.is_file()
                or not child_marker.is_file()
                or not observation_marker.is_file()
            ):
                if process.poll() is not None:
                    raise AssertionError(
                        "publisher exited before resource/child markers: "
                        f"resource={resource_marker.exists()} "
                        f"child={child_marker.exists()} "
                        f"observation={observation_marker.exists()} "
                        f"return={process.returncode}"
                    )
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "publisher did not reach empty-ps boundary: "
                        f"resource={resource_marker.exists()} "
                        f"child={child_marker.exists()} "
                        f"observation={observation_marker.exists()} "
                        f"return={process.poll()}"
                    )
                time.sleep(0.02)
            resource_marker.read_text(encoding="ascii")
            child_pid = int(child_marker.read_text(encoding="ascii"))
            child_identity = process_identity(child_pid)
            fields = resource_marker.read_text(encoding="ascii").splitlines()
            if len(fields) != 4:
                raise AssertionError(f"unexpected resource marker: {fields!r}")
            record_path = Path(fields[0])
            record_identity = tuple(int(part) for part in fields[1].split(":"))
            lock_path = Path(fields[2])
            lock_identity = tuple(int(part) for part in fields[3].split(":"))
            if len(record_identity) != 2 or len(lock_identity) != 2:
                raise AssertionError(f"unexpected resource identities: {fields!r}")
            observation_deadline = time.monotonic() + 0.5
            while time.monotonic() < observation_deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            boundary.update(
                root_alive=(
                    process.poll() is None and process_identity_alive(root_identity)
                ),
                child_alive=process_identity_alive(child_identity),
                record_alive=resource_identity_alive(record_path, record_identity),
                lock_alive=resource_identity_alive(lock_path, lock_identity),
                cleanup_absent=not cleanup_probe.exists(),
            )
            if child_identity is not None and child_pid is not None:
                try:
                    if process_identity_alive(child_identity):
                        os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            fault_disable_marker.touch()
            stdout, stderr = process.communicate(timeout=30)
            boundary["production_cleanup_completed"] = not resource_identity_alive(
                record_path, record_identity
            ) and not resource_identity_alive(lock_path, lock_identity)
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
        finally:
            if child_identity is not None and child_pid is not None:
                try:
                    if process_identity_alive(child_identity):
                        os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None and process_identity_alive(root_identity):
                process.kill()
                process.wait(timeout=5)

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id="sha256:" + "a" * 64,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest("sha256:" + "a" * 64),
        publisher_source_transform=transform,
        publisher_runner=runner,
    )

    fields = resource_marker.read_text(encoding="ascii").splitlines()
    assert len(fields) == 4
    record_path = Path(fields[0])
    record_identity = tuple(int(part) for part in fields[1].split(":"))
    lock_path = Path(fields[2])
    lock_identity = tuple(int(part) for part in fields[3].split(":"))
    assert re.fullmatch(r"[0-9a-f]{40}", lock_path.name) is not None
    try:
        assert boundary == {
            "root_alive": True,
            "child_alive": True,
            "record_alive": True,
            "lock_alive": True,
            "cleanup_absent": True,
            "production_cleanup_completed": True,
        }
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert cleanup_probe.read_text(encoding="ascii") == "gone\n"
    finally:
        _cleanup_exact_owned_test_root(
            record_path,
            record_identity,
            allowed_parents={Path("/tmp"), Path("/private/tmp")},
            prefix="travel-map-publish.",
        )
        _cleanup_exact_owned_test_root(
            lock_path,
            lock_identity,
            allowed_parents={Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")},
            prefix=lock_path.name,
        )


def test_release_fixture_exact_root_teardown_preserves_replacement() -> None:
    owned = _short_system_tmp_root() / (
        f"travel-map-publish.teardown-{os.getpid()}-{time.monotonic_ns()}"
    )
    displaced = owned.with_name(owned.name + ".owned")
    replacement_marker = owned / "replacement-marker"
    owned.mkdir(mode=0o700)
    details = owned.lstat()
    expected = (details.st_dev, details.st_ino)
    try:
        owned.rename(displaced)
        owned.mkdir(mode=0o700)
        replacement_marker.write_text("replacement\n", encoding="ascii")

        _cleanup_exact_owned_test_root(
            owned,
            expected,
            allowed_parents={Path("/tmp"), Path("/private/tmp")},
            prefix="travel-map-publish.",
        )

        assert replacement_marker.read_text(encoding="ascii") == "replacement\n"
    finally:
        for root in (owned, displaced):
            assert root.parent == _short_system_tmp_root()
            assert root.name.startswith("travel-map-publish.teardown-")
            if root.exists():
                assert root.is_dir() and not root.is_symlink()
                shutil.rmtree(root)


def test_publish_reviewed_image_retries_one_transient_broker_process_snapshot(
    tmp_path: Path,
) -> None:
    """One transient ps failure must not turn a valid publication into a flaky denial."""

    def transform(source: str) -> str:
        anchor = (
            "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        )
        assert source.count(anchor) == 2
        injection = (
            "real_subprocess_run = subprocess.run\n"
            "transient_ps_failure = True\n\n"
            "def run_with_one_transient_ps_failure(*args, **kwargs):\n"
            "    global transient_ps_failure\n"
            "    command = args[0] if args else kwargs.get('args', [])\n"
            "    if command[:2] == ['/bin/ps', '-axo'] and transient_ps_failure:\n"
            "        transient_ps_failure = False\n"
            "        return subprocess.CompletedProcess(command, 1, b'', b'')\n"
            "    return real_subprocess_run(*args, **kwargs)\n\n"
            "subprocess.run = run_with_one_transient_ps_failure\n\n"
        )
        position = source.rfind(anchor)
        return source[:position] + injection + source[position:]

    image_id = "sha256:" + "a" * 64
    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest="sha256:" + "b" * 64,
        root_manifest=_image_manifest(image_id),
        expect_success=True,
        publisher_source_transform=transform,
    )

    assert completed.returncode == 0
    assert completed.stdout == _published_reference(tmp_path)
    assert completed.stderr == ""


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


@pytest.mark.parametrize("identity_mode", ("classic-config", "containerd-config"))
def test_publish_reviewed_image_rejects_classic_or_containerd_config_mismatch(
    tmp_path: Path,
    identity_mode: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    raw_children = None
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
        raw_children = {
            runnable_digest: _image_manifest("sha256:" + "c" * 64),
            attestation_digest: _image_manifest(
                "sha256:" + "7" * 64,
                attestation=True,
            ),
        }

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=root_manifest,
        raw_children=raw_children,
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
        local_tag_ids=(image_id, image_id, "sha256:" + "0" * 64),
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
    assert "runtime_env=$base/runtime.env" in deploy
    assert 'source "$runtime_env"' not in deploy
    assert '. "$runtime_env"' not in deploy
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


# Production break caught: an operator can otherwise publish an unreviewed,
# mutable, wrong-platform image while believing it is the deployed rollback.
def test_rollback_baseline_publisher_is_tracked_from_the_reviewed_git_object() -> None:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ROLLBACK_REVIEW_COMMIT, "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    object_path = f"{ROLLBACK_REVIEW_COMMIT}:apps/travel-map/deploy/nas/publish-rollback-baseline.sh"
    blob = subprocess.run(
        ["git", "rev-parse", object_path],
        check=False,
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode == 0
    assert blob.returncode == 0
    assert blob.stdout.strip() == ROLLBACK_PUBLISH_BLOB_SHA, (
        "the reachable restoration must retain the publisher reviewed at "
        + ROLLBACK_ORIGINAL_REVIEW_COMMIT
    )
    assert ROLLBACK_PUBLISH.is_file()
    publisher = ROLLBACK_PUBLISH.read_text(encoding="utf-8")
    assert f"rollback_sha={ROLLBACK_SHA}" in publisher
    assert "rollback_platform=linux/amd64" in publisher


def test_rollback_baseline_publisher_emits_only_the_verified_immutable_digest(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"
    assert not result.python_injection_marker.exists()
    assert "test-only-docker-auth-marker" not in (
        result.completed.stdout + result.completed.stderr
    )
    gate_fields = result.gate_log.read_text(encoding="utf-8").strip().split("|")
    assert gate_fields[:5] == ["1", "error", "linux/amd64", "linux/amd64", "0"]
    assert Path(gate_fields[5]).name == "pinned-source"
    assert Path(gate_fields[5]) != result.source
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert (
        docker_calls.count(
            "buildx imagetools inspect --format {{json .Manifest}} " + ROLLBACK_TAG
        )
        == 4
    )
    run_call = next(
        line for line in docker_calls.splitlines() if line.startswith("run -d ")
    )
    for required in (
        "--user 10001:10001",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--env-file ",
        ROLLBACK_IMAGE_ID,
    ):
        assert required in run_call
    assert f"tag {ROLLBACK_IMAGE_ID} {ROLLBACK_TAG}" in docker_calls
    assert f"push {ROLLBACK_TAG}" in docker_calls
    assert "image rm " not in docker_calls
    assert not list(result.temporary_root.glob("travel-map-rollback-publish.*"))
    for retained_tag in ("legacy-created", "rollback-created"):
        assert (result.docker_state / retained_tag).exists()
        assert (result.docker_state / f"{retained_tag}.identity").read_text(
            encoding="utf-8"
        ).strip() == ROLLBACK_IMAGE_ID
    assert os.access(ROLLBACK_PUBLISH, os.X_OK)


# Production break caught: ambient Git and Docker control variables can redirect
# the publisher's source proof or registry operations before the release gate.
def test_rollback_baseline_publisher_scrubs_outer_git_and_docker_environment(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, inject_ambient_controls=True)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"


# Production break caught: repository-local accelerators can make a nominally
# clean worktree status omit changes unless each source proof disables them.
def test_rollback_baseline_publisher_disables_repo_local_git_accelerators(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, require_hardened_git=True)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"


# Production break caught: refs/replace can retain the approved SHA spelling
# while substituting a different commit/tree for archive and ls-tree.
def test_rollback_baseline_publisher_disables_git_replace_objects(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, require_no_replace_objects=True)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"


# Production break caught: ignored pytest/Playwright/venv/node_modules files in
# the operator worktree can execute inside the historical release gate.
def test_rollback_baseline_publisher_runs_gate_from_the_pinned_tree(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, git_state="ignored-gate-input")

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"
    gate_cwd = Path(result.gate_log.read_text(encoding="utf-8").strip().split("|")[5])
    assert gate_cwd.name == "pinned-source"
    assert gate_cwd != result.source


# Production break caught: `git archive` applies tar.umask and commonly emits
# tracked 100755/100644 blobs as 0775/0664. The private pinned tree must restore
# exact executable modes before the historical gate is instrumented and run.
def test_rollback_baseline_publisher_normalizes_git_archive_modes(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"


# Production break caught: retrying a completed one-time publication can
# overwrite the fixed remote tag even though the identical artifact is present.
def test_rollback_baseline_publisher_accepts_an_identical_remote_without_push(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_scenario="remote-existing-identical",
    )

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert f"push {ROLLBACK_TAG}" not in docker_calls
    assert f"tag {ROLLBACK_IMAGE_ID} {ROLLBACK_TAG}" not in docker_calls


# Production break caught: buildx may report a genuinely absent top-level tag
# as an exact `<reference>: not found` line instead of `manifest unknown`.
def test_rollback_baseline_publisher_accepts_exact_top_level_tag_absence(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_scenario="exact-top-level-not-found",
    )

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert f"push {ROLLBACK_TAG}" in docker_calls


# `docker buildx imagetools inspect --format '{{json .Manifest}}'` returns an
# OCI descriptor, not a raw manifest with schemaVersion.
def test_rollback_baseline_publisher_accepts_the_real_buildx_descriptor_shape(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}\n"


# The fake historical gate must exercise the exact legacy build tag so the
# publisher test cannot silently drift back to a nonexistent attestation file.
def test_rollback_publisher_fixture_builds_the_exact_historical_legacy_tag(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path)

    assert result.completed.returncode == 0
    docker_calls = result.docker_log.read_text(encoding="utf-8").splitlines()
    build_calls = [line for line in docker_calls if line.startswith("build ")]
    assert len(build_calls) == 1
    assert build_calls[0].startswith("build --iidfile ")
    assert (
        " --build-arg SNAPSHOT_ID=fake-snapshot "
        f"-t {ROLLBACK_LEGACY_TAG} fake-release-context"
    ) in build_calls[0]


# Production break caught: containerd image storage reports the loaded OCI index
# digest as .Id, so treating every .Id as a config digest blocks the reviewed image.
def test_rollback_baseline_publisher_proves_a_containerd_descriptor_id(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, docker_scenario="containerd-index")

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_IMAGE_ID}\n"
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert (
        f"buildx imagetools inspect --raw {ROLLBACK_REGISTRY}@{ROLLBACK_IMAGE_ID}"
        in docker_calls
    )
    assert (
        "buildx imagetools inspect --raw "
        f"{ROLLBACK_REGISTRY}@{ROLLBACK_PLATFORM_MANIFEST}" in docker_calls
    )


@pytest.mark.parametrize(
    "docker_scenario",
    ("containerd-zero-attestation", "containerd-multiple-attestations"),
)
def test_rollback_baseline_publisher_accepts_zero_or_multiple_valid_attestations(
    tmp_path: Path,
    docker_scenario: str,
) -> None:
    result = _run_rollback_publisher(tmp_path, docker_scenario=docker_scenario)

    assert result.completed.returncode == 0
    assert result.completed.stdout == f"{ROLLBACK_REGISTRY}@{ROLLBACK_IMAGE_ID}\n"


@pytest.mark.parametrize(
    ("git_state", "link_source"),
    (
        ("wrong-sha", False),
        ("dirty", False),
        ("attached", False),
        ("assume-unchanged", False),
        ("skip-worktree", False),
        ("ignored-allowed-suffix", False),
        ("clean", True),
    ),
)
def test_rollback_baseline_publisher_rejects_an_unfixed_or_dirty_source(
    tmp_path: Path,
    git_state: str,
    link_source: bool,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        git_state=git_state,
        link_source=link_source,
    )

    assert result.completed.returncode == 2
    assert result.completed.stdout == ""
    assert result.completed.stderr == "BLOCKED_INVALID_ROLLBACK_SOURCE\n"
    assert not result.gate_log.exists()
    assert not result.docker_log.exists()


@pytest.mark.parametrize(
    ("directory_mode", "config_mode"),
    ((0o755, 0o600), (0o700, 0o644)),
)
def test_rollback_baseline_publisher_requires_private_owned_docker_configuration(
    tmp_path: Path,
    directory_mode: int,
    config_mode: int,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_directory_mode=directory_mode,
        docker_config_mode=config_mode,
    )

    assert result.completed.returncode == 2
    assert result.completed.stdout == ""
    assert result.completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"
    assert not result.gate_log.exists()
    assert not result.docker_log.exists()


@pytest.mark.parametrize(
    ("docker_scenario", "expected_error", "expected_gate_calls"),
    (
        ("local-existing", "BLOCKED_LEGACY_TAG_EXISTS", 0),
        ("local-rollback-existing", "BLOCKED_ROLLBACK_TAG_EXISTS", 1),
        ("remote-existing", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-malformed-descriptor", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-descriptor-media-type-invalid", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-descriptor-size-zero", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-descriptor-kind-mismatch", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-raw-media-type-mismatch", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-referenced-manifest-missing", "BLOCKED_REMOTE_TAG_UNVERIFIED", 1),
        ("remote-ambiguous", "BLOCKED_REMOTE_TAG_UNVERIFIED", 1),
        ("remote-helper-missing", "BLOCKED_REMOTE_TAG_UNVERIFIED", 1),
        ("remote-race", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("local-platform-mismatch", "BLOCKED_INVALID_GATE_ATTESTATION", 1),
        ("tagged-id-mismatch", "BLOCKED_ROLLBACK_IMAGE_TAGGING", 1),
        ("remote-config-mismatch", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-malformed-config-shape", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-invalid-config-descriptor", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("remote-platform-mismatch", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("containerd-index-id-mismatch", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("containerd-extra-runnable", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("containerd-unlinked-attestation", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("containerd-attestation-missing", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("containerd-attestation-not-attestation", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        ("containerd-malformed-child-config", "BLOCKED_REMOTE_IMAGE_MISMATCH", 1),
        (
            "containerd-invalid-child-config-descriptor",
            "BLOCKED_REMOTE_IMAGE_MISMATCH",
            1,
        ),
    ),
)
def test_rollback_baseline_publisher_fails_closed_at_each_image_boundary(
    tmp_path: Path,
    docker_scenario: str,
    expected_error: str,
    expected_gate_calls: int,
) -> None:
    result = _run_rollback_publisher(tmp_path, docker_scenario=docker_scenario)

    assert result.completed.returncode == 2
    assert result.completed.stdout == ""
    assert result.completed.stderr.endswith(f"{expected_error}\n")
    assert "Traceback" not in result.completed.stderr
    actual_gate_calls = (
        len(result.gate_log.read_text(encoding="utf-8").splitlines())
        if result.gate_log.exists()
        else 0
    )
    assert actual_gate_calls == expected_gate_calls
    assert not list(result.temporary_root.glob("travel-map-rollback-publish.*"))


@pytest.mark.parametrize(
    ("docker_scenario", "expected_error", "competitor_marker"),
    (
        ("legacy-build-race", "BLOCKED_ROLLBACK_RELEASE_GATE", "legacy-created"),
        ("rollback-tag-race", "BLOCKED_ROLLBACK_IMAGE_TAGGING", "rollback-created"),
        (
            "container-name-race",
            "BLOCKED_ROLLBACK_RUNTIME_SMOKE",
            "competitor-container",
        ),
    ),
)
def test_rollback_baseline_publisher_never_removes_an_unowned_local_resource(
    tmp_path: Path,
    docker_scenario: str,
    expected_error: str,
    competitor_marker: str,
) -> None:
    result = _run_rollback_publisher(tmp_path, docker_scenario=docker_scenario)

    assert result.completed.returncode == 2
    assert result.completed.stderr.endswith(f"{expected_error}\n")
    assert (result.docker_state / competitor_marker).exists()


def test_rollback_baseline_publisher_cleans_a_failed_owned_container_start(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_scenario="container-start-failure-owned",
    )

    assert result.completed.returncode == 2
    assert result.completed.stderr.endswith("BLOCKED_ROLLBACK_RUNTIME_SMOKE\n")
    assert not (result.docker_state / "owned-container").exists()
    assert f"rm -f {ROLLBACK_CONTAINER_ID}" in result.docker_log.read_text(
        encoding="utf-8"
    )


# Production break caught: the historical gate's fixed legacy tag is mutable.
# A successful build must be bound to its immutable iidfile before that name can
# be retagged by another Docker client.
def test_rollback_baseline_publisher_rejects_a_post_gate_legacy_retag(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_scenario="legacy-retag-after-gate",
    )

    assert result.completed.returncode == 2
    assert result.completed.stderr.endswith("BLOCKED_INVALID_GATE_ATTESTATION\n")
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert "run -d" not in docker_calls
    assert "image rm " not in docker_calls
    assert (result.docker_state / "legacy-created").exists()
    assert (result.docker_state / "legacy-created.identity").read_text(
        encoding="utf-8"
    ).strip() == f"sha256:{'3' * 64}"


# Production break caught: Docker tag overwrites a target that appears after an
# earlier absence check. The publisher must recheck under its canonical lock.
def test_rollback_baseline_publisher_does_not_overwrite_a_racing_local_tag(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_scenario="rollback-tag-overwrite-race",
    )

    assert result.completed.returncode == 2
    assert result.completed.stderr.endswith("BLOCKED_ROLLBACK_TAG_EXISTS\n")
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert f"tag {ROLLBACK_IMAGE_ID} {ROLLBACK_TAG}" not in docker_calls
    assert "image rm " not in docker_calls
    assert (result.docker_state / "rollback-created.identity").read_text(
        encoding="utf-8"
    ).strip() == f"sha256:{'3' * 64}"


# Production break caught: a signal can arrive after Docker writes a cidfile but
# before the shell records it. Cleanup must recover the immutable ID from cidfile.
def test_rollback_baseline_publisher_recovers_cidfile_ownership_on_signal(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(
        tmp_path,
        docker_scenario="container-signal-after-cidfile",
    )

    assert result.completed.returncode == 2
    assert not (result.docker_state / "owned-container").exists()
    docker_calls = result.docker_log.read_text(encoding="utf-8")
    assert f"rm -f {ROLLBACK_CONTAINER_ID}" in docker_calls
    assert "image rm " not in docker_calls
    assert (result.docker_state / "legacy-created").exists()


@pytest.mark.parametrize(
    ("docker_scenario", "competitor_marker"),
    (
        ("legacy-replaced-before-cleanup", "legacy-created"),
        ("rollback-replaced-before-cleanup", "rollback-created"),
    ),
)
def test_rollback_baseline_publisher_retains_racing_local_tags_during_cleanup(
    tmp_path: Path,
    docker_scenario: str,
    competitor_marker: str,
) -> None:
    result = _run_rollback_publisher(tmp_path, docker_scenario=docker_scenario)

    assert result.completed.returncode == 2
    assert result.completed.stdout == ""
    assert result.completed.stderr.endswith("BLOCKED_RETAINED_LOCAL_TAG_MISMATCH\n")
    assert (result.docker_state / competitor_marker).exists()
    assert "image rm " not in result.docker_log.read_text(encoding="utf-8")


def test_rollback_baseline_publisher_rechecks_remote_immediately_before_push(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, docker_scenario="remote-prepush-race")

    assert result.completed.returncode == 2
    assert result.completed.stderr.endswith("BLOCKED_REMOTE_IMAGE_MISMATCH\n")
    assert f"push {ROLLBACK_TAG}" not in result.docker_log.read_text(encoding="utf-8")


def test_rollback_baseline_publisher_accepts_no_positional_input(
    tmp_path: Path,
) -> None:
    result = _run_rollback_publisher(tmp_path, arguments=("unexpected",))

    assert result.completed.returncode == 64
    assert result.completed.stdout == ""
    assert result.completed.stderr == "usage: publish-rollback-baseline.sh\n"
    assert not result.gate_log.exists()
    assert not result.docker_log.exists()


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


def _deploy_wrapper_fixture(
    tmp_path: Path,
    *,
    nas_architecture: str,
    image_platform: str,
    docker_info_output: str | None = None,
    stateless_beta: bool = False,
    include_migration: bool = True,
) -> tuple[Path, dict[str, str], Path, Path, Path]:
    base = tmp_path / "nas/docker/seoul-education-travel-map"
    base.mkdir(parents=True)
    (base / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    if stateless_beta:
        (base / "runtime.env").write_text("STATELESS_BETA=1\n", encoding="ascii")
        (base / "runtime.env").chmod(0o600)
    image_env = base / "image.env"
    image_env.write_text(
        "TRAVEL_MAP_MANIFEST_DIGEST=" + "b" * 64 + "\n", encoding="utf-8"
    )
    image_env.chmod(0o600)

    events_path = tmp_path / "docker-events"
    migration_events_path = tmp_path / "migration-events"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
printf 'docker %s\\n' "$*" >> "$FAKE_DOCKER_EVENTS"
case "${1-} ${2-}" in
    info[[:space:]]*) printf '%s' "$FAKE_DOCKER_INFO_OUTPUT" ;;
    image[[:space:]]inspect) printf '%s\\n' "$FAKE_DOCKER_IMAGE_PLATFORM" ;;
    pull[[:space:]]*) : ;;
    compose[[:space:]]*) : ;;
    *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        """#!/bin/sh
set -eu
printf 'mktemp %s\\n' "$*" >> "$FAKE_DOCKER_EVENTS"
exec /usr/bin/mktemp "$@"
""",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)

    if include_migration:
        migration = base / "migrate-user-database.sh"
        migration.write_text(
            """#!/bin/sh
set -eu
printf 'migration %s\\n' "$1" >> "$FAKE_MIGRATION_EVENTS"
""",
            encoding="utf-8",
        )
        migration.chmod(0o755)

    deploy = tmp_path / "deploy-reviewed-image.sh"
    deploy.write_text(
        (Path(__file__).resolve().parents[1] / "deploy/nas/deploy-reviewed-image.sh")
        .read_text(encoding="utf-8")
        .replace(
            "base=/volume1/docker/seoul-education-travel-map",
            f"base={base}",
        ),
        encoding="utf-8",
    )
    deploy.chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + ":" + environment.get("PATH", "")
    environment["FAKE_DOCKER_ARCHITECTURE"] = nas_architecture
    environment["FAKE_DOCKER_INFO_OUTPUT"] = (
        nas_architecture if docker_info_output is None else docker_info_output
    )
    environment["FAKE_DOCKER_IMAGE_PLATFORM"] = image_platform
    environment["FAKE_DOCKER_EVENTS"] = str(events_path)
    environment["FAKE_MIGRATION_EVENTS"] = str(migration_events_path)

    return deploy, environment, events_path, migration_events_path, base


def _read_test_event_log(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _run_rollback_publisher(
    tmp_path: Path,
    *,
    git_state: str = "clean",
    link_source: bool = False,
    docker_directory_mode: int = 0o700,
    docker_config_mode: int = 0o600,
    docker_scenario: str = "success",
    arguments: tuple[str, ...] = (),
    inject_ambient_controls: bool = False,
    require_hardened_git: bool = False,
    require_no_replace_objects: bool = False,
) -> SimpleNamespace:
    # Unit 4 deliberately starts RED: keep the missing tracked publisher as a
    # controlled process result so every historical contract test fails by
    # assertion, never by fixture/setup error.
    if not ROLLBACK_PUBLISH.is_file():
        return SimpleNamespace(
            completed=subprocess.CompletedProcess(
                ["/bin/sh", str(ROLLBACK_PUBLISH), *arguments],
                127,
                "",
                "BLOCKED_MISSING_ROLLBACK_PUBLISHER\n",
            ),
            source=tmp_path / "rollback-source-real",
            configured_source=tmp_path / "rollback-source-real",
            gate_log=tmp_path / "gate.log",
            docker_log=tmp_path / "docker.log",
            docker_state=tmp_path / "docker-state",
            temporary_root=tmp_path / "temporary",
            python_injection_marker=tmp_path / "python-injection-ran",
        )
    canonical_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    production_safe_path = (
        f"{canonical_home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
        "/usr/bin:/bin:/usr/sbin:/sbin"
    )
    gate_log = tmp_path / "gate.log"
    docker_log = tmp_path / "docker.log"
    docker_state = tmp_path / "docker-state"
    docker_state.mkdir()
    source = tmp_path / "rollback-source-real"
    gate = source / "apps/travel-map/scripts/release-gate.sh"
    gate.parent.mkdir(parents=True)
    tracked_app = source / "apps/travel-map/app/main.py"
    tracked_app.parent.mkdir(parents=True)
    tracked_app.write_text("# tracked app fixture\n", encoding="utf-8")
    if git_state == "ignored-allowed-suffix":
        injected = source / "apps/travel-map/app/static/injected.js"
        injected.parent.mkdir(parents=True)
        injected.write_text("globalThis.injected = true;\n", encoding="utf-8")
    if git_state == "ignored-gate-input":
        ignored_inputs = {
            "apps/travel-map/tests/conftest.py": "raise RuntimeError('injected')\n",
            "apps/travel-map/e2e/injected.spec.ts": "throw new Error('injected');\n",
            "apps/travel-map/.venv/lib/python/sitecustomize.py": (
                "raise RuntimeError('injected')\n"
            ),
            "apps/travel-map/node_modules/injected/package.json": "{}\n",
            ".git/config": "[core]\n\tfsmonitor = injected\n",
        }
        for relative_path, payload in ignored_inputs.items():
            candidate = source / relative_path
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(payload, encoding="utf-8")
    configured_source = source
    if link_source:
        configured_source = tmp_path / "rollback-source-link"
        configured_source.symlink_to(source, target_is_directory=True)

    docker_config = tmp_path / "docker-config"
    docker_config.mkdir(mode=docker_directory_mode)
    docker_config.chmod(docker_directory_mode)
    docker_configuration = docker_config / "config.json"
    docker_configuration.write_text(
        '{"auths":{"ghcr.io":{"auth":"test-only-docker-auth-marker"}}}\n',
        encoding="utf-8",
    )
    docker_configuration.chmod(docker_config_mode)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    test_safe_path = f"{fake_bin}:{production_safe_path}"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        _fake_rollback_git(
            git_state,
            canonical_home,
            test_safe_path,
            require_hardened_git,
            require_no_replace_objects,
        ),
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        _fake_rollback_docker(
            docker_log,
            docker_state,
            docker_scenario,
            canonical_home,
            test_safe_path,
            docker_config,
        ),
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    gate.write_text(
        _fake_rollback_release_gate(
            gate_log,
            docker_log,
            docker_state,
            canonical_home,
            test_safe_path,
            docker_scenario,
        ),
        encoding="utf-8",
    )
    gate.chmod(0o755)

    publisher_under_test = tmp_path / "publish-rollback-baseline.sh"
    publisher_source = ROLLBACK_PUBLISH.read_text(encoding="utf-8")
    safe_path_anchor = (
        "safe_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
        "/usr/bin:/bin:/usr/sbin:/sbin"
    )
    assert publisher_source.count(safe_path_anchor) == 1
    publisher_under_test.write_text(
        publisher_source.replace(
            safe_path_anchor,
            f"safe_path={shlex.quote(str(fake_bin))}:$canonical_home/.local/bin:"
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        ),
        encoding="utf-8",
    )
    publisher_under_test.chmod(0o755)

    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    python_injection = tmp_path / "python-injection"
    python_injection.mkdir()
    python_injection_marker = tmp_path / "python-injection-ran"
    (python_injection / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(python_injection_marker)!r}).write_text('injected')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "ROLLBACK_SOURCE_DIRECTORY": str(configured_source),
            "DOCKER_CONFIG": str(docker_config),
            "TMPDIR": str(temporary_root),
            "KAKAO_JAVASCRIPT_KEY": "must-be-unset",
            "NEIS_API_KEY": "must-be-unset",
            "KINDERGARTEN_API_KEY": "must-be-unset",
            "PYTEST_ADDOPTS": "--collect-only",
            "PYTEST_PLUGINS": "must_not_be_imported",
            "PYTHONPATH": str(python_injection),
        }
    )
    if inject_ambient_controls:
        environment.update(
            {
                "HOME": str(tmp_path / "ambient-home-must-not-be-used"),
                "GIT_DIR": str(tmp_path / "ambient-git-dir"),
                "GIT_WORK_TREE": str(tmp_path / "ambient-work-tree"),
                "GIT_INDEX_FILE": str(tmp_path / "ambient-index"),
                "GIT_CONFIG_GLOBAL": str(tmp_path / "ambient-global-config"),
                "GIT_CONFIG_SYSTEM": str(tmp_path / "ambient-system-config"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(tmp_path / "ambient-hooks"),
                "DOCKER_HOST": "tcp://127.0.0.1:1",
                "DOCKER_CONTEXT": "ambient-context",
                "DOCKER_CERT_PATH": str(tmp_path / "ambient-certificates"),
                "DOCKER_TLS_VERIFY": "1",
                "BUILDX_CONFIG": str(tmp_path / "ambient-buildx"),
            }
        )
    completed = subprocess.run(
        ["/bin/sh", str(publisher_under_test), *arguments],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return SimpleNamespace(
        completed=completed,
        source=source,
        configured_source=configured_source,
        gate_log=gate_log,
        docker_log=docker_log,
        docker_state=docker_state,
        temporary_root=temporary_root,
        python_injection_marker=python_injection_marker,
    )


def _fake_rollback_release_gate(
    gate_log: Path,
    docker_log: Path,
    docker_state: Path,
    canonical_home: Path,
    expected_gate_path: str,
    docker_scenario: str,
) -> str:
    gate_log_path = shlex.quote(str(gate_log))
    legacy_state_path = shlex.quote(str(docker_state / "legacy-created"))
    uv_cache_path = shlex.quote(str(canonical_home / ".cache/uv"))
    playwright_path = shlex.quote(str(canonical_home / "Library/Caches/ms-playwright"))
    gate_path = shlex.quote(expected_gate_path)
    scenario = shlex.quote(docker_scenario)
    return f"""#!/bin/sh
set -eu
umask 077
[ -z "${{RELEASE_GATE_IMAGE_RECORD:-}}" ] || exit 97
[ -z "${{KAKAO_JAVASCRIPT_KEY:-}}${{NEIS_API_KEY:-}}${{KINDERGARTEN_API_KEY:-}}" ] \
    || exit 98
[ -z "${{PYTEST_ADDOPTS:-}}${{PYTEST_PLUGINS:-}}${{PYTHONPATH:-}}" ] || exit 99
[ "$PATH" = {gate_path} ] || exit 100
private_root=${{TMPDIR%/release-gate-tmp}}
[ "$HOME" = "$private_root/release-gate-home" ] || exit 101
[ "$XDG_CONFIG_HOME" = "$HOME/xdg-config" ] || exit 102
[ "$XDG_CACHE_HOME" = "$HOME/xdg-cache" ] || exit 103
[ "$XDG_DATA_HOME" = "$HOME/xdg-data" ] || exit 104
[ "$NPM_CONFIG_CACHE" = "$HOME/npm-cache" ] || exit 105
[ "$NPM_CONFIG_STORE_DIR" = "$HOME/pnpm-store" ] || exit 106
[ "$UV_CACHE_DIR" = {uv_cache_path} ] || exit 107
[ "$PLAYWRIGHT_BROWSERS_PATH" = {playwright_path} ] || exit 108
for forbidden in \
    .git \
    apps/travel-map/tests/conftest.py \
    apps/travel-map/e2e/injected.spec.ts \
    apps/travel-map/.venv/lib/python/sitecustomize.py \
    apps/travel-map/node_modules/injected/package.json
do
    [ ! -e "$forbidden" ] || exit 109
done
gate_mode=$(stat -c '%a' "$0" 2>/dev/null || stat -f '%Lp' "$0")
main_mode=$(stat -c '%a' apps/travel-map/app/main.py 2>/dev/null \
    || stat -f '%Lp' apps/travel-map/app/main.py)
[ "$gate_mode" = 755 ] && [ "$main_mode" = 644 ] || exit 110
printf '%s\\n' "$CI|$PYTHONWARNINGS|$DOCKER_DEFAULT_PLATFORM|$NAS_PLATFORM|$#|$(pwd -P)" >> {gate_log_path}
if [ {scenario} = legacy-build-race ]; then
    printf '%s\\n' 'sha256:{"3" * 64}' > {legacy_state_path}.identity
    : > {legacy_state_path}
    exit 1
fi
snapshot_id=fake-snapshot
context_root=fake-release-context
docker build --build-arg SNAPSHOT_ID="$snapshot_id" -t seoul-education-travel-map:0.1.0 "$context_root"
[ {scenario} != legacy-retag-after-gate ] \\
    || printf '%s\\n' 'sha256:{"3" * 64}' > {legacy_state_path}.identity
printf '%s\\n' 'RELEASE_GATE_TRANSCRIPT'
"""


def _fake_rollback_git(
    git_state: str,
    canonical_home: Path,
    expected_path: str,
    require_hardened_git: bool,
    require_no_replace_objects: bool,
) -> str:
    state = shlex.quote(git_state)
    home = shlex.quote(str(canonical_home))
    path = shlex.quote(expected_path)
    hardened = 1 if require_hardened_git else 0
    no_replace = 1 if require_no_replace_objects else 0
    return f"""#!/bin/sh
set -eu
[ "$PATH" = {path} ] || exit 80
[ "$HOME" = {home} ] || exit 81
[ "${{GIT_CONFIG_GLOBAL:-}}" = /dev/null ] || exit 82
[ "${{GIT_CONFIG_NOSYSTEM:-}}" = 1 ] || exit 83
[ -z "${{GIT_DIR:-}}${{GIT_WORK_TREE:-}}${{GIT_INDEX_FILE:-}}" ] || exit 84
[ -z "${{GIT_CONFIG_SYSTEM:-}}${{GIT_CONFIG_COUNT:-}}" ] || exit 85
[ -z "${{GIT_CONFIG_KEY_0:-}}${{GIT_CONFIG_VALUE_0:-}}" ] || exit 86
FAKE_GIT_STATE={state}
REQUIRE_HARDENED_GIT={hardened}
REQUIRE_NO_REPLACE_OBJECTS={no_replace}
[ "$REQUIRE_NO_REPLACE_OBJECTS" -eq 0 ] \
    || [ "${{GIT_NO_REPLACE_OBJECTS:-}}" = 1 ] \
    || exit 89
[ "$1" = '-C' ] || exit 90
source_directory=$2
shift 2
fsmonitor_fixed=0
untracked_cache_fixed=0
while [ "${{1:-}}" = '-c' ]; do
    case "${{2:-}}" in
        core.fsmonitor=false) fsmonitor_fixed=1 ;;
        core.untrackedCache=false) untracked_cache_fixed=1 ;;
    esac
    shift 2
done
if [ "$REQUIRE_HARDENED_GIT" -eq 1 ]; then
    [ "$fsmonitor_fixed" -eq 1 ] || exit 87
    [ "$untracked_cache_fixed" -eq 1 ] || exit 88
fi
case "$1:$2" in
    rev-parse:--show-toplevel)
        printf '%s\\n' "$source_directory"
        ;;
    rev-parse:HEAD)
        if [ "$FAKE_GIT_STATE" = 'wrong-sha' ]; then
            printf '%s\\n' '{"a" * 40}'
        else
            printf '%s\\n' '{ROLLBACK_SHA}'
        fi
        ;;
    rev-parse:--is-inside-work-tree)
        printf '%s\\n' 'true'
        ;;
    symbolic-ref:--quiet)
        [ "$FAKE_GIT_STATE" = 'attached' ] && exit 0
        exit 1
        ;;
    status:--porcelain=v1)
        [ "$FAKE_GIT_STATE" = 'dirty' ] && printf '%s\\n' ' M tracked-file'
        exit 0
        ;;
    ls-files:-v)
        case "$FAKE_GIT_STATE" in
            assume-unchanged) prefix=h ;;
            skip-worktree) prefix=S ;;
            *) prefix=H ;;
        esac
        printf '%s\\n' "$prefix apps/travel-map/Dockerfile"
        ;;
    ls-tree:-r)
        case "$*" in
            *--name-only*)
                printf 'apps/travel-map/app/main.py\\0'
                ;;
            *)
                gate_id=$(/usr/bin/git hash-object \
                    "$source_directory/apps/travel-map/scripts/release-gate.sh")
                main_id=$(/usr/bin/git hash-object \
                    "$source_directory/apps/travel-map/app/main.py")
                printf '100755 blob %s\\tapps/travel-map/scripts/release-gate.sh\\0' \
                    "$gate_id"
                printf '100644 blob %s\\tapps/travel-map/app/main.py\\0' "$main_id"
                ;;
        esac
        ;;
    archive:--format=tar)
        case "${{3:-}}" in
            --output=*) archive_output=${{3#--output=}} ;;
            *) exit 92 ;;
        esac
        [ "${{4:-}}" = '{ROLLBACK_SHA}' ] || exit 93
        archive_stage=$archive_output.stage
        mkdir -p "$archive_stage/apps/travel-map/scripts" \
            "$archive_stage/apps/travel-map/app"
        /bin/cp -p "$source_directory/apps/travel-map/scripts/release-gate.sh" \
            "$archive_stage/apps/travel-map/scripts/release-gate.sh"
        /bin/cp -p "$source_directory/apps/travel-map/app/main.py" \
            "$archive_stage/apps/travel-map/app/main.py"
        chmod 0775 "$archive_stage/apps/travel-map/scripts/release-gate.sh"
        chmod 0664 "$archive_stage/apps/travel-map/app/main.py"
        COPYFILE_DISABLE=1 /usr/bin/tar -cf "$archive_output" \
            -C "$archive_stage" apps
        ;;
    *)
        exit 91
        ;;
esac
"""


def _fake_rollback_docker(
    docker_log: Path,
    docker_state: Path,
    docker_scenario: str,
    canonical_home: Path,
    expected_path: str,
    docker_config: Path,
) -> str:
    docker_log_path = shlex.quote(str(docker_log))
    docker_state_path = shlex.quote(str(docker_state))
    scenario = shlex.quote(docker_scenario)
    home = shlex.quote(str(canonical_home))
    path = shlex.quote(expected_path)
    config = shlex.quote(str(docker_config))
    return f"""#!/bin/sh
set -eu
[ "$PATH" = {path} ] || exit 80
case "$HOME" in
    {home}|*/release-gate-home) ;;
    *) exit 81 ;;
esac
[ "$DOCKER_CONFIG" = {config} ] || exit 82
[ -z "${{DOCKER_HOST:-}}${{DOCKER_CONTEXT:-}}${{DOCKER_CERT_PATH:-}}" ] || exit 83
[ -z "${{DOCKER_TLS_VERIFY:-}}${{BUILDX_CONFIG:-}}" ] || exit 84
FAKE_DOCKER_LOG={docker_log_path}
FAKE_DOCKER_STATE_DIRECTORY={docker_state_path}
FAKE_DOCKER_SCENARIO={scenario}
mkdir -p "$FAKE_DOCKER_STATE_DIRECTORY"
printf '%s' "$1" >> "$FAKE_DOCKER_LOG"
shift
for argument in "$@"; do
    printf ' %s' "$argument" >> "$FAKE_DOCKER_LOG"
done
printf '\\n' >> "$FAKE_DOCKER_LOG"
built_image_id='{ROLLBACK_IMAGE_ID}'
image_id="$built_image_id"
competitor_id='sha256:{"3" * 64}'
container_id='{ROLLBACK_CONTAINER_ID}'
legacy_tag='{ROLLBACK_LEGACY_TAG}'
rollback_tag='{ROLLBACK_TAG}'
repo_digest='{ROLLBACK_REGISTRY}@{ROLLBACK_MANIFEST}'
platform_manifest='{ROLLBACK_REGISTRY}@{ROLLBACK_PLATFORM_MANIFEST}'
config_digest='{ROLLBACK_CONFIG_DIGEST}'
[ "$FAKE_DOCKER_SCENARIO" != 'legacy-retag-after-gate' ] \
    || image_id="$competitor_id"
if [ "$FAKE_DOCKER_SCENARIO" = 'legacy-retag-after-gate' ]; then
    rollback_tag='{ROLLBACK_REGISTRY}:rollback-baseline-{ROLLBACK_SHA}-'${{image_id#sha256:}}
fi
case "$FAKE_DOCKER_SCENARIO" in
    containerd-index|containerd-extra-runnable|containerd-unlinked-attestation|containerd-zero-attestation|containerd-multiple-attestations|containerd-attestation-missing|containerd-attestation-not-attestation|containerd-malformed-child-config|containerd-invalid-child-config-descriptor)
        repo_digest='{ROLLBACK_REGISTRY}@{ROLLBACK_IMAGE_ID}'
        ;;
esac
case "${{1-}}" in
    '') : ;;
esac
command_name=$(sed -n '$p' "$FAKE_DOCKER_LOG" | cut -d ' ' -f 1)
case "$command_name" in
    version)
        exit 0
        ;;
    image)
        subcommand=$1
        shift
        case "$subcommand" in
            ls)
                case "$*" in
                    *"reference=$legacy_tag"*)
                        if [ "$FAKE_DOCKER_SCENARIO" = 'local-existing' ] \\
                            || [ -f "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created" ]; then
                            if [ -f "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created.identity" ]; then
                                sed -n '1p' "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created.identity"
                            else
                                printf '%s\\n' "$image_id"
                            fi
                        fi
                        ;;
                    *"reference=$rollback_tag"*)
                        if [ "$FAKE_DOCKER_SCENARIO" = \
                            'rollback-tag-overwrite-race' ]; then
                            rollback_lookup_count_file="$FAKE_DOCKER_STATE_DIRECTORY/rollback-local-lookup-count"
                            rollback_lookup_count=0
                            [ ! -f "$rollback_lookup_count_file" ] \
                                || rollback_lookup_count=$(sed -n '1p' \
                                    "$rollback_lookup_count_file")
                            rollback_lookup_count=$((rollback_lookup_count + 1))
                            printf '%s\\n' "$rollback_lookup_count" \
                                > "$rollback_lookup_count_file"
                            if [ "$rollback_lookup_count" -ge 2 ]; then
                                printf '%s\\n' "$competitor_id" \
                                    > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
                                : > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created"
                            fi
                        fi
                        if [ "$FAKE_DOCKER_SCENARIO" = 'local-rollback-existing' ] \\
                            || [ -f "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created" ]; then
                            if [ -f "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity" ]; then
                                sed -n '1p' "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
                            else
                                printf '%s\\n' "$image_id"
                            fi
                        fi
                        ;;
                esac
                exit 0
                ;;
            inspect)
                inspected_reference=
                for inspected_argument in "$@"; do
                    inspected_reference=$inspected_argument
                done
                case "$inspected_reference" in
                    "$legacy_tag"|"$built_image_id"|"$image_id")
                        [ -f "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created" ] \
                            || exit 98
                        ;;
                    "$rollback_tag")
                        [ -f "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created" ] \
                            || exit 98
                        ;;
                    *) exit 98 ;;
                esac
                case "$*" in
                    *RepoDigests*) printf '%s\\n' "$repo_digest" ;;
                    *)
                        platform='linux/amd64'
                        inspected_id="$image_id"
                        if [ "$FAKE_DOCKER_SCENARIO" = \
                            'rollback-retag-during-cleanup' ] \\
                            && [ "$inspected_reference" = "$rollback_tag" ]; then
                            rollback_inspect_count_file="$FAKE_DOCKER_STATE_DIRECTORY/rollback-inspect-count"
                            rollback_inspect_count=0
                            [ ! -f "$rollback_inspect_count_file" ] \
                                || rollback_inspect_count=$(sed -n '1p' \
                                    "$rollback_inspect_count_file")
                            rollback_inspect_count=$((rollback_inspect_count + 1))
                            printf '%s\\n' "$rollback_inspect_count" \
                                > "$rollback_inspect_count_file"
                            if [ "$rollback_inspect_count" -ge 3 ]; then
                                printf '%s\\n' "$competitor_id" \
                                    > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
                            fi
                        fi
                        case "$inspected_reference" in
                            "$built_image_id")
                                inspected_id="$built_image_id"
                                ;;
                            "$legacy_tag")
                                inspected_id=$(sed -n '1p' \
                                    "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created.identity")
                                ;;
                            "$rollback_tag")
                                inspected_id=$(sed -n '1p' \
                                    "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity")
                                ;;
                        esac
                        [ "$FAKE_DOCKER_SCENARIO" = 'local-platform-mismatch' ] \\
                            && platform='linux/arm64'
                        case "$FAKE_DOCKER_SCENARIO:$*" in
                            tagged-id-mismatch:*"$rollback_tag"*)
                                inspected_id='sha256:{"3" * 64}'
                                ;;
                        esac
                        case "$*" in
                            *'{{{{.Id}}}} {{{{.Os}}}}/{{{{.Architecture}}}}'*)
                                printf '%s %s\\n' "$inspected_id" "$platform"
                                ;;
                            *'{{{{.Id}}}}'*) printf '%s\\n' "$inspected_id" ;;
                            *) exit 99 ;;
                        esac
                        ;;
                esac
                ;;
            rm)
                [ "$FAKE_DOCKER_SCENARIO" = 'cleanup-failure' ] && exit 1
                case "$*" in
                    *"$legacy_tag"*)
                        rm -f "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created" \
                            "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created.identity"
                        ;;
                    *"$rollback_tag"*)
                        if [ "$FAKE_DOCKER_SCENARIO" = \
                            'rollback-retag-during-cleanup' ]; then
                            printf '%s\\n' "$competitor_id" \
                                > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
                            : > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created"
                        fi
                        rm -f "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created" \
                            "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
                        ;;
                esac
                exit 0
                ;;
            *) exit 92 ;;
        esac
        ;;
    build)
        [ "$1" = '--iidfile' ] || exit 97
        build_iid_file=$2
        shift 2
        [ "$*" = "--build-arg SNAPSHOT_ID=fake-snapshot -t $legacy_tag fake-release-context" ] \
            || exit 97
        printf '%s\\n' "$built_image_id" > "$build_iid_file"
        chmod 0600 "$build_iid_file"
        printf '%s\\n' "$built_image_id" \
            > "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created.identity"
        : > "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created"
        ;;
    buildx)
        [ "$1" = 'imagetools' ] && [ "$2" = 'inspect' ] || exit 93
        case "$*" in
            *"$rollback_tag"*)
                lookup_count_file="$FAKE_DOCKER_STATE_DIRECTORY/remote-lookup-count"
                lookup_count=0
                [ ! -f "$lookup_count_file" ] \
                    || lookup_count=$(sed -n '1p' "$lookup_count_file")
                lookup_count=$((lookup_count + 1))
                printf '%s\\n' "$lookup_count" > "$lookup_count_file"
                remote_present=0
                case "$FAKE_DOCKER_SCENARIO" in
                    remote-ambiguous)
                        printf '%s\\n' 'ERROR: request failed: network timeout' >&2
                        exit 1
                        ;;
                    remote-helper-missing)
                        printf '%s\\n' \\
                            "ERROR: $rollback_tag: credential helper executable not found" >&2
                        exit 1
                        ;;
                    remote-referenced-manifest-missing)
                        printf '%s\\n' \\
                            "ERROR: $rollback_tag: referenced manifest sha256:{"7" * 64} not found" >&2
                        exit 1
                        ;;
                    remote-existing|remote-existing-identical)
                        remote_present=1
                        ;;
                    remote-prepush-race)
                        [ "$lookup_count" -lt 2 ] || remote_present=1
                        ;;
                    remote-malformed-descriptor)
                        printf '%s\\n' '[]'
                        exit 0
                        ;;
                    exact-top-level-not-found)
                        if [ ! -f "$FAKE_DOCKER_STATE_DIRECTORY/remote-pushed" ]; then
                            printf '%s\\n' "ERROR: $rollback_tag: not found" >&2
                            exit 1
                        fi
                        remote_present=1
                        ;;
                    *)
                        [ ! -f "$FAKE_DOCKER_STATE_DIRECTORY/remote-pushed" ] \\
                            || remote_present=1
                        ;;
                esac
                if [ "$remote_present" -eq 0 ]; then
                    printf '%s\\n' 'ERROR: manifest unknown: not found' >&2
                    exit 1
                fi
                case "$FAKE_DOCKER_SCENARIO" in
                    containerd-index|containerd-extra-runnable|containerd-unlinked-attestation|containerd-zero-attestation|containerd-multiple-attestations|containerd-attestation-missing|containerd-attestation-not-attestation|containerd-malformed-child-config|containerd-invalid-child-config-descriptor)
                        remote_digest="$image_id"
                        remote_media_type='{ROLLBACK_INDEX_MEDIA_TYPE}'
                        ;;
                    *)
                        remote_digest='{ROLLBACK_MANIFEST}'
                        remote_media_type='{ROLLBACK_MANIFEST_MEDIA_TYPE}'
                        ;;
                esac
                remote_size=1234
                case "$FAKE_DOCKER_SCENARIO" in
                    remote-descriptor-media-type-invalid)
                        remote_media_type='application/json'
                        ;;
                    remote-descriptor-size-zero)
                        remote_size=0
                        ;;
                    remote-descriptor-kind-mismatch)
                        remote_media_type='{ROLLBACK_INDEX_MEDIA_TYPE}'
                        ;;
                esac
                printf '{{"mediaType":"%s","digest":"%s","size":%s}}\\n' \\
                    "$remote_media_type" "$remote_digest" "$remote_size"
                ;;
            *"$platform_manifest"*)
                case "$*" in
                    *--raw*)
                        if [ "$FAKE_DOCKER_SCENARIO" = \\
                            'containerd-malformed-child-config' ]; then
                            printf '%s\\n' '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":[]}}'
                        elif [ "$FAKE_DOCKER_SCENARIO" = \\
                            'containerd-invalid-child-config-descriptor' ]; then
                            printf '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":{{"digest":"%s"}}}}\\n' \\
                                "$config_digest"
                        else
                            printf '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":{{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"%s","size":222}}}}\\n' \\
                                "$config_digest"
                        fi
                        ;;
                    *--format*)
                        printf '%s\\n' '{{"architecture":"amd64","os":"linux"}}'
                        ;;
                    *) exit 94 ;;
                esac
                ;;
            *'{ROLLBACK_REGISTRY}@{ROLLBACK_ATTESTATION_MANIFEST}'*)
                [ "$FAKE_DOCKER_SCENARIO" != 'containerd-attestation-missing' ] \
                    || exit 94
                if [ "$FAKE_DOCKER_SCENARIO" = \
                    'containerd-attestation-not-attestation' ]; then
                    printf '%s\n' \
                        '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":{{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"{ROLLBACK_ATTESTATION_CONFIG}","size":100}},"layers":[{{"mediaType":"application/vnd.oci.image.layer.v1.tar+gzip","digest":"{ROLLBACK_ATTESTATION_LAYER}","size":101}}]}}'
                else
                    printf '%s\n' \
                        '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":{{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"{ROLLBACK_ATTESTATION_CONFIG}","size":100}},"layers":[{{"mediaType":"application/vnd.in-toto+json","digest":"{ROLLBACK_ATTESTATION_LAYER}","size":101,"annotations":{{"in-toto.io/predicate-type":"https://slsa.dev/provenance/v0.2"}}}}]}}'
                fi
                ;;
            *'{ROLLBACK_REGISTRY}@{ROLLBACK_SECOND_ATTESTATION_MANIFEST}'*)
                printf '%s\n' \
                    '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":{{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"{ROLLBACK_ATTESTATION_CONFIG}","size":100}},"layers":[{{"mediaType":"application/vnd.in-toto+json","digest":"{ROLLBACK_ATTESTATION_LAYER}","size":101,"annotations":{{"in-toto.io/predicate-type":"https://spdx.dev/Document"}}}}]}}'
                ;;
            *"$repo_digest"*)
                case "$FAKE_DOCKER_SCENARIO" in
                    containerd-extra-runnable)
                        printf '%s\\n' \\
                            '{{"schemaVersion":2,"mediaType":"{ROLLBACK_INDEX_MEDIA_TYPE}","manifests":[{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_PLATFORM_MANIFEST}","size":111,"platform":{{"architecture":"amd64","os":"linux"}}}},{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_EXTRA_PLATFORM_MANIFEST}","size":112,"platform":{{"architecture":"arm64","os":"linux"}}}},{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_ATTESTATION_MANIFEST}","size":113,"annotations":{{"vnd.docker.reference.digest":"{ROLLBACK_PLATFORM_MANIFEST}","vnd.docker.reference.type":"attestation-manifest"}},"platform":{{"architecture":"unknown","os":"unknown"}}}}]}}'
                        ;;
                    containerd-unlinked-attestation)
                        printf '%s\\n' \\
                            '{{"schemaVersion":2,"mediaType":"{ROLLBACK_INDEX_MEDIA_TYPE}","manifests":[{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_PLATFORM_MANIFEST}","size":111,"platform":{{"architecture":"amd64","os":"linux"}}}},{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_ATTESTATION_MANIFEST}","size":113,"annotations":{{"vnd.docker.reference.digest":"{ROLLBACK_EXTRA_PLATFORM_MANIFEST}","vnd.docker.reference.type":"attestation-manifest"}},"platform":{{"architecture":"unknown","os":"unknown"}}}}]}}'
                        ;;
                    containerd-zero-attestation)
                        printf '%s\n' \
                            '{{"schemaVersion":2,"mediaType":"{ROLLBACK_INDEX_MEDIA_TYPE}","manifests":[{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_PLATFORM_MANIFEST}","size":111,"platform":{{"architecture":"amd64","os":"linux"}}}}]}}'
                        ;;
                    containerd-multiple-attestations)
                        printf '%s\n' \
                            '{{"schemaVersion":2,"mediaType":"{ROLLBACK_INDEX_MEDIA_TYPE}","manifests":[{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_PLATFORM_MANIFEST}","size":111,"platform":{{"architecture":"amd64","os":"linux"}}}},{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_ATTESTATION_MANIFEST}","size":113,"annotations":{{"vnd.docker.reference.digest":"{ROLLBACK_PLATFORM_MANIFEST}","vnd.docker.reference.type":"attestation-manifest"}},"platform":{{"architecture":"unknown","os":"unknown"}}}},{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_SECOND_ATTESTATION_MANIFEST}","size":114,"annotations":{{"vnd.docker.reference.digest":"{ROLLBACK_PLATFORM_MANIFEST}","vnd.docker.reference.type":"attestation-manifest"}},"platform":{{"architecture":"unknown","os":"unknown"}}}}]}}'
                        ;;
                    containerd-index|containerd-index-id-mismatch|containerd-attestation-missing|containerd-attestation-not-attestation|containerd-malformed-child-config|containerd-invalid-child-config-descriptor)
                        printf '%s\\n' \\
                            '{{"schemaVersion":2,"mediaType":"{ROLLBACK_INDEX_MEDIA_TYPE}","manifests":[{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_PLATFORM_MANIFEST}","size":111,"platform":{{"architecture":"amd64","os":"linux"}}}},{{"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","digest":"{ROLLBACK_ATTESTATION_MANIFEST}","size":113,"annotations":{{"vnd.docker.reference.digest":"{ROLLBACK_PLATFORM_MANIFEST}","vnd.docker.reference.type":"attestation-manifest"}},"platform":{{"architecture":"unknown","os":"unknown"}}}}]}}'
                        ;;
                    *)
                        case "$*" in
                            *--raw*)
                                if [ "$FAKE_DOCKER_SCENARIO" = \
                                    'remote-malformed-config-shape' ]; then
                                    printf '%s\\n' '{{"schemaVersion":2,"mediaType":"{ROLLBACK_MANIFEST_MEDIA_TYPE}","config":[]}}'
                                    exit 0
                                fi
                                remote_id="$image_id"
                                case "$FAKE_DOCKER_SCENARIO" in
                                    remote-config-mismatch|remote-existing|remote-race|remote-prepush-race)
                                        remote_id='sha256:{"3" * 64}'
                                        ;;
                                esac
                                raw_media_type='{ROLLBACK_MANIFEST_MEDIA_TYPE}'
                                [ "$FAKE_DOCKER_SCENARIO" != \\
                                    'remote-raw-media-type-mismatch' ] \\
                                    || raw_media_type='application/vnd.docker.distribution.manifest.v2+json'
                                if [ "$FAKE_DOCKER_SCENARIO" = \\
                                    'remote-invalid-config-descriptor' ]; then
                                    printf '{{"schemaVersion":2,"mediaType":"%s","config":{{"digest":"%s"}}}}\\n' \\
                                        "$raw_media_type" "$remote_id"
                                else
                                    printf '{{"schemaVersion":2,"mediaType":"%s","config":{{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"%s","size":222}}}}\\n' \\
                                        "$raw_media_type" "$remote_id"
                                fi
                                ;;
                            *--format*)
                                architecture='amd64'
                                [ "$FAKE_DOCKER_SCENARIO" = 'remote-platform-mismatch' ] \\
                                    && architecture='arm64'
                                printf '{{"architecture":"%s","os":"linux"}}\\n' \\
                                    "$architecture"
                                ;;
                            *) exit 94 ;;
                        esac
                        ;;
                esac
                ;;
            *) exit 95 ;;
        esac
        ;;
    run)
        cidfile=
        previous=
        for argument in "$@"; do
            [ "$previous" != '--cidfile' ] || cidfile=$argument
            previous=$argument
        done
        if [ -z "$cidfile" ]; then
            if [ "$FAKE_DOCKER_SCENARIO" = 'container-name-race' ]; then
                : > "$FAKE_DOCKER_STATE_DIRECTORY/competitor-container"
                exit 1
            fi
            printf '%s\\n' 'fake-container-id'
            exit 0
        fi
        if [ "$FAKE_DOCKER_SCENARIO" = 'container-name-race' ]; then
            : > "$FAKE_DOCKER_STATE_DIRECTORY/competitor-container"
            exit 1
        fi
        printf '%s\\n' "$container_id" > "$cidfile"
        chmod 0600 "$cidfile"
        if [ "$FAKE_DOCKER_SCENARIO" = 'container-start-failure-owned' ]; then
            : > "$FAKE_DOCKER_STATE_DIRECTORY/owned-container"
            exit 1
        fi
        if [ "$FAKE_DOCKER_SCENARIO" = 'container-signal-after-cidfile' ]; then
            : > "$FAKE_DOCKER_STATE_DIRECTORY/owned-container"
            kill -TERM "$PPID"
            exit 0
        fi
        printf '%s\\n' "$container_id"
        ;;
    exec)
        cat >/dev/null
        ;;
    rm)
        if [ "$FAKE_DOCKER_SCENARIO" = 'container-name-race' ]; then
            rm -f "$FAKE_DOCKER_STATE_DIRECTORY/competitor-container"
        fi
        case "$FAKE_DOCKER_SCENARIO" in
            container-start-failure-owned|container-signal-after-cidfile)
                rm -f "$FAKE_DOCKER_STATE_DIRECTORY/owned-container"
                ;;
        esac
        ;;
    tag)
        if [ "$FAKE_DOCKER_SCENARIO" = 'rollback-tag-race' ]; then
            printf '%s\\n' "$competitor_id" \
                > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
            : > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created"
            exit 1
        fi
        if [ "$FAKE_DOCKER_SCENARIO" = 'rollback-tag-overwrite-race' ]; then
            printf '%s\\n' "$competitor_id" \
                > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
            : > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created"
        fi
        printf '%s\\n' "$image_id" \
            > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
        : > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created"
        ;;
    push)
        case "$FAKE_DOCKER_SCENARIO" in
            legacy-replaced-before-cleanup)
                printf '%s\\n' "$competitor_id" \
                    > "$FAKE_DOCKER_STATE_DIRECTORY/legacy-created.identity"
                ;;
            rollback-replaced-before-cleanup)
                printf '%s\\n' "$competitor_id" \
                    > "$FAKE_DOCKER_STATE_DIRECTORY/rollback-created.identity"
                ;;
        esac
        : > "$FAKE_DOCKER_STATE_DIRECTORY/remote-pushed"
        printf '%s\\n' 'PUSH_TRANSCRIPT'
        ;;
    *)
        exit 96
        ;;
esac
"""
