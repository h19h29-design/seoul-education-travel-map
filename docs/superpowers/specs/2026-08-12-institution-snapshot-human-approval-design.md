# Institution snapshot human-approval design

## Context

The institution synchronizer currently fetches official sources, builds an
unapproved candidate, and immediately calls `promote_snapshot()` in the same
process. A successful data fetch therefore writes an approved manifest and
changes `resources/institution-snapshots/current.json` before a data steward
can inspect counts, quarantined rows, coordinate quality, or the previous
snapshot diff.

Production documentation already requires a human review. The executable
workflow must enforce that requirement instead of relying on a review after
promotion.

## Goals

- Make synchronization candidate-only and leave `current.json` unchanged.
- Provide a deterministic, privacy-safe review packet for a candidate.
- Require a separate, credential-free approval command tied to the exact
  candidate that was reviewed.
- Preserve the existing signed transaction, path containment, hash replay,
  lock, atomic promotion, and crash-recovery protections.
- Keep production release fail-closed until an approved snapshot exists.

## Non-goals

- Acquiring or storing NEIS, kindergarten, or Kakao credentials.
- Changing source mappings, reconciliation thresholds, geocoding, or snapshot
  schemas except where approval evidence requires it.
- Building a web administration interface, user accounts, or a database.
- Deploying the service or completing the 30-pair route review.

## Selected workflow

### 1. Build an unapproved candidate

`sync-institutions.py` remains the only networked institution-ingestion
command. With the three synchronization credentials it performs the existing
source, reconciliation, geocoding, Seoul-boundary, provenance, and quality
checks and writes `.<snapshot-id>.candidate` plus its signed build transaction.

The command must not call `promote_snapshot()` and must not create or modify
`current.json`. Its final JSON status is `CANDIDATE_REVIEW_REQUIRED` and contains
only safe review metadata and the snapshot ID. Existing credentials are
cleared on every success and failure path.

If quality issues exist, the command exits nonzero. The rejected candidate may
remain for diagnosis, but it cannot be approved.

### 2. Inspect the exact candidate

A new credential-free command, `review-institution-snapshot.py`, accepts
`--snapshot-id`, `--snapshot-root`, and `--geodata-root`. It revalidates the
candidate directory, signed transaction, manifest, record hashes, provenance,
coverage, previous-snapshot relationship, and quality gates without changing
any file.

It emits one canonical JSON review packet containing:

- status `CANDIDATE_REVIEW_REQUIRED`;
- snapshot ID, creation/as-of timestamps, and previous snapshot ID;
- source, institution-type, foundation, district, status, and coordinate-
  quality counts;
- quarantined institution and site IDs;
- added, changed, missing, closed-candidate, and site-only diff information;
- institutions, sites, and candidate-manifest hashes;
- a `reviewDigest`, calculated from the canonical packet fields before the
  digest field is appended.

The packet never contains names, addresses, coordinates, credentials, request
headers, provider responses, or raw source rows. Stable institution/site IDs
are retained because they are required to review quarantined records and are
already part of the approved operational audit contract.

The digest binds approval to the snapshot ID, candidate hashes, provenance,
quality counts, quarantine lists, and diff that the reviewer saw. It is a
review-evidence identifier, not a replacement for the existing signed build
transaction. Re-running inspection without a candidate change produces the
same digest.

### 3. Approve in a separate process

A new credential-free command, `approve-institution-snapshot.py`, requires:

```text
--snapshot-id <safe snapshot id>
--review-digest <64 lowercase hexadecimal characters>
--reviewer-role data-steward
```

It loads and revalidates the candidate, regenerates the review packet, and
compares the supplied digest using a constant-time comparison. A missing or
mismatched digest, unsupported role, quality issue, tampered file, stale
previous-snapshot relationship, or invalid transaction fails before any
promotion mutation.

After validation it invokes the existing locked atomic promotion path. The
approved manifest continues to record `approved=true`, `approvedAt`, and
`approvedByRole=data-steward`. The command then verifies the selected snapshot
through the production snapshot loader and emits only:

```json
{
  "status": "SNAPSHOT_APPROVED",
  "snapshotId": "...",
  "reviewDigest": "..."
}
```

The approval command performs no network requests and accepts no credential or
environment-file option.

## Component boundaries

The institution sync module gains read-only candidate loading and review-packet
construction functions. Both commands use those functions so validation and
digest semantics have one implementation. The low-level atomic promotion
implementation remains responsible only for verified filesystem transition
and recovery; the approval service owns human-review evidence.

The scripts remain thin adapters:

- `sync-institutions.py`: credentials, collection, candidate construction;
- `review-institution-snapshot.py`: read-only inspection and JSON output;
- `approve-institution-snapshot.py`: review-digest verification and promotion.

## Failure and recovery behavior

- Candidate build failure never changes the active snapshot.
- Inspection is read-only and repeatable.
- Approval rejects any candidate change after inspection because the review
  digest and signed transaction no longer match.
- Concurrent approvals retain the existing root promotion lock.
- A crash during approval retains the existing signed-journal recovery rules;
  re-running the same approval command is idempotent when the same candidate is
  already the verified current snapshot.
- A new candidate cannot silently replace a different current snapshot if its
  recorded previous snapshot no longer matches.

## Verification

Tests must first fail against the current immediate-promotion behavior, then
cover:

1. synchronization creates a clean candidate while leaving an absent or
   existing `current.json` byte-for-byte unchanged;
2. review output is deterministic, contains all required audit categories,
   and excludes names, addresses, coordinates, credentials, and raw payloads;
3. approval requires the exact review digest and `data-steward` role;
4. tampered candidate data, transaction data, provenance, review digest, path,
   symlink, and previous-snapshot state fail before pointer mutation;
5. successful approval verifies the approved manifest and current pointer;
6. approval is credential-free, offline, locked, and restart-idempotent;
7. the release gate remains blocked for candidate-only state and succeeds at
   the artifact-preflight stage only after explicit approval;
8. existing institution, security, release, and full application suites remain
   warning-clean.

## Operator documentation

The README will describe three distinct commands: synchronize, review, and
approve. It will state that the reviewer copies the digest from the inspection
output only after checking the full packet and that candidate creation alone
does not make a release deployable. Credentials remain confined to the first
command.
