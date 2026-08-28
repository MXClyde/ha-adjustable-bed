# Phase 4B odd-cluster handoff (2026-08-28)

This is the repository accounting note for the interrupted odd-cluster Phase 4B run. It records
the completed cluster-007 result for later issue #436/#443 bookkeeping. Raw artifacts,
decompilation output, reports, audit history, and reproducer logs remain machine-local under the
ignored `disassembly/output/phase4-early/` tree.

## Run accounting

Assigned clusters: `007`, `009`, `011`, `013`, `015`, `017`, `019`, `021`, `023`, and `025`.

Cluster 007 is **COMPLETE**. Work stopped before cluster 009, so clusters 009 through 025 were not
started by this run. No integration source or historical protocol documentation was inspected or
modified.

## Cluster 007 packages

| Role | Package | Version | Artifact-set SHA-256 | Final route | Report manifest SHA-256 | Audit result |
|---|---|---:|---|---|---|---|
| Representative | `com.malouf.bedbase` | 2.4.3 (54) | `d9b242a8dda6772f62c5b8f21fe13b462af2533b82a16d67ee7a4a841e6ff894` | FULL | `746d913ad256afd0deffe42e989a96c4e2f4c7aa941add5343a65f2236f9e5de` | ACCEPT, round 003 |
| Sibling | `com.lucid.bedbase` | 1.3.3 (16) | `88162f4b6adc2cf0d0d5ee4252fe4de3e6564db1881e42ed0a34db3ba58ad148` | FULL, promoted from planned DELTA | `8d21996769e6d209345dc38ac84bb178b9fe70293305f22a39227dfe9f3a880c` | ACCEPT, round 007 |

The cluster input audit passed every artifact member, identity, signer, ABI, canonical prompt,
schema, and tooling check.

## Report counts

| Package | Inventory | Candidates | Commands | Protocol split | Vectors | Gates | Package audits |
|---|---:|---:|---:|---|---:|---:|---:|
| `com.malouf.bedbase` | 23 | 19 | 69 | 30 + 39 | 27 | 17/17 PASS | 3 |
| `com.lucid.bedbase` | 32 | 133 | 174 | 112 + 62 | 19 | 17/17 PASS | 7 |

The representative audit rejected rounds 001 and 002 before accepting round 003. The sibling
audit rejected rounds 001 through 006 before accepting round 007. All rejected revisions and audit
history are preserved machine-locally.

## Reconciliation

The accepted reconciliation covers all 11 frozen delta-checklist areas:

| Result | Areas |
|---|---|
| SAME | GATT roles; packet construction; framing/authentication; notification parsing; application stack/native payloads |
| DIFFERENT | delivery; manifest/discovery; BLE callsites; reachable command/STOP/timing routes; resources/variants; capability routing |
| INCOMPLETE | none |

Both reports contain the same 68 normalized controller terminal families with no terminal-only
delta. The packages nevertheless differ in six semantic checklist areas, including reachable scan,
preset, model, and capability routes. The frozen promotion rule therefore requires a FULL sibling
route rather than DELTA.

Reconciliation accounting:

- 11/11 areas reconciled: 5 SAME, 6 DIFFERENT, 0 incomplete;
- 2 unique members: 2 FULL, 0 DELTA, 0 missing, 0 duplicate, 0 unaccounted;
- 20 exact source anchors;
- 140 source-bound mutation cases, all 140 rejected;
- deterministic Markdown, JSON, search-log, and validation-log agreement; and
- reconciliation audits 001 through 004 REJECT, round 005 ACCEPT.

The final reconciliation `REPORT.SHA256` has SHA-256
`fd9492a0f0a17ba454247ad9532c5e5f309cbc1090c20a5c9519e8bc8c820239`. Its accepted round-005
audit has SHA-256 `94c824326a6c8391065c15e48aa5e5c59401b6b89dec784c53bf84fd6e12769c`.
Both package reports, the reconciliation report, audit histories, and the final machine-local
handoff are read-only and their manifests verify after freeze.

## Deferred external validation

Artifact analysis has no remaining blocker. Physical-device acceptance, actuator semantics,
multi-service ordering, and runtime notification values remain deferred external validation. They
do not make either report or the cluster reconciliation partial.

## Tracker-ready accounting

- Cluster 007: COMPLETE.
- `com.malouf.bedbase` 2.4.3: FULL, COMPLETE, independently accepted and frozen.
- `com.lucid.bedbase` 1.3.3: promoted from DELTA to FULL, COMPLETE, independently accepted and
  frozen.
- Cluster total: 2 FULL, 0 DELTA, 0 blocked, 0 missing, 0 duplicate, 0 unaccounted.
- Assigned clusters 009 through 025: not started by this interrupted run.
- Origin: the scheduled Phase 4B bulk run tracked by issues #436 and #443, with the durable
  repository handoff carried by PR #528.
- Registry: issue #443 records both exact cluster-007 package entries as `ACCEPTED / COUNTED`;
  issue #436 lists both under completed scheduled-bulk reports and deducts them from its remaining
  queue accounting.
- Bulk disposition: these are scheduled-bulk completions, not early analyses. They therefore do
  not belong in issue #447's `ACCEPTED / EXCLUDE FROM BULK` registry.
