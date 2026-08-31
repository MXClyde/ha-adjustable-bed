# Phase 4 v2 preflight

This package inventories APK deliveries without extracting into or modifying the source tree.
It produces two identities:

- `delivery_digest` identifies the exact caller-supplied files.
- `artifact_digest` identifies the logical APK member set, independent of XAPK/APKS packaging.

Package identity is derived from the sealed APKs with `aapt2 dump badging`; `apksigner verify`
cryptographically verifies each APK and supplies its signer-certificate digests. A verified install
set has exactly one base, unique split names, identical package/version/signer identity, and every
required `uses-split` present. Missing tools, malformed or unsigned APKs, ambiguity, and any mismatch
remain precise fail-closed blockers. The overall decision remains `BLOCKED` until exhaustive
specialized/native stack detection is implemented. Recognized markers provide suggested analysis
routes only.

Cache objects contain APK bytes and byte identity only, are addressed by cache-schema revision plus
`artifact_digest`, and never retain package identity or classification output. Mutable processing
status is separately namespaced by pipeline revision. Materialization always copies verified bytes
and never hardlinks.

This slice requires Linux interfaces including `fcntl.flock` and `renameat2`; it also uses
`O_NOATIME` where the filesystem permits it. ZIP64 deliveries are rejected because the bounded
ZIP64 central-directory parser is not implemented yet. Operators should account for these platform
and archive limits when diagnosing rejected large `.apks` or `.xapk` deliveries.

Preflight seals every supplied delivery file and each expanded APK member into private temporary
storage. Peak use can therefore approach the supplied delivery bytes plus the expanded APK-member
bytes. Production runs should pass a persistent, capacity-checked `sealing_directory`; omitting it
uses the host `TMPDIR` default.
