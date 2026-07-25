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
# hand it deliberately. All must be free of protocol values.
_ANALYST_VISIBLE_DOCS = (
    "AGENTS.md",
    "docs/apk-analysis/TOOLING.md",
    "docs/apk-analysis/phase4-analyst-prompt.md",
)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UUID_FRAGMENT = re.compile(r"uuid", re.IGNORECASE)
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{4,}\b")
_HEX_BYTE = re.compile(r"0x[0-9a-fA-F]{2}\b")
_BYTE_SEQUENCE = re.compile(r"\b[0-9A-F]{2}(?:\s+[0-9A-F]{2})+\b")

# A pointer *to* the comparison notes, as opposed to a prohibition mentioning them by name.
# Both forms that previously existed in AGENTS.md are covered: a markdown link target, and a
# parenthetical "see `path`".
_POINTER = re.compile(
    r"\]\(disassembly/(?:AGENTS|CLAUDE)\.md|see\s+\**\[?`?disassembly/(?:AGENTS|CLAUDE)\.md",
    re.IGNORECASE,
)


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

    violations: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _UUID.search(line):
            violations.append(f"{number}: UUID literal -> {line.strip()[:90]}")
        elif _UUID_FRAGMENT.search(line) and _HEX_RUN.search(line):
            violations.append(f"{number}: hex run on a UUID line -> {line.strip()[:90]}")
        # A lone byte (e.g. a config default of 0x00) is fine; a pair starts describing a packet.
        elif len(_HEX_BYTE.findall(line)) >= 2:
            violations.append(f"{number}: multiple hex bytes -> {line.strip()[:90]}")
        elif _BYTE_SEQUENCE.search(line):
            violations.append(f"{number}: byte sequence -> {line.strip()[:90]}")

    assert not violations, (
        f"{relative_path} is visible to a clean-room analyst and must state no protocol values. "
        f"Move them to disassembly/AGENTS.md (comparison pass only). Found:\n"
        + "\n".join(violations)
    )


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


def test_schema_revision_matches_prompt() -> None:
    """The tracked prompt must pin the tracked schema's revision.

    These drifted apart once already, when both lived only as per-workspace copies.
    """
    schema = json.loads(
        (REPO_ROOT / "docs/apk-analysis/analysis.schema.json").read_text(encoding="utf-8")
    )
    revision = schema["properties"]["schema_revision"]["const"]
    prompt = (REPO_ROOT / "docs/apk-analysis/phase4-analyst-prompt.md").read_text(encoding="utf-8")
    assert revision in prompt, (
        f"analysis.schema.json pins revision {revision!r}, which the analyst prompt does not "
        "mention. One of the two has drifted."
    )


# The per-project auto-memory index is injected into every subagent regardless of working
# directory, so it is an analyst-visible document even though it lives outside the repo. Detail
# belongs in the linked per-topic files, which are not injected.
_MEMORY_INDEX = Path(
    os.path.expanduser(
        "~/.claude/projects/-home-kristoffer-Code-Home-Assistant-ha-adjustable-bed"
        "/memory/MEMORY.md"
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
    violations: list[str] = []
    for number, line in enumerate(_MEMORY_INDEX.read_text(encoding="utf-8").splitlines(), start=1):
        if (
            _UUID.search(line)
            or _BYTE_SEQUENCE.search(line)
            or len(_HEX_BYTE.findall(line)) >= 2
        ):
            violations.append(f"{number}: {line.strip()[:90]}")

    assert not violations, (
        "The auto-memory index is injected into every agent, including clean-room analysts. "
        "Keep protocol values in the linked per-topic memory files, which are not injected. "
        "Found:\n" + "\n".join(violations)
    )
