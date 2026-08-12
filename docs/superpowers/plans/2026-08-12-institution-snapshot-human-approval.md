# Institution Snapshot Human Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate live institution synchronization from production promotion so a data steward must inspect a deterministic candidate review packet and approve its exact digest before `current.json` changes.

**Architecture:** Keep the existing source ingestion, signed build transaction, provenance replay, quality gates, promotion lock, and crash-recovery state machine. Add one read-only candidate-review service in `app.institutions.sync`, then place a digest-and-role gate in front of the existing private mutation path. Expose three thin commands: networked candidate creation, credential-free review, and credential-free approval.

**Tech Stack:** Python 3.12, Pydantic v2, Shapely-backed `CoverageService`, SHA-256/HMAC comparison from the standard library, pytest, Ruff, mypy.

## Global Constraints

- Synchronization must create an unapproved `.<snapshot_id>.candidate` and must not create or modify `current.json`.
- Review and approval commands perform no network requests, load no environment file, and accept no credential argument.
- The review packet must contain counts, stable quarantine IDs, diff evidence, and hashes, but no names, addresses, coordinates, credentials, request headers, provider responses, or raw source rows.
- `reviewDigest` is the SHA-256 of canonical JSON for the packet before the digest field is appended: UTF-8, `ensure_ascii=False`, `sort_keys=True`, and separators `(",", ":")`.
- Approval accepts only a safe snapshot ID, exactly 64 lowercase hexadecimal digest characters, and reviewer role `data-steward`.
- Approval must regenerate and compare the packet under the existing promotion lock with `hmac.compare_digest()` before the first filesystem mutation.
- Existing signed transaction, path containment, symlink rejection, manifest/row hash replay, provenance replay, Seoul coverage replay, previous-snapshot relationship, atomic replacement, directory fsync, and restart-idempotency protections remain binding.
- Candidate-only state remains a release blocker. Fixtures and synthetic institutions must never become production resources.
- Every production behavior change follows RED → GREEN → refactor. Run tests with `PYTHONWARNINGS=error`.

---

## File structure

- `apps/travel-map/app/institutions/sync.py`: own candidate validation, canonical review packet construction, digest-gated approval, and the private atomic promotion state machine.
- `apps/travel-map/scripts/sync-institutions.py`: fetch official sources and stop after candidate creation.
- `apps/travel-map/scripts/review-institution-snapshot.py`: read-only candidate review adapter.
- `apps/travel-map/scripts/approve-institution-snapshot.py`: explicit data-steward approval adapter.
- `apps/travel-map/tests/institutions/test_sync.py`: candidate, privacy, digest, promotion, tamper, lock, and CLI regressions using the existing signed-transaction fixtures.
- `apps/travel-map/tests/test_release.py`: candidate-only release blocking and approved-snapshot staging.
- `apps/travel-map/README.md`: operator sequence and credential boundary.

---

### Task 1: Deterministic read-only candidate review packet

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py:688-786, 1712-1721, 1967-2013`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:1-75, 2327-2400, 3498-3650`

**Interfaces:**
- Consumes: existing `SnapshotBuildResult`, `_validated_snapshot_root()`, `_load_build_transaction()`, `_verify_manifest_fields()`, `_validate_unapproved_manifest_schema()`, `_recheck_candidate()`, `_recheck_promotion_quality()`, `_recheck_source_provenance()`, `_recheck_enrichment_provenance()`, `_transaction_attests_manifest()`, `verify_snapshot()`.
- Produces:

```text
build_candidate_review_packet(*, snapshot_id: str, snapshot_root: Path, coverage: CoverageService) -> dict[str, object]
```

- Private boundary used by Task 2:

```python
@dataclass(frozen=True)
class _ReviewableCandidate:
    result: SnapshotBuildResult
    root: Path
    selected_path: Path
    manifest: dict[str, object]
    institutions: tuple[Institution, ...]
    sites: tuple[InstitutionSite, ...]
    transaction: dict[str, object]

_load_reviewable_candidate(*, snapshot_id: str, snapshot_root: Path, coverage: CoverageService, allow_recovery_final: bool) -> _ReviewableCandidate

_build_review_packet(reviewable: _ReviewableCandidate) -> dict[str, object]
```

- Packet keys are exactly:

```python
{
    "status": "CANDIDATE_REVIEW_REQUIRED",
    "snapshotId": str,
    "createdAt": str,
    "snapshotAsOf": str,
    "previousSnapshotId": str | None,
    "sourceCounts": dict[str, dict[str, int]],
    "institutionTypeCounts": dict[str, int],
    "foundationCounts": dict[str, int],
    "districtCounts": dict[str, int],
    "statusCounts": dict[str, int],
    "coordinateQualityCounts": dict[str, int],
    "quarantinedInstitutionIds": list[str],
    "quarantinedSiteIds": list[str],
    "diff": dict[str, object],
    "siteOnlyDiff": {
        "addedSiteIds": list[str],
        "changedSiteIds": list[str],
        "missingSiteIds": list[str],
    },
    "institutionsSha256": str,
    "sitesSha256": str,
    "candidateManifestSha256": str,
    "sourceProvenanceSha256": str,
    "enrichmentProvenanceSha256": str,
    "reviewDigest": str,
}
```

- `candidateManifestSha256` always hashes the manifest after normalizing `approved=False`, `approvedAt=None`, and `approvedByRole=None`, so a crash-recovery retry can reconstruct the same packet from an attested final directory.
- `siteOnlyDiff` compares site change keys only for institutions whose institution-level change key is unchanged. This prevents a site change from being counted both as an institution change and a site-only change.

- [ ] **Step 1: Add failing deterministic and audit-category tests**

Add `import re`, `from typing import cast`, and the
`build_candidate_review_packet` import, then add:

```python
def test_review_packet_is_deterministic_and_contains_audit_categories(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="review-packet",
        coverage=TEST_COVERAGE,
    )

    first = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    second = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert first == second
    assert set(first) == {
        "status", "snapshotId", "createdAt", "snapshotAsOf",
        "previousSnapshotId", "sourceCounts", "institutionTypeCounts",
        "foundationCounts", "districtCounts", "statusCounts",
        "coordinateQualityCounts", "quarantinedInstitutionIds",
        "quarantinedSiteIds", "diff", "siteOnlyDiff",
        "institutionsSha256", "sitesSha256", "candidateManifestSha256",
        "sourceProvenanceSha256", "enrichmentProvenanceSha256",
        "reviewDigest",
    }
    assert first["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert first["snapshotId"] == candidate.snapshot_id
    assert re.fullmatch(r"[0-9a-f]{64}", cast(str, first["reviewDigest"]))
    without_digest = {key: value for key, value in first.items() if key != "reviewDigest"}
    assert first["reviewDigest"] == hashlib.sha256(
        json.dumps(
            without_digest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert not (tmp_path / "current.json").exists()
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py::test_review_packet_is_deterministic_and_contains_audit_categories -q
```

Expected: collection fails because `build_candidate_review_packet` does not exist.

- [ ] **Step 3: Add failing privacy and tamper tests**

```python
def test_review_packet_excludes_sensitive_source_values(tmp_path: Path) -> None:
    sensitive = replace(
        source_record(road_address="서울특별시 중구 비공개로 987"),
        official_name="비공개검토학교",
    )
    candidate = build_test_candidate(
        records=(sensitive,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="private-review",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)

    for forbidden in (
        sensitive.official_name,
        sensitive.road_address,
        str(sensitive.latitude),
        str(sensitive.longitude),
        "Authorization",
        "rawResponse",
        "KAKAO_REST_API_KEY",
    ):
        assert forbidden not in serialized


def test_review_rejects_tampered_candidate_before_emitting_packet(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="tampered-review",
        coverage=TEST_COVERAGE,
    )
    (candidate.candidate_path / "sites.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )

    with pytest.raises(SnapshotQualityError, match="hash|candidate"):
        build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


def test_review_rejects_unsafe_id_symlink_and_quality_issues(tmp_path: Path) -> None:
    with pytest.raises(SnapshotQualityError, match="snapshot ID"):
        build_candidate_review_packet(
            snapshot_id="../unsafe",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    external = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path / "external",
        snapshot_id="symlink-review",
        coverage=TEST_COVERAGE,
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / ".symlink-review.candidate").symlink_to(
        external.candidate_path,
        target_is_directory=True,
    )
    with pytest.raises(SnapshotQualityError, match="symlink"):
        build_candidate_review_packet(
            snapshot_id="symlink-review",
            snapshot_root=target,
            coverage=TEST_COVERAGE,
        )

    invalid = replace(source_record(), latitude=35.1796, longitude=129.0756)
    rejected = build_test_candidate(
        records=(invalid,),
        previous=None,
        output_root=tmp_path / "quality",
        snapshot_id="quality-review",
        coverage=TEST_COVERAGE,
    )
    assert rejected.issues
    with pytest.raises(SnapshotQualityError, match="coordinate|quality"):
        build_candidate_review_packet(
            snapshot_id=rejected.snapshot_id,
            snapshot_root=tmp_path / "quality",
            coverage=TEST_COVERAGE,
        )
```

- [ ] **Step 4: Run the privacy/tamper tests and verify RED**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'review_packet or review_rejects_tampered' -q
```

Expected: the packet tests fail because the review API is absent.

- [ ] **Step 5: Extract read-only validation from the promotion path**

Implement `_load_reviewable_candidate()` by moving, without weakening, the read-only section of `_promote_snapshot_locked()` from safe-ID validation through these existing calls:

```python
institutions, sites = _recheck_candidate(selected_path, manifest, snapshot_id)
_recheck_promotion_quality(root, manifest, institutions, sites, coverage)
_recheck_source_provenance(manifest, institutions, sites)
_recheck_enrichment_provenance(manifest, institutions, sites)
_transaction_attests_manifest(transaction, manifest)
```

For public review, require the candidate directory, `approved is False`, and transaction phase `BUILT`. For approval recovery, permit only the already-enforced final-directory phase/manifest combinations. Do not fsync, rename, write, or lock in this helper.

- [ ] **Step 6: Implement canonical packet construction**

Use these helpers and exact digest algorithm:

```python
def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _unapproved_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    value = dict(manifest)
    value["approved"] = False
    value["approvedAt"] = None
    value["approvedByRole"] = None
    return value
```

Build all count dictionaries and ID lists in sorted order. Derive district counts from default sites. Build `packet_without_digest`, then:

```python
packet = dict(packet_without_digest)
packet["reviewDigest"] = _canonical_sha256(packet_without_digest)
return packet
```

- [ ] **Step 7: Run focused review tests and all existing promotion tests**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'review or promotion or candidate' -q
```

Expected: PASS with no warnings. Existing direct promotion behavior remains unchanged until Task 2.

- [ ] **Step 8: Commit Task 1**

```sh
git add apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: add institution candidate review packet"
```

---

### Task 2: Digest-gated explicit approval service

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py:656-910`
- Modify: `apps/travel-map/scripts/sync-institutions.py:1-31, 92-252`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:2327-3425, 3498-3523`

**Interfaces:**
- Consumes: `build_candidate_review_packet()` and `_load_reviewable_candidate()` from Task 1, existing promotion lock and `_promote_snapshot_locked()` state machine.
- Produces:

```text
approve_candidate_snapshot(*, snapshot_id: str, review_digest: str, reviewer_role: str, snapshot_root: Path, coverage: CoverageService) -> str
```

- Stop `sync-institutions.py` from importing or calling `promote_snapshot()` in
  this task, so no production command has an automatic approval path. Task 3
  removes the temporary test-only compatibility symbol after migrating the
  existing atomic-promotion regression suite.
- Test helper:

```python
def approve_test_candidate(
    candidate: SnapshotBuildResult,
    output_root: Path,
    *,
    coverage: CoverageService = TEST_COVERAGE,
) -> str:
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=output_root,
        coverage=coverage,
    )
    return approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=cast(str, packet["reviewDigest"]),
        reviewer_role="data-steward",
        snapshot_root=output_root,
        coverage=coverage,
    )
```

- [ ] **Step 1: Add failing role and digest tests**

```python
@pytest.mark.parametrize(
    "review_digest",
    ["", "A" * 64, "0" * 63, "0" * 65, True, 1],
)
def test_approval_requires_exact_lowercase_review_digest(
    tmp_path: Path,
    review_digest: object,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="digest-contract", coverage=TEST_COVERAGE,
    )
    with pytest.raises(SnapshotQualityError, match="review digest"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=cast(str, review_digest),
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


def test_approval_requires_data_steward_role(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="role-contract", coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    with pytest.raises(SnapshotQualityError, match="reviewer role"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=cast(str, packet["reviewDigest"]),
            reviewer_role="developer",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
```

- [ ] **Step 2: Run role/digest tests and verify RED**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'approval_requires_exact or approval_requires_data' -q
```

Expected: collection fails because `approve_candidate_snapshot` does not exist.

- [ ] **Step 3: Add failing exact-review and successful-approval tests**

```python
def test_approval_rechecks_review_digest_before_pointer_mutation(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="digest-recheck", coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["countsByType"] = {"ELEMENTARY_SCHOOL": 2}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="attestation|manifest|digest"):
        approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=cast(str, packet["reviewDigest"]),
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


def test_reviewed_approval_writes_verified_current_snapshot(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="reviewed-approval", coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    digest = approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=cast(str, packet["reviewDigest"]),
        reviewer_role="data-steward",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    verified = verify_snapshot(tmp_path)
    assert digest == packet["reviewDigest"]
    assert verified.manifest.snapshot_id == candidate.snapshot_id
    assert verified.manifest.approved is True
    assert verified.manifest.approved_by_role == "data-steward"
```

- [ ] **Step 4: Run exact-review and success tests and verify RED**

Run the two named tests. Expected: FAIL because the approval API is absent.

- [ ] **Step 5: Implement approval validation under the existing lock**

Validate types before opening the lock:

```python
if type(snapshot_id) is not str or _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
    raise SnapshotQualityError("snapshot ID is unsafe")
if type(review_digest) is not str or _SHA256.fullmatch(review_digest) is None:
    raise SnapshotQualityError("review digest is invalid")
if reviewer_role != "data-steward":
    raise SnapshotQualityError("reviewer role is invalid")
```

Open and validate `.promotion.lock` exactly as the current public promotion function does. Under `LOCK_EX`:

1. load the reviewable candidate with `allow_recovery_final=True`;
2. rebuild the packet and extract its digest;
3. compare with `hmac.compare_digest(review_digest, actual_digest)`;
4. call the existing private state machine with a `SnapshotBuildResult` derived from the validated reviewable state;
5. call `verify_snapshot(root)` and require the requested snapshot ID;
6. return the actual digest.

Do not acquire the same lock twice. Split the old function into `_approve_candidate_locked()` and `_publish_reviewed_candidate_locked()` as needed.

- [ ] **Step 6: Stop the production synchronizer after candidate creation**

Change these signatures:

```text
run(args: argparse.Namespace, keys: dict[str, str]) -> Awaitable[str]
_run_with_keys(args: argparse.Namespace, keys: dict[str, str], credential_holders: list[NeisSource | KindergartenSource | KakaoLocalClient]) -> Awaitable[str]
```

Remove the `promote_snapshot` import and call. After
`build_candidate_snapshot()`, reject `candidate.issues` and return
`candidate.snapshot_id`. Delete the approved-snapshot verification and
post-promotion summary. In `main()`, print this compact, sorted final status
after `asyncio.run()` returns:

```python
print(json.dumps(
    {"snapshotId": snapshot_id, "status": "CANDIDATE_REVIEW_REQUIRED"},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
))
```

- [ ] **Step 7: Add candidate-only, pointer preservation, and restart-idempotency regressions**

Use the existing `importlib.util` loading pattern:

```python
async def test_sync_cli_stops_at_candidate_review_without_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location(
        "candidate_only_sync_cli", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def fake_run_with_keys(
        _args: argparse.Namespace,
        _keys: dict[str, str],
        _holders: list[object],
    ) -> str:
        return "cli-candidate"

    monkeypatch.setattr(module, "_run_with_keys", fake_run_with_keys)
    keys = {
        "NEIS_API_KEY": "neis-test",
        "KINDERGARTEN_API_KEY": "kindergarten-test",
        "KAKAO_REST_API_KEY": "kakao-test",
    }
    result = await module.run(argparse.Namespace(), keys)

    assert result == "cli-candidate"
    assert set(keys.values()) == {""}
    assert not hasattr(module, "promote_snapshot")
    assert not (tmp_path / "current.json").exists()
```

Add `approve_test_candidate()` to the test helpers for the new tests below;
leave the old direct promotion tests untouched until Task 3.

For the digest-recheck test, preserve this ordering:

```python
packet = build_candidate_review_packet(
    snapshot_id=candidate.snapshot_id,
    snapshot_root=tmp_path,
    coverage=TEST_COVERAGE,
)
tamper_candidate()
with pytest.raises(SnapshotQualityError):
    approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=cast(str, packet["reviewDigest"]),
        reviewer_role="data-steward",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
```

Also add:

```python
def test_candidate_review_leaves_existing_current_pointer_unchanged(
    tmp_path: Path,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="initial-current", coverage=TEST_COVERAGE,
    )
    approve_test_candidate(initial, tmp_path)
    before = (tmp_path / "current.json").read_bytes()
    second = build_test_candidate(
        records=(source_record(),), previous=verify_snapshot(tmp_path),
        output_root=tmp_path, snapshot_id="next-candidate",
        coverage=TEST_COVERAGE,
    )

    packet = build_candidate_review_packet(
        snapshot_id=second.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert packet["previousSnapshotId"] == "initial-current"
    assert (tmp_path / "current.json").read_bytes() == before


def test_reviewed_approval_retry_is_idempotent(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="reviewed-retry", coverage=TEST_COVERAGE,
    )
    packet = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    kwargs = {
        "snapshot_id": candidate.snapshot_id,
        "review_digest": packet["reviewDigest"],
        "reviewer_role": "data-steward",
        "snapshot_root": tmp_path,
        "coverage": TEST_COVERAGE,
    }
    approve_candidate_snapshot(**kwargs)
    first_pointer = (tmp_path / "current.json").read_bytes()
    approve_candidate_snapshot(**kwargs)
    assert (tmp_path / "current.json").read_bytes() == first_pointer
```

- [ ] **Step 8: Run the full institution sync suite and sync subprocess tests**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py -q
```

Expected: PASS, including all existing tamper, provenance, lock, atomicity, and crash-recovery cases.

- [ ] **Step 9: Run static checks for the modified domain module**

```sh
uv run --project apps/travel-map ruff check \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/tests/institutions/test_sync.py
uv run --project apps/travel-map mypy \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/scripts/sync-institutions.py
```

Expected: both commands exit 0.

- [ ] **Step 10: Commit Task 2**

```sh
git add apps/travel-map/app/institutions/sync.py \
  apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: require reviewed institution snapshot approval"
```

---

### Task 3: Remove the automatic bypass and add review/approval CLIs

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py:656-910`
- Create: `apps/travel-map/scripts/review-institution-snapshot.py`
- Create: `apps/travel-map/scripts/approve-institution-snapshot.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:579-3408, 3498-3523`

**Interfaces:**
- Consumes the candidate-only sync final stdout line implemented in Task 2:

```json
{"snapshotId":"20260812T120000Z","status":"CANDIDATE_REVIEW_REQUIRED"}
```

- `review-institution-snapshot.py` arguments: required `--snapshot-id`; optional roots with the same production defaults as sync. Output is the Task 1 packet.
- `approve-institution-snapshot.py` arguments: required `--snapshot-id`, `--review-digest`, `--reviewer-role`; optional roots. Exact success output:

```json
{"reviewDigest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","snapshotId":"20260812T120000Z","status":"SNAPSHOT_APPROVED"}
```

- Both credential-free commands reject `--env-file` as an unknown argument.

- [ ] **Step 1: Migrate atomic-promotion tests through reviewed approval**

Replace the `promote_snapshot` import with `approve_candidate_snapshot` and
`build_candidate_review_packet`. Use `approve_test_candidate()` for clean
success paths.

For every test that tampers after candidate construction, review first, then
tamper, then call `approve_candidate_snapshot()` with the original digest. For
crash-recovery tests, compute the digest before injecting `os.replace`
failures, call approval, restore the primitive, and retry with the same digest.
Keep all existing assertions about pointer immutability, signed transaction
phases, fsync order, symlink/path rejection, provenance replay, and strict
manifest verification.

- [ ] **Step 2: Remove the public automatic-promotion symbol**

Rename the remaining mutation entry point to a leading-underscore private
function used only by `approve_candidate_snapshot()` under the already-held
lock. Assert no application script imports or calls it:

```python
def test_no_production_script_has_automatic_snapshot_promotion() -> None:
    assert not hasattr(sync_module, "promote_snapshot")
    for path in Path("apps/travel-map/scripts").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "promote_snapshot" not in source
```

- [ ] **Step 3: Run the migrated institution suite**

Run the full `test_sync.py`. Expected: PASS with no direct automatic approval
entry point and no loss of atomic/recovery coverage.

- [ ] **Step 4: Add failing review CLI tests**

```python
def test_review_cli_is_offline_and_emits_only_review_packet(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),), previous=None, output_root=tmp_path,
        snapshot_id="review-cli", coverage=TEST_COVERAGE,
    )
    completed = subprocess.run(
        [
            sys.executable,
            "apps/travel-map/scripts/review-institution-snapshot.py",
            "--snapshot-id", candidate.snapshot_id,
            "--snapshot-root", str(tmp_path),
            "--geodata-root", "apps/travel-map/resources/geodata",
        ],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "apps/travel-map"},
        text=True, capture_output=True, check=False,
    )
    packet = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert packet["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert completed.stderr == ""
```

Add a second subprocess call with `--env-file ignored` and assert exit 2 / unrecognized argument.

- [ ] **Step 5: Run review CLI tests and verify RED**

Expected: FAIL because the script does not exist.

- [ ] **Step 6: Implement the review CLI**

Use only `argparse`, `json`, `Path`, `build_candidate_review_packet`, and `CoverageService`. The `main()` exception boundary catches `SnapshotQualityError`, `OSError`, and `ValueError`, executes `print(f"candidate review failed: {exc}", file=sys.stderr)`, and returns 1. Print JSON with:

```python
print(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
```

- [ ] **Step 7: Add failing approval CLI tests**

Create a candidate and packet in the parent test process. Run the new command in a subprocess with only `PATH` and `PYTHONPATH`. Assert exact success object, `verify_snapshot()` success, and absence of credential names in stdout/stderr. Add an `--env-file` rejection assertion.

- [ ] **Step 8: Run approval CLI tests and verify RED**

Expected: FAIL because the approval script does not exist.

- [ ] **Step 9: Implement the approval CLI**

Use only `argparse`, `json`, `Path`, `approve_candidate_snapshot`, and `CoverageService`. Require all three approval arguments. Call the service and emit exactly the three success fields. Catch the same safe exception set and return 1.

- [ ] **Step 10: Run all CLI and credential-scrubbing tests**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/test_release.py \
  -k 'cli or credential or environment or review or approval' -q
```

Expected: PASS with no secrets or warnings in captured output.

- [ ] **Step 11: Run script static checks**

```sh
uv run --project apps/travel-map ruff check \
  apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/scripts/review-institution-snapshot.py \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py
uv run --project apps/travel-map mypy \
  apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/scripts/review-institution-snapshot.py \
  apps/travel-map/scripts/approve-institution-snapshot.py
```

- [ ] **Step 12: Commit Task 3**

```sh
git add apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/scripts/review-institution-snapshot.py \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: split institution sync review and approval"
```

---

### Task 4: Release blocking characterization, operator documentation, and complete verification

**Files:**
- Modify: `apps/travel-map/tests/test_release.py:26-55, 122-191, 327-357`
- Modify: `apps/travel-map/README.md:44-82`

**Interfaces:**
- Candidate-only root has no valid production `current.json`, so `verify_snapshot()` and `prepare-release-context.py` remain blocked without production-code changes.
- Explicitly approved root is accepted by the existing release artifact preflight.

- [ ] **Step 1: Add the unchanged release-boundary characterization**

```python
def test_release_staging_rejects_candidate_without_current_pointer(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "travel-map"
    shutil.copytree(ROOT, source_root)
    snapshots = source_root / "resources/institution-snapshots"
    snapshots.mkdir()
    shutil.copytree(
        FIXTURE_SNAPSHOT / "fixture-001",
        snapshots / ".candidate-review.candidate",
    )
    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="candidate_release_test")

    with pytest.raises((OSError, ValueError), match="current|snapshot"):
        module["stage_release_context"](source_root, tmp_path / "context")
```

This uses the approved fixture bytes only as an inert candidate-shaped directory in `tmp_path`; it never writes the repository production resources. The existing `test_release_context_contains_only_the_current_verified_snapshot` remains the positive approved-state assertion.

- [ ] **Step 2: Run the release-state characterization**

Run the named test. Expected: PASS immediately because the production release boundary is already fail-closed. This is an unchanged-boundary characterization, not a reason to modify release code.

- [ ] **Step 3: Confirm no release production file changed**

Run `git diff --name-only` and confirm neither `prepare-release-context.py` nor `release-gate.sh` appears. The candidate test must fail via the existing production verifier; the existing approved fixture test must pass through that same verifier.

- [ ] **Step 4: Update the operator workflow**

Replace the current one-command implication with these exact stages:

```sh
review_dir=$(mktemp -d "${TMPDIR:-/tmp}/institution-review.XXXXXX")
chmod 700 "$review_dir"
trap 'rm -rf -- "$review_dir"' EXIT HUP INT TERM

uv run --project apps/travel-map python \
  apps/travel-map/scripts/sync-institutions.py \
  --env-file apps/travel-map/.env | tee "$review_dir/institution-sync.jsonl"

snapshot_id=$(tail -n 1 "$review_dir/institution-sync.jsonl" | \
  uv run --project apps/travel-map python -c \
  'import json,sys; print(json.load(sys.stdin)["snapshotId"])')
uv run --project apps/travel-map python \
  apps/travel-map/scripts/review-institution-snapshot.py \
  --snapshot-id "$snapshot_id" | tee "$review_dir/institution-review.json"

less "$review_dir/institution-review.json"
review_digest=$(uv run --project apps/travel-map python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["reviewDigest"])' \
  "$review_dir/institution-review.json")
uv run --project apps/travel-map python \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  --snapshot-id "$snapshot_id" \
  --review-digest "$review_digest" \
  --reviewer-role data-steward
```

Explain that `snapshot_id` comes from the final sync JSON line. The reviewer must inspect source/type/foundation/district/status/coordinate counts, quarantine IDs, and diffs in `less` before continuing past that command. The temporary directory is owner-only and deleted by the trap. State explicitly that only the sync command reads provider credentials and that candidate creation alone does not unblock release.

- [ ] **Step 5: Run focused institution and release suites**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions \
  apps/travel-map/tests/test_release.py -q
```

Expected: PASS with no warnings.

- [ ] **Step 6: Run the complete Python suite**

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests -q
```

Expected: all tests pass; no approved production snapshot is created.

- [ ] **Step 7: Run complete static verification**

```sh
uv run --project apps/travel-map ruff check apps/travel-map
uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 8: Verify the real repository remains candidate-free and release-blocked**

```sh
test ! -e apps/travel-map/resources/institution-snapshots/current.json
./apps/travel-map/scripts/release-gate.sh
```

Expected: the first command exits 0. The release gate exits 2 with `BLOCKED_INVALID_RELEASE_ARTIFACT`; it must not reach Docker or create a production snapshot.

- [ ] **Step 9: Commit Task 4**

```sh
git add apps/travel-map/README.md apps/travel-map/tests/test_release.py
git commit -m "docs: require institution snapshot review before release"
```

- [ ] **Step 10: Request final whole-branch review**

Provide the design, this plan, implementation reports, and the full branch diff to a fresh reviewer. A clean result must explicitly confirm:

- no automatic promotion path remains in production scripts;
- digest comparison occurs under the promotion lock before mutation;
- review and approval are credential-free and privacy-safe;
- signed transaction and crash recovery remain intact;
- release stays fail-closed without explicit approval.
