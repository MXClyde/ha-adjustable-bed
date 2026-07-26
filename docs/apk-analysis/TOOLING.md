# APK decompilation tooling

<!--
METHOD ONLY. This document must never state a service or characteristic UUID, a command byte,
a packet framing or checksum description, a timing constant, a device-name pattern, a bed brand,
or a protocol-family name. It is written to be safe to hand to a clean-room analyst, who must
recover all of that from the artifact alone. If any such value appears here, that is a repository
defect: fix the document, and treat any run that read it as contaminated.

Protocol knowledge and cross-app comparison material live in the machine-local, gitignored
`disassembly/PROTOCOL_NOTES.md`, which is for the post-freeze comparison pass only.
-->

How to decompile an Android bed-controller app and where its logic hides. This is tooling and
method only, by construction: it deliberately contains no protocol values, so it can be given to a
clean-room analyst without contaminating the run.

For the analysis itself, use the canonical prompt in
[`phase4-analyst-prompt.md`](phase4-analyst-prompt.md) and the pinned schema in
[`analysis.schema.json`](analysis.schema.json). See the root `AGENTS.md` section "Mandatory
clean-room analysis" for the rules that govern a run.

Throughout, `<WORKSPACE>` is the run's workspace directory.

## Thoroughness over speed

Reverse engineering is not a summarization task. A missed code path or an unverified constant
produces a broken implementation that costs far more time than the analysis saved.

- Never stop at the first match. One UUID, one protocol class, or one working code path is a
  starting point, not an answer.
- A negative result is not proven by a single failed search. Record what you searched and where
  before concluding that something is absent.
- Treat decompiler output as a hypothesis. Confirm against resources, smali, disassembly, and
  multiple independent references.
- Distinguish app-owned live code from dead code, sample code, third-party SDKs, firmware
  payloads, tests, and unused assets.
- Preserve exact bytes, signed and unsigned conversions, endianness, masks, offsets, encodings,
  and state preconditions. Do not paraphrase a transformation.
- Do not fill a gap with a plausible value. Mark it uncertain, or stop and report the blocker.

## Hardware validation is deferred

A report may be complete while its findings remain unverified on physical hardware. Static
evidence and hardware confirmation are separate axes.

Do not mark a report incomplete solely because its statically recovered behavior has not been
exercised on a real bed, and do not present hardware testing as an immediately actionable
maintainer task. Mark such results as statically verified and hardware unverified, and treat
physical checks as deferred external validation by real users after a beta or release.

## Workflow

### 1. Identify the artifact

```bash
# .apk
aapt dump badging <app>.apk | grep package:

# .xapk (a zip of the base APK plus splits)
unzip -p <app>.xapk manifest.json | jq .package_name
```

Record the package ID, version name, version code, and the hash of every file in the delivery set
before decompiling anything.

### 2. Create the workspace

```bash
mkdir -p "<WORKSPACE>"/{input,work,report}
```

Give every run a new directory. Never seed one with an older decompilation or report.

### 3. Decompile with jadx

```bash
jadx --show-bad-code --comments-level info --threads-count 4 \
    -d "<WORKSPACE>"/work/jadx <ALL_RELEVANT_APKS>
```

For an XAPK, unpack the set first and decompile the base APK together with any split that carries
code.

**The flags matter. Do not fall back to a bare `--no-res` one-liner:**

- **`--show-bad-code` (always).** Without it, jadx silently drops methods it cannot fully
  decompile, which are frequently the exact methods doing bit manipulation and checksums. A
  dropped method reads as "no such command exists" and produces a confidently wrong conclusion.
- **Do a resource pass.** `res/values/arrays.xml` and `strings.xml` often carry model tables,
  remote-code lists, name patterns, and sometimes identifiers that never appear in code. Only add
  `--no-res` for a quick code-only smoke pass, never for the real run.
- **If the app is obfuscated** (classes named `a`, `b`, `c`), add `--deobf --deobf-min-length 3`
  so names are stable and greppable and cross-file tracing works.
- **When jadx cannot decompile a class at all**, fall back to smali. Bytecode always disassembles
  even when the Java reconstruction fails:

  ```bash
  apktool d <APK> -o "<WORKSPACE>"/work/smali/<APK_NAME>
  ```

### 4. Flutter apps: Blutter is mandatory

Flutter compiles Dart business logic to native code, so jadx shows only engine boilerplate and
none of the app's own logic. A jadx-only pass on a Flutter app is not an analysis.

```bash
unzip <APK> -d "<WORKSPACE>"/work/extracted/
ls "<WORKSPACE>"/work/extracted/lib/*/libapp.so   # present => Flutter
ls "<WORKSPACE>"/work/extracted/lib/               # available architectures

blutter "<WORKSPACE>"/work/extracted/lib/arm64-v8a "<WORKSPACE>"/work/blutter/
```

Blutter requires **arm64-v8a**. If the delivery set has only armeabi-v7a, stop and report the
blocker rather than proceeding on jadx output alone: an analyst may not leave the isolated
workspace to fetch another build, because that would change the artifact set whose identity and
hashes were verified. Acquiring an arm64 build of the same version is a maintainer task that
produces a new frozen-corpus entry for a fresh run.

Blutter output:

- `asm/` — Dart assembly with symbols; the primary search target
- `pp.txt` — object pool, where string constants usually surface
- `objs.txt` — object definitions
- `ida_script/` — IDA Pro scripts

### 5. Other stacks

Cover every stack that contains app logic, not just the one you found first:

- **React Native / Hermes** — analyse the shipped JS bundle. A Hermes bytecode bundle needs a
  Hermes disassembler; a plain bundle may ship with a source map, which is stronger evidence than
  any decompiler output.
- **Adobe AIR / Flash** — use FFDec on the shipped SWF.
- **Native JNI** — some apps implement transport in C or C++ in `lib*.so`.
- **Cordova / Capacitor** — the logic is in `assets/www`. Check for a shipped source map.

### 6. Where to search

```bash
# Characteristic writes: the boundary where a command becomes bytes
grep -ri "writeCharacteristic\|BluetoothGattCharacteristic" "<WORKSPACE>"/work/jadx/

# GATT plumbing and BLE manager classes
grep -ri "BluetoothGatt\|BleManager\|BluetoothLeService" "<WORKSPACE>"/work/jadx/

# Identifier and hex-constant surfaces. Never pipe a search through `head`: every hit has to be
# dispositioned, so redirect the full result set to a file in work/ instead of truncating it.
grep -riE "0x[0-9a-fA-F]{2}" --include="*.java" "<WORKSPACE>"/work/jadx/ > "<WORKSPACE>"/work/hex-constants.txt
grep -ri "uuid" "<WORKSPACE>"/work/jadx/

# Flutter
grep -ri "uuid\|characteristic\|write" "<WORKSPACE>"/work/blutter/asm/
```

File-name patterns worth opening directly: `*Ble*`, `*Bluetooth*`, `*Protocol*`, `*Command*`,
`*Manager*`, and anything named for a transport or a remote.

## Tools reference

### One-shot setup on Ubuntu/WSL

```bash
sudo apt update
sudo apt install -y openjdk-17-jdk unzip jq ripgrep
uv tool install check-jsonschema   # Draft 2020-12 validator, runs offline once installed
# jadx, apktool, blutter, aapt2 and ffdec are installed per their own instructions below
```

Install `check-jsonschema` before the workspace is handed over. The completion gates require the
report to validate against the pinned schema, and without a validator in the toolchain that gate
cannot be substantiated from inside the isolated workspace.

### jadx

Java/Kotlin decompiler. Primary tool for JVM-stack apps.

### apktool

Resource and smali decompiler. The authoritative fallback when jadx output is missing or
suspicious, and the way to read resources that jadx skipped.

### blutter

Flutter/Dart decompiler. Mandatory for Flutter apps, arm64-v8a only.

Its input is the **architecture directory** holding `libapp.so`, not the library file itself:
upstream is `python3 blutter.py <lib/arm64-v8a> <output_dir>`.

Note: the wrapper changes directory internally, so always pass **absolute, quoted** paths. A
relative path, or one pointing at the library instead of its directory, fails with the same
misleading "cannot find libapp file".

### aapt / aapt2

Reads the manifest, package ID, version name and code from an APK.

### ffdec (JPEXS Free Flash Decompiler)

Decompiles SWF, for Adobe AIR apps.

### check-jsonschema

Validates the finished report against the pinned schema. Run it before declaring a run complete:

```bash
check-jsonschema --schemafile "<WORKSPACE>"/input/analysis.schema.json \
    "<WORKSPACE>"/report/analysis.json
```

A report that does not validate is not finished, whatever its status field says.

## Tips

1. **Follow the writes.** Find the characteristic-write calls and trace backwards. The write
   boundary is where a named constant becomes actual bytes, and only those bytes are the protocol.
2. **Trace transformations to the end.** A constant that looks like a command may be wrapped,
   masked, checksummed, escaped, or reordered before transmission. The value at the write call is
   the only one that counts.
3. **Check for obfuscation.** ProGuard and R8 rename classes; string constants and resource names
   often survive and reveal intent when identifiers do not.
4. **Read the debug logging.** Log calls frequently name the very thing the code obscures.
5. **Check native libraries.** Some apps push the transport into C or C++ via JNI.
6. **Check for variants.** Many apps drive several device models over different code paths. Look
   for switch statements, model checks, and version handling, and document every variant found.
7. **When stuck, widen the search.** Try synonyms, abbreviations, and partial matches. The logic
   may sit in a native library, an obfuscated class, generated code, or a resource file.
8. **Be exhaustive, not fast.** Run multiple search variations, check every code path, and verify
   every value.
