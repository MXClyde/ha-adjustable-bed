"""Read-only integrity checks for one frozen Phase 4 report directory.

The validator treats the directory as hostile input. Relative members are opened
through directory file descriptors with ``O_NOFOLLOW`` and no report-local code
is imported or executed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast

VALIDATOR_REVISION = "phase4-v2-bundle-validator-v1"
REPORT_MANIFEST = "REPORT.SHA256"
_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  ([^\x00\r\n]+)$")
_READ_SIZE = 1024 * 1024

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class StrictJsonError(ValueError):
    """A JSON document is not strict, unambiguous JSON."""

    def __init__(self, reason: str, *, key: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.key = key


class _SnapshotError(RuntimeError):
    """A source tree could not be captured consistently."""

    def __init__(self, operation: str, path: str, error: OSError | None = None) -> None:
        super().__init__(operation)
        self.operation = operation
        self.path = path
        self.error = error


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One deterministic validation failure."""

    code: str
    path: str
    context: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a canonical JSON-compatible representation."""
        result: dict[str, object] = {"code": self.code, "path": self.path}
        if self.context:
            result["context"] = dict(self.context)
        return result


@dataclass(frozen=True, slots=True)
class ValidationReceipt:
    """Content-stable result of validating one report bundle."""

    validator_revision: str
    accepted: bool
    source_unchanged: bool
    bundle_sha256: str | None
    report_manifest_sha256: str | None
    discovered_members: int
    declared_members: int
    diagnostics: tuple[Diagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a canonical JSON-compatible representation."""
        return {
            "accepted": self.accepted,
            "bundle_sha256": self.bundle_sha256,
            "declared_members": self.declared_members,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "discovered_members": self.discovered_members,
            "report_manifest_sha256": self.report_manifest_sha256,
            "source_unchanged": self.source_unchanged,
            "validator_revision": self.validator_revision,
        }

    def to_json(self) -> str:
        """Return the deterministic single-line receipt."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _Node:
    path: str
    kind: str
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    sha256: str | None = None
    link_target: str | None = None

    def snapshot_bytes(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, slots=True)
class _TreeSnapshot:
    nodes: tuple[_Node, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    path: str
    sha256: str


def _duplicate_rejecting_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate_key", key=key)
        result[key] = value
    return result


def _reject_constant(value: str) -> JsonValue:
    raise StrictJsonError("non_finite_number", key=value)


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StrictJsonError("non_finite_number", key=value)
    return parsed


def load_json_strict(data: bytes) -> JsonValue:
    """Decode JSON while rejecting duplicate keys and non-finite numbers."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StrictJsonError("invalid_utf8") from error
    try:
        return cast(
            JsonValue,
            json.loads(
                text,
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
                parse_float=_parse_finite_float,
            ),
        )
    except StrictJsonError:
        raise
    except json.JSONDecodeError as error:
        raise StrictJsonError("invalid_json") from error


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "other"


def _stat_identity(node_stat: os.stat_result) -> tuple[int, ...]:
    return (
        node_stat.st_dev,
        node_stat.st_ino,
        stat.S_IFMT(node_stat.st_mode),
        stat.S_IMODE(node_stat.st_mode),
        node_stat.st_size,
        node_stat.st_mtime_ns,
        node_stat.st_ctime_ns,
    )


def _node_from_stat(
    relative: str,
    node_stat: os.stat_result,
    *,
    sha256: str | None = None,
    link_target: str | None = None,
) -> _Node:
    return _Node(
        path=relative,
        kind=_kind(node_stat.st_mode),
        mode=stat.S_IMODE(node_stat.st_mode),
        uid=node_stat.st_uid,
        gid=node_stat.st_gid,
        size=node_stat.st_size,
        mtime_ns=node_stat.st_mtime_ns,
        ctime_ns=node_stat.st_ctime_ns,
        device=node_stat.st_dev,
        inode=node_stat.st_ino,
        sha256=sha256,
        link_target=link_target,
    )


def _open_root(root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(root, flags)


def _hash_regular_at(directory_fd: int, name: str, expected: os.stat_result) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(expected):
            raise _SnapshotError("file_changed_while_opening", name)
        digest = hashlib.sha256()
        while chunk := os.read(file_fd, _READ_SIZE):
            digest.update(chunk)
        finished = os.fstat(file_fd)
        if _stat_identity(finished) != _stat_identity(opened):
            raise _SnapshotError("file_changed_while_reading", name)
        return digest.hexdigest()
    finally:
        os.close(file_fd)


def _scan_directory(directory_fd: int, prefix: PurePosixPath) -> list[_Node]:
    nodes: list[_Node] = []
    try:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        relative = prefix.as_posix() if prefix.parts else "."
        raise _SnapshotError("scan_directory", relative, error) from error

    for entry in entries:
        relative_path = prefix / entry.name
        relative = relative_path.as_posix()
        try:
            node_stat = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise _SnapshotError("stat_entry", relative, error) from error
        kind = _kind(node_stat.st_mode)
        if kind == "file":
            try:
                digest = _hash_regular_at(directory_fd, entry.name, node_stat)
            except OSError as error:
                raise _SnapshotError("read_file", relative, error) from error
            nodes.append(_node_from_stat(relative, node_stat, sha256=digest))
        elif kind == "symlink":
            try:
                target = os.readlink(entry.name, dir_fd=directory_fd)
            except OSError as error:
                raise _SnapshotError("read_symlink", relative, error) from error
            nodes.append(_node_from_stat(relative, node_stat, link_target=target))
        elif kind == "directory":
            nodes.append(_node_from_stat(relative, node_stat))
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
            except OSError as error:
                raise _SnapshotError("open_directory", relative, error) from error
            try:
                opened = os.fstat(child_fd)
                if _stat_identity(opened) != _stat_identity(node_stat):
                    raise _SnapshotError("directory_changed_while_opening", relative)
                nodes.extend(_scan_directory(child_fd, relative_path))
            finally:
                os.close(child_fd)
        else:
            nodes.append(_node_from_stat(relative, node_stat))
    return nodes


def capture_tree_snapshot(root: Path) -> _TreeSnapshot:
    """Capture metadata and file content without following a source symlink."""
    try:
        root_fd = _open_root(root)
    except OSError as error:
        raise _SnapshotError("open_root", ".", error) from error
    try:
        root_stat = os.fstat(root_fd)
        nodes = [_node_from_stat(".", root_stat)]
        nodes.extend(_scan_directory(root_fd, PurePosixPath()))
    finally:
        os.close(root_fd)
    ordered = tuple(sorted(nodes, key=lambda node: os.fsencode(node.path)))
    digest = hashlib.sha256()
    for node in ordered:
        digest.update(node.snapshot_bytes())
    return _TreeSnapshot(nodes=ordered, digest=digest.hexdigest())


def _safe_member_path(raw: str) -> PurePosixPath | None:
    if not raw or "\\" in raw or "\x00" in raw:
        return None
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or raw != candidate.as_posix():
        return None
    if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate


def _read_member(root: Path, member: PurePosixPath) -> bytes:
    root_fd = _open_root(root)
    current_fd = root_fd
    try:
        for part in member.parts[:-1]:
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            next_fd = os.open(part, flags, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_fd = os.open(member.parts[-1], flags, dir_fd=current_fd)
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("member is not a regular file")
            chunks: list[bytes] = []
            while chunk := os.read(file_fd, _READ_SIZE):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _parse_manifest(data: bytes) -> tuple[list[_ManifestEntry], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], [Diagnostic("MANIFEST_INVALID_UTF8", REPORT_MANIFEST)]
    if not text.endswith("\n"):
        diagnostics.append(Diagnostic("MANIFEST_MISSING_FINAL_NEWLINE", REPORT_MANIFEST))
    entries: list[_ManifestEntry] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _MANIFEST_LINE.fullmatch(line)
        if match is None:
            diagnostics.append(
                Diagnostic(
                    "MANIFEST_INVALID_LINE",
                    REPORT_MANIFEST,
                    (("line", str(line_number)),),
                )
            )
            continue
        digest, raw_path = match.groups()
        member = _safe_member_path(raw_path)
        if member is None:
            diagnostics.append(Diagnostic("PATH_ESCAPE", raw_path, (("line", str(line_number)),)))
            continue
        canonical = member.as_posix()
        if canonical == REPORT_MANIFEST:
            diagnostics.append(Diagnostic("MANIFEST_SELF_REFERENCE", canonical))
            continue
        if canonical in seen:
            diagnostics.append(Diagnostic("MANIFEST_DUPLICATE_MEMBER", canonical))
            continue
        seen.add(canonical)
        entries.append(_ManifestEntry(path=canonical, sha256=digest))
    return entries, diagnostics


def _diagnostic_for_snapshot(error: _SnapshotError) -> Diagnostic:
    context: tuple[tuple[str, str], ...] = (("operation", error.operation),)
    if error.error is not None and error.error.errno is not None:
        context += (("errno", str(error.error.errno)),)
    return Diagnostic("SOURCE_SNAPSHOT_FAILED", error.path, tuple(sorted(context)))


def _bundle_digest(nodes: dict[str, _Node]) -> str:
    digest = hashlib.sha256()
    for path in sorted(nodes, key=os.fsencode):
        node = nodes[path]
        if node.kind != "file" or path == REPORT_MANIFEST:
            continue
        digest.update(f"{path}\x00{node.size}\x00{node.sha256}\n".encode())
    return digest.hexdigest()


def _sorted_diagnostics(diagnostics: list[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(
        sorted(
            set(diagnostics),
            key=lambda item: (item.code, os.fsencode(item.path), item.context),
        )
    )


def validate_report_bundle(report_root: Path) -> ValidationReceipt:
    """Validate a report directory without modifying or executing anything in it."""
    diagnostics: list[Diagnostic] = []
    try:
        before = capture_tree_snapshot(report_root)
    except _SnapshotError as error:
        diagnostic = _diagnostic_for_snapshot(error)
        return ValidationReceipt(
            validator_revision=VALIDATOR_REVISION,
            accepted=False,
            source_unchanged=False,
            bundle_sha256=None,
            report_manifest_sha256=None,
            discovered_members=0,
            declared_members=0,
            diagnostics=(diagnostic,),
        )

    nodes = {node.path: node for node in before.nodes if node.path != "."}
    regular_members = {
        path for path, node in nodes.items() if node.kind == "file" and path != REPORT_MANIFEST
    }
    for path, node in nodes.items():
        if node.kind == "symlink":
            diagnostics.append(Diagnostic("SYMLINK_FORBIDDEN", path))
        elif node.kind not in {"file", "directory"}:
            diagnostics.append(Diagnostic("SPECIAL_NODE_FORBIDDEN", path, (("kind", node.kind),)))

    manifest_node = nodes.get(REPORT_MANIFEST)
    manifest_digest: str | None = None
    manifest_entries: list[_ManifestEntry] = []
    if manifest_node is None:
        diagnostics.append(Diagnostic("MANIFEST_MISSING", REPORT_MANIFEST))
    elif manifest_node.kind != "file":
        diagnostics.append(
            Diagnostic(
                "MANIFEST_NOT_REGULAR",
                REPORT_MANIFEST,
                (("kind", manifest_node.kind),),
            )
        )
    else:
        manifest_digest = manifest_node.sha256
        try:
            manifest_bytes = _read_member(report_root, PurePosixPath(REPORT_MANIFEST))
        except OSError as error:
            manifest_context = (("errno", str(error.errno)),) if error.errno is not None else ()
            diagnostics.append(Diagnostic("MANIFEST_UNREADABLE", REPORT_MANIFEST, manifest_context))
        else:
            manifest_entries, manifest_diagnostics = _parse_manifest(manifest_bytes)
            diagnostics.extend(manifest_diagnostics)

    declared = {entry.path: entry for entry in manifest_entries}
    for path in sorted(regular_members - set(declared), key=os.fsencode):
        diagnostics.append(Diagnostic("MEMBER_UNDECLARED", path))
    for path in sorted(set(declared) - set(nodes), key=os.fsencode):
        diagnostics.append(Diagnostic("MEMBER_MISSING", path))
    for path in sorted(set(declared) & set(nodes), key=os.fsencode):
        node = nodes[path]
        if node.kind != "file":
            diagnostics.append(Diagnostic("MEMBER_NOT_REGULAR", path, (("kind", node.kind),)))
            continue
        if node.sha256 != declared[path].sha256:
            diagnostics.append(Diagnostic("MEMBER_DIGEST_MISMATCH", path))

    for path in sorted(regular_members, key=os.fsencode):
        if not path.lower().endswith(".json"):
            continue
        try:
            data = _read_member(report_root, PurePosixPath(path))
            load_json_strict(data)
        except StrictJsonError as error:
            json_context: tuple[tuple[str, str], ...] = (("reason", error.reason),)
            if error.key is not None:
                json_context += (("key", error.key),)
            diagnostics.append(Diagnostic("JSON_NOT_STRICT", path, tuple(sorted(json_context))))
        except OSError as error:
            member_context = (("errno", str(error.errno)),) if error.errno is not None else ()
            diagnostics.append(Diagnostic("MEMBER_UNREADABLE", path, member_context))

    try:
        after = capture_tree_snapshot(report_root)
    except _SnapshotError as error:
        diagnostics.append(_diagnostic_for_snapshot(error))
        source_unchanged = False
    else:
        source_unchanged = before == after
        if not source_unchanged:
            diagnostics.append(Diagnostic("SOURCE_TREE_MUTATED", "."))

    ordered_diagnostics = _sorted_diagnostics(diagnostics)
    return ValidationReceipt(
        validator_revision=VALIDATOR_REVISION,
        accepted=not ordered_diagnostics,
        source_unchanged=source_unchanged,
        bundle_sha256=_bundle_digest(nodes),
        report_manifest_sha256=manifest_digest,
        discovered_members=len(regular_members),
        declared_members=len(declared),
        diagnostics=ordered_diagnostics,
    )
