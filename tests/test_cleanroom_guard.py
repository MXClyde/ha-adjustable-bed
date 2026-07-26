"""Guards for the clean-room analysis isolation boundary.

Clean-room APK analyses (issues #436, #443, #447) require that an analyst subagent recovers a
protocol from the artifact alone. Claude Code auto-injects ``CLAUDE.md`` into subagents based on
working directory, so an instruction file sitting above a run's workspace silently becomes
evidence. That happened twice: ``com.leggett.prodigy4`` run 1 was rejected and discarded, and
``de.octoactuators.octosmartcontrolapp`` was registered PARTIAL.

These tests exist because the dangerous file cannot be caught by review. ``disassembly/`` is
gitignored in full, and ``.gitignore``'s ``CLAUDE.md`` entry has no leading slash so it matches at
every depth. A reintroduced symlink would therefore be invisible in a diff forever. Only a
filesystem check can see it.

Pure filesystem and regex work: no Home Assistant imports, no fixtures, sub-second.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that legitimately contain their own checkout or vendored tree.
_PRUNED = {
    ".git",
    ".venv",
    ".worktrees",
    ".claude",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "smartbed-mqtt",
    "smartbed-mqtt-discord-chats",
}

# Filenames a harness auto-injects based on working directory. Outside the repo root, either one
# is an ancestor of a run workspace and reaches the analyst unbidden.
_INJECTED_FILENAMES = frozenset({"CLAUDE.md", "AGENTS.md"})

# Instruction files that the harness may inject into a clean-room analyst, plus the documents we
# hand it deliberately. All must be free of protocol values. The schema is copied into every run's
# input/, so a UUID or packet example added to a description or default would contaminate the run
# just as surely as one in the prompt.
_TRACKED_ANALYST_DOCS = (
    "docs/apk-analysis/TOOLING.md",
    "docs/apk-analysis/phase4-analyst-prompt.md",
    "docs/apk-analysis/analysis.schema.json",
)


def _analyst_visible_docs() -> tuple[str, ...]:
    """Return every document the analyst can see, including untracked root instruction files.

    The walk above deliberately permits instruction files at the repo root, so their *contents*
    have to be scanned instead. ``CLAUDE.md`` is gitignored at every depth, so a machine-local root
    copy (or a symlink to something else entirely) would otherwise carry protocol answers into
    every analyst while remaining invisible to both review and this guard.
    """
    roots = [name for name in sorted(_INJECTED_FILENAMES) if (REPO_ROOT / name).is_file()]
    return (*roots, *_TRACKED_ANALYST_DOCS)


_ANALYST_VISIBLE_DOCS = _analyst_visible_docs()

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A short UUID is only recognisable from its label, and "Service: FFE0" is as much an answer as the
# full 128-bit form, so the surrounding GATT vocabulary is part of the signal.
_UUID_CONTEXT = re.compile(r"\b(?:uuid|service|characteristic|descriptor)s?\b", re.IGNORECASE)
# A short-form UUID is 4, 8 or 32 hex digits containing both a letter and a numeral. All three
# constraints earn their place: the fixed lengths rule out git SHAs, the required letter rules out
# decimal numbers such as issue references, and the required numeral rules out hex-shaped BLE
# acronyms such as CCCD. Each would otherwise fire on ordinary prose mentioning a service.
_SHORT_UUID = re.compile(
    r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)"
    r"(?:[0-9a-fA-F]{4}|[0-9a-fA-F]{8}|[0-9a-fA-F]{32})\b"
)
# An explicit assignment such as "Service UUID: 1234" names the value outright, so a numeric-only
# short UUID counts there. It cannot count on mere GATT vocabulary, because a bare four-digit run
# is indistinguishable from an issue reference in prose like "the service broke in #1188".
_LABELLED_UUID = re.compile(
    r"\b(?:uuid|service|characteristic|descriptor)s?\b[^\n:=]{0,20}[:=]\s*"
    r"(?:0x)?[0-9a-fA-F]{4}(?:[0-9a-fA-F]{4})?\b",
    re.IGNORECASE,
)
_HEX_BYTE = re.compile(r"0x[0-9a-fA-F]{2}\b")
# Packet vocabulary. "STOP command: 0x12" is a complete answer from a single byte, so one byte is
# enough once the line says what the byte is for.
_PACKET_CONTEXT = re.compile(
    r"\b(?:command|opcode|frame|packet|payload|checksum|crc|byte value)s?\b", re.IGNORECASE
)
# Uppercase pairs are the conventional way to write a packet, so two are enough to be a give-away.
_BYTE_SEQUENCE = re.compile(r"\b[0-9A-F]{2}(?:[\s,]+[0-9A-F]{2})+\b")
# Lower and mixed case need three, because two-letter hex words ("be ad") occur in ordinary prose.
_BYTE_SEQUENCE_LOOSE = re.compile(r"\b[0-9a-fA-F]{2}(?:[\s,]+[0-9a-fA-F]{2}){2,}\b")
# A bare pair of any case, which only counts alongside packet vocabulary.
_BYTE_SEQUENCE_PAIR = re.compile(r"\b[0-9a-fA-F]{2}(?:[\s,]+[0-9a-fA-F]{2})+\b")

# A pointer *to* the comparison notes, as opposed to a prohibition mentioning them by name.
# Both forms that previously existed in AGENTS.md are covered: a markdown link target, and a
# parenthetical "see `path`".
_POINTER = re.compile(
    r"\]\([^)]*disassembly/(?:AGENTS|CLAUDE|PROTOCOL_NOTES)\.md"
    r"|see\s+\**\[?`?[^\s`)]*disassembly/(?:AGENTS|CLAUDE|PROTOCOL_NOTES)\.md",
    re.IGNORECASE,
)


# A model or module code: letters then a digit, e.g. a control-box part number. Brand names alone
# are fine and appear throughout this repo's docs; a model code sitting next to a description of
# how that model behaves on the wire is what gives an answer away in prose rather than in hex.
_MODEL_CODE = re.compile(r"\b[A-Z]{2,}[0-9][A-Z0-9]*\b")
_BEHAVIOUR_CONTEXT = re.compile(
    r"\b(?:stops?|release|refresh|framing|checksum|handshake|keep-?alive|preamble|"
    r"terminat\w+|acknowledg\w+)\b",
    re.IGNORECASE,
)

# The BLOCKED rule names device-name matching and command repeat/hold timing as answer keys, so
# both need a detector. The vocabulary is deliberately narrow on each side.
#
# Names: only advertised-identity wording counts. "File-name patterns" in the tooling guide is
# about which source files to open, and a discovery rule is worthless without a literal to match,
# so a bare mention of "device-name rule" stays clean.
_NAME_CONTEXT = re.compile(
    r"\b(?:device[- ]names?|local[- ]names?|advertised names?|name prefix(?:es)?|BLE names?)\b",
    re.IGNORECASE,
)
# A quoted filename is not a device name, and the rule text itself cites `SEARCH_LOG.md`, so
# tokens ending in a file extension are excluded before anything else.
_NAME_LITERAL = re.compile(
    r"[\"'`](?![^\"'`]*\.[A-Za-z]{2,4}[\"'`])[A-Za-z0-9_.*^$\[\]|-]{2,}[\"'`]"
    r"|\b[A-Z0-9_]{3,}\s*\*"
)

# Timing: only a repeat/hold cadence counts. The integration's own connection backoff and idle
# timeouts are documented in AGENTS.md and are explicitly not protocol evidence, so "30-50ms
# intervals" and "5-7.5s delays" must stay clean or the guard fires on its own instruction file.
_REPEAT_CONTEXT = re.compile(
    r"\b(?:repeat|resend|re-send|refresh|hold|held|press-and-hold|pulse)\w*\b", re.IGNORECASE
)
_TIMING_VALUE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|msec|millisecond|sec|second)s?\b", re.IGNORECASE
)


def _answer_key_violation(line: str) -> str | None:
    """Describe why a line reads as protocol evidence, or return None if it is clean.

    Every injection source runs through this one predicate, so a form added here is caught in all
    of them rather than in whichever loop happened to be updated.
    """
    if _UUID.search(line):
        return "UUID literal"
    if _UUID_CONTEXT.search(line) and _SHORT_UUID.search(line):
        return "short UUID on a GATT line"
    if _LABELLED_UUID.search(line):
        return "short UUID assigned to a GATT label"
    # A lone byte (e.g. a config default) is fine; a pair starts describing a packet.
    if len(_HEX_BYTE.findall(line)) >= 2:
        return "multiple hex bytes"
    if _BYTE_SEQUENCE.search(line) or _BYTE_SEQUENCE_LOOSE.search(line):
        return "byte sequence"
    if _PACKET_CONTEXT.search(line) and (
        _HEX_BYTE.search(line) or _BYTE_SEQUENCE_PAIR.search(line)
    ):
        return "byte in packet context"
    # Prose is an answer too: "<model> stops by ending its held-command refresh" is exactly the
    # STOP/release behaviour the analyst is required to recover from the artifact.
    if _MODEL_CODE.search(line) and _BEHAVIOUR_CONTEXT.search(line):
        return "model-specific protocol behaviour in prose"
    if _NAME_CONTEXT.search(line) and _NAME_LITERAL.search(line):
        return "device-name matching pattern"
    if _REPEAT_CONTEXT.search(line) and _TIMING_VALUE.search(line):
        return "command repeat/hold timing"
    return None


def _answer_key_violations(text: str) -> list[str]:
    """Report every offending line as ``<line number>: <reason> -> <excerpt>``."""
    return [
        f"{number}: {reason} -> {line.strip()[:90]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if (reason := _answer_key_violation(line))
    ]


def _walk_repo() -> list[Path]:
    """Yield every file in the repo, skipping vendored and worktree trees."""
    found: list[Path] = []
    stack = [REPO_ROOT]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if child.is_dir():
                if child.name not in _PRUNED:
                    stack.append(child)
            else:
                found.append(child)
    return found


def test_no_directory_scoped_instruction_file() -> None:
    """Only the repo root may hold an auto-injected instruction file.

    Both names are injected by working directory, so either one sitting above a run workspace
    reaches the analyst before its first action. The known offender was ``disassembly/CLAUDE.md``,
    a symlink to the protocol notes; deleting it left the notes themselves at
    ``disassembly/AGENTS.md``, which is injected by the same mechanism under the other name. They
    now live at ``disassembly/PROTOCOL_NOTES.md``, which no harness injects.
    """
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _walk_repo()
        if path.name in _INJECTED_FILENAMES and path.parent != REPO_ROOT
    ]
    assert not offenders, (
        f"{sorted(_INJECTED_FILENAMES)} are auto-injected into any agent working beneath them, "
        f"which contaminates a clean-room run. Rename to a name no harness injects: {offenders}"
    )


@pytest.mark.parametrize("relative_path", _ANALYST_VISIBLE_DOCS)
def test_injected_instructions_carry_no_answer_key(relative_path: str) -> None:
    """Documents an analyst can see must not hand them the protocol.

    These files are either auto-injected or deliberately supplied, so any UUID, byte value or
    device-name pattern in them is evidence the analyst did not have to earn.
    """
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing"

    violations = _answer_key_violations(path.read_text(encoding="utf-8"))

    assert not violations, (
        f"{relative_path} is visible to a clean-room analyst and must state no protocol values. "
        f"Move them to disassembly/PROTOCOL_NOTES.md (comparison pass only). Found:\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "line",
    [
        "Service UUID: 0000ffe0-0000-1000-8000-00805f9b34fb",
        "Service: FFE0",
        "the characteristic is 180A",
        "the frame is AA BB",
        "the frame is aa bb cc",
        "the frame is AA, BB, CC",
        "prefix 0x12 then 0x34",
        "STOP command: 0x12",
        "the command is aa bb",
        "Service UUID: 1234",
        "characteristic = 0x1812",
        "Device-name pattern: BED_*",
        "the local name is `SLEEP-1`",
        "Protocol X repeats every 150 ms",
        "hold the button and resend every 100 ms",
    ],
)
def test_answer_key_forms_are_detected(line: str) -> None:
    """The detector has to cover the ways a protocol value is actually written down.

    Short UUIDs, lowercase bytes and comma-separated bytes are all ordinary documentation forms,
    and each one is a complete answer key on its own.
    """
    assert _answer_key_violation(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "- write type, characteristic properties, CCCD value, MTU, bonding",
        "the service regressed in e8fb668 and was fixed in #1188",
        "Registers conservative BLE connection parameters (30-50ms intervals)",
        "the service is to be added",
        "Connection retry with progressive backoff (3 attempts, 5-7.5s delays)",
        "Auto-disconnect after configurable idle time (default 40s)",
        "File-name patterns worth opening directly: `*Ble*`, `*Bluetooth*`",
        "- exact local-name rules, prefixes, suffixes, regexes, and exclusions",
    ],
)
def test_ordinary_prose_is_not_flagged(line: str) -> None:
    """A guard that fires on its own instruction files would just be switched off.

    Hex-shaped acronyms, git SHAs, issue numbers and two-letter words all live on lines that
    legitimately mention GATT vocabulary. The integration's own connection timeouts and the
    prompt's own description of what to look for must stay clean for the same reason.
    """
    assert _answer_key_violation(line) is None


def test_no_analyst_visible_doc_points_at_the_comparison_notes() -> None:
    """Nothing may send a reader to the machine-local protocol notes.

    Naming the file in a prohibition is fine and intended; linking to it as a place to go is the
    regression this guards. It is also untracked, so such a pointer dangles on a fresh clone.

    Every root instruction file is checked, not just the tracked ``AGENTS.md``. A pointer carries
    no protocol value of its own, so the answer-key scan passes it, and a machine-local root
    ``CLAUDE.md`` is gitignored: a "see disassembly/PROTOCOL_NOTES.md" line there would reach every
    analyst while being invisible to review and to every other check here.
    """
    candidates = [
        *(REPO_ROOT / name for name in sorted(_INJECTED_FILENAMES)),
        *(REPO_ROOT / "docs").rglob("*.md"),
    ]
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in candidates
        if path.is_file()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _POINTER.search(line)
    ]
    assert not offenders, (
        "disassembly/PROTOCOL_NOTES.md is untracked and is comparison material; point readers at "
        f"docs/apk-analysis/TOOLING.md instead: {offenders}"
    )


def test_nullable_protocol_sections_require_an_explanation() -> None:
    """Every nullable protocol section needs a companion ``<field>_unknown_reason``.

    The prompt tells the analyst to report an unknown as null plus an explanation. A bare null
    would otherwise validate, leaving later matrix construction unable to tell an exhaustively
    searched absence from a section nobody looked at. Adding a nullable section without its
    explanation clause silently reopens that hole, so the two lists are checked against each other.
    """
    item = _load_schema()["properties"]["protocols"]["items"]
    nullable = {
        name for name, spec in item["properties"].items() if "null" in (spec.get("type") or [])
    }
    explained = {
        clause["then"]["required"][0].removesuffix("_unknown_reason") for clause in item["allOf"]
    }
    assert nullable == explained, (
        "Nullable protocol sections and their explanation clauses have drifted. "
        f"Nullable without a clause: {sorted(nullable - explained)}. "
        f"Clause without a nullable section: {sorted(explained - nullable)}."
    )
    missing_property = {f"{name}_unknown_reason" for name in nullable} - set(item["properties"])
    assert not missing_property, (
        f"Explanation fields are constrained nowhere, so any value would pass: {sorted(missing_property)}"
    )


def _load_schema() -> dict:
    return json.loads(
        (REPO_ROOT / "docs/apk-analysis/analysis.schema.json").read_text(encoding="utf-8")
    )


def _load_prompt() -> str:
    return (REPO_ROOT / "docs/apk-analysis/phase4-analyst-prompt.md").read_text(encoding="utf-8")


def test_schema_revision_matches_prompt() -> None:
    """The tracked prompt must pin the tracked schema's revision.

    These drifted apart once already, when both lived only as per-workspace copies.
    """
    revision = _load_schema()["properties"]["schema_revision"]["const"]
    assert revision in _load_prompt(), (
        f"analysis.schema.json pins revision {revision!r}, which the analyst prompt does not "
        "mention. One of the two has drifted."
    )


# A gate bullet in the prompt, e.g. "- `identity_verified` — Input identity ...".
_PROMPT_GATE_ID = re.compile(r"^- `([a-z_]+)` [—-] ")


def test_schema_gate_ids_match_prompt() -> None:
    """The schema's canonical gate IDs must be exactly the gates the prompt lists.

    The schema requires one entry per gate so a COMPLETE report cannot validate on a single
    arbitrary passing gate. That only holds while both lists agree, and only the prompt is read
    by the analyst.
    """
    schema = _load_schema()
    schema_ids = set(schema["$defs"]["gate_id"]["enum"])
    conditional_ids = set(schema["$defs"]["conditional_gate_id"]["enum"])
    gates = schema["properties"]["completion_gates"]

    section = _load_prompt().split("COMPLETION GATES", 1)
    assert len(section) == 2, "The analyst prompt no longer has a COMPLETION GATES section"
    prompt_ids = {
        match.group(1) for line in section[1].splitlines() if (match := _PROMPT_GATE_ID.match(line))
    }

    assert prompt_ids == schema_ids, (
        "The analyst prompt and analysis.schema.json list different completion gates. "
        f"Prompt only: {sorted(prompt_ids - schema_ids)}. Schema only: {sorted(schema_ids - prompt_ids)}."
    )
    assert conditional_ids <= schema_ids, (
        f"conditional_gate_id names gates that do not exist: {sorted(conditional_ids - schema_ids)}"
    )
    # Bounding the array at the gate count is what makes each contains clause mean "exactly one":
    # without maxItems a report could file the same gate twice with conflicting results.
    assert gates["minItems"] == gates["maxItems"] == len(schema_ids), (
        f"completion_gates is bounded [{gates['minItems']}, {gates['maxItems']}] for "
        f"{len(schema_ids)} gates; both bounds must equal the gate count"
    )
    required_present = {
        clause["contains"]["properties"]["gate"]["const"] for clause in gates["allOf"]
    }
    assert required_present == schema_ids, (
        "Every gate needs a contains clause, otherwise a report can omit it: "
        f"missing {sorted(schema_ids - required_present)}"
    )


# The per-project auto-memory index is injected into every subagent regardless of working
# directory, so it is an analyst-visible document even though it lives outside the repo. Detail
# belongs in the linked per-topic files, which are not injected.
def _memory_index_for_checkout() -> Path:
    """Locate this checkout's auto-memory index.

    Claude derives the project key from the checkout's absolute path, so hard-coding one
    maintainer's path would silently skip this test on every other checkout, including CI images
    and worktrees, exactly where an unnoticed index could be carrying answers.
    """
    project_key = str(REPO_ROOT).replace("/", "-").replace(" ", "-")
    return Path.home() / ".claude" / "projects" / project_key / "memory" / "MEMORY.md"


_MEMORY_INDEX = _memory_index_for_checkout()


@pytest.mark.skipif(
    not _MEMORY_INDEX.is_file(), reason="auto-memory index is machine-local; absent in CI"
)
def test_auto_memory_index_carries_no_answer_key() -> None:
    """The injected memory index must not summarise protocols in bytes.

    Discovered by probing a subagent: this file is injected everywhere, and it carried framing
    bytes and characteristic identifiers. A clean-room analyst following the prompt's injected-file
    rule would have to report BLOCKED on every run until it is clean.
    """
    violations = _answer_key_violations(_MEMORY_INDEX.read_text(encoding="utf-8"))

    assert not violations, (
        "The auto-memory index is injected into every agent, including clean-room analysts. "
        "Keep protocol values in the linked per-topic memory files, which are not injected. "
        "Found:\n" + "\n".join(violations)
    )
