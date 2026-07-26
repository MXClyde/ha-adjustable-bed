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
import os
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

# Instruction files that the harness may inject into a clean-room analyst, plus the documents we
# hand it deliberately. All must be free of protocol values. The schema is copied into every run's
# input/, so a UUID or packet example added to a description or default would contaminate the run
# just as surely as one in the prompt.
_ANALYST_VISIBLE_DOCS = (
    "AGENTS.md",
    "docs/apk-analysis/TOOLING.md",
    "docs/apk-analysis/phase4-analyst-prompt.md",
    "docs/apk-analysis/analysis.schema.json",
)

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
_HEX_BYTE = re.compile(r"0x[0-9a-fA-F]{2}\b")
# Uppercase pairs are the conventional way to write a packet, so two are enough to be a give-away.
_BYTE_SEQUENCE = re.compile(r"\b[0-9A-F]{2}(?:[\s,]+[0-9A-F]{2})+\b")
# Lower and mixed case need three, because two-letter hex words ("be ad") occur in ordinary prose.
_BYTE_SEQUENCE_LOOSE = re.compile(r"\b[0-9a-fA-F]{2}(?:[\s,]+[0-9a-fA-F]{2}){2,}\b")

# A pointer *to* the comparison notes, as opposed to a prohibition mentioning them by name.
# Both forms that previously existed in AGENTS.md are covered: a markdown link target, and a
# parenthetical "see `path`".
_POINTER = re.compile(
    r"\]\(disassembly/(?:AGENTS|CLAUDE)\.md|see\s+\**\[?`?disassembly/(?:AGENTS|CLAUDE)\.md",
    re.IGNORECASE,
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
    # A lone byte (e.g. a config default) is fine; a pair starts describing a packet.
    if len(_HEX_BYTE.findall(line)) >= 2:
        return "multiple hex bytes"
    if _BYTE_SEQUENCE.search(line) or _BYTE_SEQUENCE_LOOSE.search(line):
        return "byte sequence"
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


def test_no_directory_scoped_claude_md() -> None:
    """Only the repo root may hold a CLAUDE.md.

    A CLAUDE.md anywhere else is auto-injected into agents working beneath it. The known offender
    was ``disassembly/CLAUDE.md``, a symlink to protocol notes sitting directly above the
    clean-room workspaces.
    """
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in _walk_repo()
        if path.name == "CLAUDE.md" and path.parent != REPO_ROOT
    ]
    assert not offenders, (
        "Directory-scoped CLAUDE.md files are auto-injected into any agent working beneath them, "
        f"which contaminates a clean-room run: {offenders}"
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
        f"Move them to disassembly/AGENTS.md (comparison pass only). Found:\n"
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
    ],
)
def test_ordinary_prose_is_not_flagged(line: str) -> None:
    """A guard that fires on its own instruction files would just be switched off.

    Hex-shaped acronyms, git SHAs, issue numbers and two-letter words all live on lines that
    legitimately mention GATT vocabulary.
    """
    assert _answer_key_violation(line) is None


def test_no_tracked_doc_points_at_the_comparison_notes() -> None:
    """Nothing may send a reader to the machine-local protocol notes.

    Naming the file in a prohibition is fine and intended; linking to it as a place to go is the
    regression this guards. It is also untracked, so such a pointer dangles on a fresh clone.
    """
    candidates = [REPO_ROOT / "AGENTS.md", *(REPO_ROOT / "docs").rglob("*.md")]
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in candidates
        if path.is_file()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _POINTER.search(line)
    ]
    assert not offenders, (
        "disassembly/AGENTS.md is untracked and is comparison material; point readers at "
        f"docs/apk-analysis/TOOLING.md instead: {offenders}"
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
    assert gates["minItems"] == len(schema_ids), (
        f"completion_gates requires minItems {gates['minItems']} for {len(schema_ids)} gates"
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
_MEMORY_INDEX = Path(
    os.path.expanduser(
        "~/.claude/projects/-home-kristoffer-Code-Home-Assistant-ha-adjustable-bed/memory/MEMORY.md"
    )
)


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
