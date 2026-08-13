# NEIS Record-Vintage Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a bounded range of official NEIS record load dates while preserving and review-binding the complete date distribution before human approval.

**Architecture:** `SourceProvenance` carries an immutable raw-row observation-date histogram for every source. The candidate builder, persisted manifest validator, snapshot loader, and review packet all replay the same histogram; `sourceAsOf` remains its latest key for compatibility. NEIS records keep their own `LOAD_DTM` instead of being rewritten to a page maximum, and a 90-calendar-day span is a fail-closed source-quality gate.

**Tech Stack:** Python 3.12, Pydantic v2 strict models, pytest, canonical JSON SHA-256, existing signed candidate transaction and review-digest approval workflow.

## Global Constraints

- The observation-date histogram is sorted, uses ISO dates as keys, positive exact `int` counts as values, and sums to `fetchedRowCount`.
- A source's `sourceAsOf` is exactly the histogram maximum; its span (`max - min`) must be no greater than 90 calendar days.
- Every current normalized institution date must occur in its source histogram; missing-from-source records retain the current source maximum date so persisted candidate validation remains deterministic.
- Every source, including a single-date source, must emit the required `sourceObservationDateCounts` manifest field; old snapshots lacking it fail closed.
- Candidate review includes aggregate date provenance only. It must never include names, addresses, coordinates, raw rows, request parameters, or credentials.
- `sync-institutions.py` remains candidate-only. Only `approve-institution-snapshot.py` may write `current.json` after digest revalidation.
- Update only administrator operations documentation in `apps/travel-map/README.md`; do not add source-vintage language to public UI, API responses, or user-facing static copy.

---

### Task 1: Preserve raw source observation dates

**Files:**
- Modify: `apps/travel-map/app/institutions/sources/common.py:42-57`
- Modify: `apps/travel-map/app/institutions/sources/neis.py:96-211`
- Modify: `apps/travel-map/app/institutions/sources/kindergarten.py:84-156`
- Modify: `apps/travel-map/app/institutions/sources/sen.py:73-113`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`

**Interfaces:**
- Consumes: raw NEIS rows with `LOAD_DTM`, and existing `SourceInstitutionRecord.source_as_of`.
- Produces: `SourceProvenance(source_observation_date_counts: tuple[tuple[str, int], ...])`, where the tuple is sorted by ISO date and is attached to every source result.

- [ ] **Step 1: Write failing NEIS-source tests**

```python
def test_neis_fetch_preserves_mixed_raw_load_dates() -> None:
    result = asyncio.run(source.fetch())
    assert {record.source_as_of for record in result.records} == {
        "2026-04-23", "2026-06-07"
    }
    assert result.provenance.source_as_of == "2026-06-07"
    assert result.provenance.source_observation_date_counts == (
        ("2026-04-23", 1),
        ("2026-06-07", 1),
    )


def test_neis_fetch_rejects_raw_observation_span_over_90_days() -> None:
    with pytest.raises(SourceDataError, match="observation date span"):
        asyncio.run(source.fetch())
```

- [ ] **Step 2: Run the NEIS source tests to verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_sync.py -k 'neis and (mixed or observation)' -q`

Expected: FAIL because mixed `LOAD_DTM` currently raises and `SourceProvenance` has no histogram field.

- [ ] **Step 3: Implement immutable observation-date collection**

```python
def _sorted_observation_date_counts(raw_rows: list[object]) -> tuple[tuple[str, int], ...]:
    counts = Counter(_yyyymmdd_as_iso(_required_string_from_object(row, "LOAD_DTM"))
                     for row in raw_rows)
    dates = tuple(sorted(counts.items()))
    if not dates or (date.fromisoformat(dates[-1][0]) - date.fromisoformat(dates[0][0])).days > 90:
        raise SourceDataError("NEIS observation date span exceeds 90 days")
    return dates
```

Remove the singleton raw-date rejection. Set every parsed NEIS record's
`source_as_of` from its own `LOAD_DTM`, set provenance `source_as_of` to the
histogram maximum, and set the histogram from all fetched raw rows. For
kindergarten and SEN, emit a one-entry histogram whose count is their actual
fetched raw-row count.

- [ ] **Step 4: Run the source tests to verify GREEN**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_sync.py -k 'neis or kindergarten or sen' -q`

Expected: PASS with mixed records retained, a 90-day span accepted, a 91-day
span rejected, and all existing single-date source assertions intact.

- [ ] **Step 5: Commit the source-boundary change**

```bash
git add apps/travel-map/app/institutions/sources/common.py \
  apps/travel-map/app/institutions/sources/neis.py \
  apps/travel-map/app/institutions/sources/kindergarten.py \
  apps/travel-map/app/institutions/sources/sen.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: preserve NEIS observation dates"
```

### Task 2: Bind and verify date histograms in candidate snapshots

**Files:**
- Modify: `apps/travel-map/app/institutions/models.py:234-306`
- Modify: `apps/travel-map/app/institutions/snapshot.py:35-75, 587-625`
- Modify: `apps/travel-map/app/institutions/sync.py:472-507, 1218-1306, 1630-1665, 2484-2558`
- Modify: `apps/travel-map/tests/institutions/test_snapshot.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`

**Interfaces:**
- Consumes: `SourceProvenance.source_observation_date_counts` from Task 1.
- Produces: strict manifest field `sourceObservationDateCounts: dict[str, int]` and verified per-institution date membership.

- [ ] **Step 1: Write failing strict-schema and integrity tests**

```python
def test_snapshot_accepts_bounded_date_histogram_and_mixed_institution_dates(tmp_path: Path) -> None:
    snapshot = copy_fixture_snapshot(tmp_path)
    set_source_histogram(snapshot, {"2026-04-23": 1, "2026-06-07": 1})
    set_institution_date(snapshot, 0, "2026-04-23")
    assert verify_snapshot(snapshot).manifest.sources[0].source_as_of == "2026-06-07"


@pytest.mark.parametrize("histogram", [
    {"2026-04-23": 0, "2026-06-07": 2},
    {"2026-04-23": 1, "2026-07-23": 1},
])
def test_snapshot_rejects_invalid_observation_date_histogram(tmp_path: Path, histogram: dict[str, int]) -> None:
    snapshot = copy_fixture_snapshot(tmp_path)
    set_source_histogram(snapshot, histogram)
    with pytest.raises(SnapshotIntegrityError):
        verify_snapshot(snapshot)
```

Also add tests that reject a missing histogram, count sum mismatch, maximum
that differs from `sourceAsOf`, and an institution date absent from the
histogram.

- [ ] **Step 2: Run the snapshot tests to verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_snapshot.py -k 'observation or histogram' -q`

Expected: FAIL because strict manifest fields and `SourceSnapshotInfo` do not
yet define `sourceObservationDateCounts`.

- [ ] **Step 3: Implement strict manifest and candidate replay**

```python
class SourceSnapshotInfo(_StrictSnapshotModel):
    # existing fields
    source_observation_date_counts: dict[str, int]

    @model_validator(mode="after")
    def observation_dates_are_consistent(self) -> Self:
        pairs = tuple(sorted(self.source_observation_date_counts.items()))
        if not pairs or sum(count for _, count in pairs) != self.fetched_row_count:
            raise ValueError("source observation dates do not match fetchedRowCount")
        if any(type(count) is not int or count <= 0 for _, count in pairs):
            raise ValueError("source observation date count must be positive")
        if pairs[-1][0] != self.source_as_of:
            raise ValueError("sourceAsOf must equal the latest observation date")
        if (date.fromisoformat(pairs[-1][0]) - date.fromisoformat(pairs[0][0])).days > 90:
            raise ValueError("source observation date span exceeds 90 days")
        return self
```

Add the camel-case field to `_SOURCE_FIELDS`, serialize it in
`_candidate_manifest`, and replace every single-date equality condition in
`build_candidate_snapshot`, `_validate_source_provenance`,
`_recheck_source_provenance`, and `_verify_source_counts` with exact
histogram replay. Preserve missing institutions using the current source
histogram maximum.

- [ ] **Step 4: Run candidate, promotion, and snapshot integrity tests**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_sync.py apps/travel-map/tests/institutions/test_snapshot.py -q`

Expected: PASS. A tampered histogram or an unobserved institution date must
fail candidate review and approval before a `current.json` write.

- [ ] **Step 5: Commit the manifest-integrity change**

```bash
git add apps/travel-map/app/institutions/models.py \
  apps/travel-map/app/institutions/snapshot.py \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: verify source observation date ranges"
```

### Task 3: Surface only safe provenance to administrators

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py:1865-1935`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`
- Modify: `apps/travel-map/README.md:55-96`

**Interfaces:**
- Consumes: verified manifest `sourceObservationDateCounts` from Task 2.
- Produces: review-packet `sourceObservationDateRanges` with per-source
  earliest/latest/span/counts, bound by `reviewDigest`; administrator-only
  instructions to inspect it before approval.

- [ ] **Step 1: Write failing review-packet and documentation tests**

```python
def test_review_packet_binds_sorted_source_observation_date_ranges(tmp_path: Path) -> None:
    candidate = build_test_candidate(tmp_path, mixed_neis_dates=True)
    packet = build_candidate_review_packet(**candidate.review_args)
    assert packet["sourceObservationDateRanges"]["NEIS"] == {
        "earliest": "2026-04-23",
        "latest": "2026-06-07",
        "spanDays": 45,
        "rawRowCounts": {"2026-04-23": 1, "2026-06-07": 1},
    }
    assert "비공개검토학교" not in json.dumps(packet, ensure_ascii=False)
```

Add a digest-mismatch test after changing only the date histogram. Add a
documentation assertion that the administrator synchronization section names
the date range review, while no file under `apps/travel-map/app/static/` is
modified.

- [ ] **Step 2: Run review-packet tests to verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_sync.py -k 'review_packet and observation' -q`

Expected: FAIL because the packet does not yet contain
`sourceObservationDateRanges`.

- [ ] **Step 3: Implement privacy-safe packet projection and admin guidance**

```python
def _review_source_observation_date_ranges(entries: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        source: {
            "earliest": dates[0],
            "latest": dates[-1],
            "spanDays": (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days,
            "rawRowCounts": {day: counts[day] for day in dates},
        }
        for source, counts in sorted_source_histograms(entries)
        for dates in [sorted(counts)]
    }
```

Insert this aggregate-only field into `_build_review_packet` before computing
the canonical digest. In the existing administrator snapshot guide, require a
data steward to inspect each source's earliest/latest date, span, and raw-row
counts before approval; retain the existing credential-free review/approval
separation and add no public-copy changes.

- [ ] **Step 4: Run review/approval and documentation checks**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/institutions/test_sync.py -k 'review or approval' -q`

Expected: PASS. Packet content is sorted, digest-bound, privacy-safe, and a
candidate with a modified date histogram cannot be approved using its old
digest.

- [ ] **Step 5: Commit administrator provenance review**

```bash
git add apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/README.md
git commit -m "docs: add source date range review"
```

### Task 4: Verify and create a live candidate for human review

**Files:**
- Modify: none unless a failing verification proves a defect
- Test: existing `apps/travel-map/tests/` suite

**Interfaces:**
- Consumes: the three local keys in ignored `apps/travel-map/.env` and Task 1–3 behavior.
- Produces: an unapproved `.snapshot-id.candidate` and a safe review packet; never creates `current.json`.

- [ ] **Step 1: Run all offline quality gates**

Run:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map test:e2e
```

Expected: all commands exit zero with no warnings.

- [ ] **Step 2: Create the live candidate only**

Run:

```bash
review_dir=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-review.XXXXXX")
trap 'rm -rf -- "$review_dir"' EXIT HUP INT TERM
uv run --project apps/travel-map python \
  apps/travel-map/scripts/sync-institutions.py \
  --env-file apps/travel-map/.env | tee "$review_dir/institution-sync.jsonl"
snapshot_id=$(tail -n 1 "$review_dir/institution-sync.jsonl" | \
  uv run --project apps/travel-map python -c \
  'import json,sys; print(json.load(sys.stdin)["snapshotId"])')
```

Expected: compact final JSON status `CANDIDATE_REVIEW_REQUIRED` with a safe
snapshot ID; `resources/institution-snapshots/current.json` remains absent or
byte-for-byte unchanged.

- [ ] **Step 3: Generate and inspect the credential-free review packet**

Run:

```bash
uv run --project apps/travel-map python \
  apps/travel-map/scripts/review-institution-snapshot.py \
  --snapshot-id "$snapshot_id"
```

Expected: safe count/diff/quarantine data plus `sourceObservationDateRanges`
and `reviewDigest`. Stop for the data steward's explicit approval; do not run
the approval command or create `current.json` in this task.

- [ ] **Step 4: Commit and push the verified implementation**

```bash
git add apps/travel-map docs/superpowers/specs docs/superpowers/plans
git commit -m "feat: review mixed NEIS source dates"
git push github main
git push gitlab main
```

Expected: GitHub and GitLab `main` refer to the same commit. The candidate
directory and local `.env` remain untracked operational artifacts.

## Plan self-review

- Spec coverage: Task 1 preserves NEIS record dates and enforces the 90-day
  limit; Task 2 binds the range to the strict manifest, review, approval, and
  loader; Task 3 exposes only safe administrator provenance; Task 4 verifies
  and produces a candidate without promotion.
- Placeholder scan: no deferred implementation wording, unspecified tests, or
  unnamed interfaces remain.
- Type consistency: `source_observation_date_counts` is the source-layer
  tuple; `sourceObservationDateCounts` is its strict manifest mapping; and
  `sourceObservationDateRanges` is the safe review-packet projection.
