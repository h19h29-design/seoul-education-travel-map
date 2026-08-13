# NEIS record-vintage provenance design

## Context

The live Seoul NEIS `schoolInfo` response identifies the individual record
load date in `LOAD_DTM`. On 2026-08-13 the official B10 response contained
2026-04-23, 2026-05-17, and 2026-06-07 across its two pages. These are
record-level update dates, not a single dataset publication date.

The synchronizer currently rejects any response that has more than one
`LOAD_DTM` value and then rewrites every accepted institution's
`sourceAsOf` to its page maximum. This prevents a live candidate from being
created and discards source provenance that a reviewer needs to assess.

## Decision

Keep each NEIS institution's original `LOAD_DTM` as its ISO-8601
`sourceAsOf`. Extend every source manifest entry with a hash-bound, sorted
`sourceObservationDateCounts` mapping of record-observation date to raw row
count. Sources with a single observation date emit a one-entry mapping.

For a source entry:

- `sourceAsOf` remains the latest observed date for compatibility;
- the earliest and latest mapping keys define its observation range;
- mapping values are positive integers whose sum equals `fetchedRowCount`;
- no observation date may be later than `fetchedAt` or `snapshotAsOf`;
- every normalized institution's `sourceAsOf` must be an observed date for
  its source; and
- the earliest-to-latest span must be at most 90 calendar days.

The 90-day guard is a quality limit, not a freshness claim. It blocks an
unexpectedly stale or fragmented source while allowing the observed 45-day
NEIS range. Candidate review and approval continue to revalidate every
source hash, observation-date mapping, count, coverage rule, and signed
transaction before any pointer mutation.

## Review packet and administrator procedure

The credential-free review packet gains only aggregate provenance for each
source: earliest date, latest date, span in days, and the sorted
date-to-raw-row-count mapping. It contains no institution name, road address,
coordinate, raw payload, or credential. The packet digest binds this content
to the candidate and is regenerated under the promotion lock.

The administrator-only synchronization guide must instruct a data steward to
inspect the NEIS range and counts along with the existing provenance, quality,
quarantine, and diff fields before copying the review digest into the separate
approval command. The public no-login map and its user-facing copy do not
mention source-update dates or this operational process.

## Failure behavior

Candidate creation fails before `current.json` is created or changed if any
source date is malformed, missing, outside the 90-day range, later than the
fetch or snapshot date, inconsistent with the raw row total, or not replayed
by the persisted candidate. Existing snapshots without this required
provenance remain invalid for release; there is no approved production
snapshot today, so no migration or compatibility exception is needed.

## Tests

Regression coverage will prove that:

1. mixed NEIS `LOAD_DTM` values preserve per-record dates and build a sorted
   raw-date histogram;
2. the 90-day boundary is accepted and a 91-day range is rejected;
3. malformed dates, zero/incorrect counts, and an institution date outside
   the source histogram fail snapshot verification and approval before any
   pointer mutation;
4. the review packet is deterministic, hash-bound, and includes only the
   aggregate date range/counts; and
5. the administrator guide documents the review step without adding this
   material to the public interface.
