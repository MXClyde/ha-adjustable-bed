"""Focused tests for deterministic Phase 4 v2 delivery preflight."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

if sys.platform != "linux":
    pytest.skip("Phase 4 v2 preflight requires Linux", allow_module_level=True)

import tools.phase4_v2.preflight.core as legacy_preflight
from tools.phase4_v2.preflight import (
    ArtifactCache,
    CacheIntegrityError,
    PreflightError,
    PreflightLimits,
    SafetyError,
    StackDecision,
    preflight_delivery,
)


def _apk(path: Path, *markers: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in markers:
            archive.writestr(name, f"fixture:{name}")


def _native_apk(path: Path, *extra: str) -> None:
    _apk(path, "AndroidManifest.xml", "classes.dex", *extra)


def _snapshot(paths: list[Path]) -> dict[str, tuple[int, int, int, str]]:
    return {
        path.name: (
            stat.S_IMODE(path.stat().st_mode),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in paths
    }


def test_split_delivery_identity_is_order_independent_and_read_only(tmp_path: Path) -> None:
    base = tmp_path / "base.apk"
    split = tmp_path / "split_config.arm64_v8a.apk"
    _native_apk(base)
    _native_apk(split)
    before = _snapshot([base, split])

    first = preflight_delivery([base, split])
    second = preflight_delivery([split, base])

    assert first.manifest() == second.manifest()
    assert first.delivery_digest == second.delivery_digest
    assert first.artifact_digest == second.artifact_digest
    assert first.decision.status == "BLOCKED"
    assert first.decision.routes == ("apktool", "jadx")
    assert "split_coherence_not_verified" in first.decision.blockers
    assert {
        "package_identity_not_verified",
        "version_identity_not_verified",
        "signer_coherence_not_verified",
        "stack_detection_not_exhaustive",
    }.issubset(first.decision.blockers)
    assert _snapshot([base, split]) == before


def test_delivery_digest_changes_across_packaging_but_artifact_digest_does_not(
    tmp_path: Path,
) -> None:
    direct = tmp_path / "base.apk"
    _native_apk(direct)
    container = tmp_path / "delivery.xapk"
    with zipfile.ZipFile(container, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("metadata.json", "{}")
        archive.writestr("nested/base.apk", direct.read_bytes())

    direct_result = preflight_delivery([direct])
    container_result = preflight_delivery([container])

    assert direct_result.delivery_digest != container_result.delivery_digest
    assert direct_result.artifact_digest == container_result.artifact_digest
    assert container_result.artifact_members[0].name == "base.apk"


def test_explicit_sealing_directory_and_result_cleanup(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    sealing_root = tmp_path / "persistent-sealing"
    sealing_root.mkdir()

    with preflight_delivery([artifact], sealing_directory=sealing_root) as result:
        sealed = result.artifact_members[0]._sealed_path
        assert sealing_root in sealed.parents
        assert sealed.is_file()

    assert not sealed.exists()


@pytest.mark.parametrize("unsafe_name", ["../escape.apk", "/absolute.apk", "bad\\name.apk"])
def test_container_rejects_unsafe_member_names(tmp_path: Path, unsafe_name: str) -> None:
    inner = tmp_path / "inner.apk"
    _native_apk(inner)
    delivery = tmp_path / "unsafe.xapk"
    with zipfile.ZipFile(delivery, "w") as archive:
        archive.writestr(unsafe_name, inner.read_bytes())

    with pytest.raises(SafetyError, match="unsafe archive member"):
        preflight_delivery([delivery])


def test_container_rejects_symlink_and_duplicate_members(tmp_path: Path) -> None:
    symlink_delivery = tmp_path / "symlink.xapk"
    link = zipfile.ZipInfo("base.apk")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink_delivery, "w") as archive:
        archive.writestr(link, "target")
    with pytest.raises(SafetyError, match="non-regular"):
        preflight_delivery([symlink_delivery])

    duplicate_delivery = tmp_path / "duplicate.xapk"
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(duplicate_delivery, "w") as archive,
    ):
        archive.writestr("base.apk", b"one")
        archive.writestr("base.apk", b"two")
    with pytest.raises(SafetyError, match="duplicate archive member"):
        preflight_delivery([duplicate_delivery])


@pytest.mark.parametrize(
    ("marker", "stack", "route"),
    [
        ("lib/arm64-v8a/libapp.so", "flutter", "blutter"),
        ("assets/index.android.bundle.hbc", "hermes", "hermes-bundle"),
        ("assets/index.android.bundle", "react_native", "react-native-bundle"),
        ("assets/META-INF/AIR/application.xml", "air", "ffdec"),
    ],
)
def test_specialized_stack_routes_are_suggested_but_fail_closed(
    tmp_path: Path,
    marker: str,
    stack: str,
    route: str,
) -> None:
    artifact = tmp_path / f"{stack}.apk"
    _native_apk(artifact, marker)

    result = preflight_delivery([artifact])

    assert {"android", stack}.issubset(result.decision.stacks)
    assert {"apktool", "jadx", route}.issubset(result.decision.routes)
    assert result.decision.status == "BLOCKED"
    assert "stack_detection_not_exhaustive" in result.decision.blockers


def test_unknown_stack_blocks_pipeline_but_not_byte_cache(tmp_path: Path) -> None:
    artifact = tmp_path / "unknown.apk"
    _apk(artifact, "assets/unidentified.payload")

    result = preflight_delivery([artifact])

    assert result.decision.status == "BLOCKED"
    assert "unknown_application_stack:unknown.apk" in result.decision.blockers
    cache = ArtifactCache(tmp_path / "cache")
    object_dir = cache.store(result)
    assert object_dir.is_dir()
    assert "classification" not in cache.verify(result.artifact_digest)


def test_archive_without_apk_members_has_a_dedicated_blocker(tmp_path: Path) -> None:
    delivery = tmp_path / "empty.xapk"
    with zipfile.ZipFile(delivery, "w") as archive:
        archive.writestr("manifest.json", "{}")

    result = preflight_delivery([delivery])

    assert "delivery_contains_no_apk_members" in result.decision.blockers
    assert not any(
        blocker.startswith("unknown_application_stack:") for blocker in result.decision.blockers
    )


def test_delivery_rejects_non_regular_input_without_opening_it(tmp_path: Path) -> None:
    fifo = tmp_path / "delivery.apk"
    os.mkfifo(fifo)

    with pytest.raises(SafetyError, match="regular file"):
        preflight_delivery([fifo])


def test_delivery_rejects_symlink_input(tmp_path: Path) -> None:
    artifact = tmp_path / "real.apk"
    _native_apk(artifact)
    alias = tmp_path / "alias.apk"
    alias.symlink_to(artifact)

    with pytest.raises(SafetyError, match="regular file"):
        preflight_delivery([alias])


def test_delivery_rejects_unsafe_direct_apk_name(tmp_path: Path) -> None:
    artifact = tmp_path / "unsafe\\name.apk"
    _native_apk(artifact)

    with pytest.raises(SafetyError, match="unsafe archive member name"):
        preflight_delivery([artifact])


def test_one_unknown_member_blocks_an_otherwise_known_split_set(tmp_path: Path) -> None:
    known = tmp_path / "base.apk"
    unknown = tmp_path / "opaque.apk"
    _native_apk(known)
    _apk(unknown, "assets/unidentified.payload")

    result = preflight_delivery([known, unknown])

    assert result.decision.status == "BLOCKED"
    assert "unknown_application_stack:opaque.apk" in result.decision.blockers
    assert "split_coherence_not_verified" in result.decision.blockers


def test_cache_integrity_status_separation_and_copy_materialization(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")

    object_dir = cache.store(result)
    manifest = cache.verify(result.artifact_digest)
    object_manifest_before = (object_dir / "manifest.json").read_bytes()
    status_path = cache.write_status(
        result.artifact_digest,
        "READY",
        pipeline_revision="pipeline-v1",
        detail="queued",
    )
    cache.write_status(result.artifact_digest, "COMPLETE", pipeline_revision="pipeline-v1")

    assert status_path.parent == cache.root / "status" / "pipeline-v1"
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "COMPLETE"
    assert (object_dir / "manifest.json").read_bytes() == object_manifest_before
    assert "status" not in manifest

    materialized = cache.materialize(result.artifact_digest, tmp_path / "materialized")
    stored_name = manifest["members"][0]["stored_name"]
    cached_member = object_dir / "members" / stored_name
    copied_member = materialized / stored_name
    assert copied_member.read_bytes() == cached_member.read_bytes()
    assert (cached_member.stat().st_dev, cached_member.stat().st_ino) != (
        copied_member.stat().st_dev,
        copied_member.stat().st_ino,
    )
    assert (materialized / "MATERIALIZED.COMPLETE").is_file()


def test_cache_detects_member_corruption(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    object_dir = cache.store(result)
    manifest = cache.verify(result.artifact_digest)
    stored_name = manifest["members"][0]["stored_name"]
    (object_dir / "members" / stored_name).write_bytes(b"corrupt")

    with pytest.raises(CacheIntegrityError, match="size or type mismatch"):
        cache.verify(result.artifact_digest)


def test_cache_object_is_published_by_atomic_directory_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    original_rename = legacy_preflight._rename_noreplace
    publications: list[tuple[Path, Path]] = []

    def record_rename(source: Path, destination: Path) -> None:
        publications.append((source, destination))
        original_rename(source, destination)

    monkeypatch.setattr(legacy_preflight, "_rename_noreplace", record_rename)

    object_dir = cache.store(result)

    assert len(publications) == 1
    temporary, published = publications[0]
    assert published == object_dir
    assert temporary.parent == object_dir.parent
    assert temporary.name.startswith(f".{result.artifact_digest}.tmp-")
    assert (object_dir / "OBJECT.COMPLETE").is_file()


def test_preflight_derives_everything_from_one_sealed_source_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    original_open = legacy_preflight.os.open
    source_opens = 0

    def count_source_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal source_opens
        if os.fsdecode(path) == os.fspath(artifact):
            source_opens += 1
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(legacy_preflight.os, "open", count_source_open)

    result = preflight_delivery([artifact])

    assert source_opens == 1
    assert result.delivery_files[0].sha256 == result.artifact_members[0].sha256


def test_cache_object_is_independent_of_classification(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    altered = replace(
        result,
        decision=StackDecision(
            stacks=("future_stack",),
            routes=("future-route",),
            status="BLOCKED",
            blockers=("future_gate",),
        ),
    )
    cache = ArtifactCache(tmp_path / "cache")

    first = cache.store(result)
    manifest_before = (first / "manifest.json").read_bytes()
    second = cache.store(altered)

    assert second == first
    assert (first / "manifest.json").read_bytes() == manifest_before
    assert "classification" not in cache.verify(result.artifact_digest)
    assert first.parent.name == legacy_preflight.CACHE_SCHEMA


def test_status_requires_object_and_enforces_terminal_transitions(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")

    with pytest.raises(CacheIntegrityError, match="missing"):
        cache.write_status(result.artifact_digest, "READY", pipeline_revision="pipeline-v1")

    cache.store(result)
    with pytest.raises(PreflightError, match="transition"):
        cache.write_status(result.artifact_digest, "COMPLETE", pipeline_revision="pipeline-v1")
    cache.write_status(result.artifact_digest, "READY", pipeline_revision="pipeline-v1")
    cache.write_status(result.artifact_digest, "COMPLETE", pipeline_revision="pipeline-v1")
    with pytest.raises(PreflightError, match="transition"):
        cache.write_status(result.artifact_digest, "READY", pipeline_revision="pipeline-v1")
    with pytest.raises(PreflightError, match="transition"):
        cache.write_status(result.artifact_digest, "COMPLETE", pipeline_revision="pipeline-v1")

    second = cache.write_status(result.artifact_digest, "READY", pipeline_revision="pipeline-v2")
    assert second.parent.name == "pipeline-v2"

    cache.write_status(result.artifact_digest, "BLOCKED", pipeline_revision="pipeline-v3")
    cache.write_status(result.artifact_digest, "READY", pipeline_revision="pipeline-v3")
    cache.write_status(result.artifact_digest, "FAILED", pipeline_revision="pipeline-v3")
    with pytest.raises(PreflightError, match="transition"):
        cache.write_status(result.artifact_digest, "READY", pipeline_revision="pipeline-v3")


def test_preflight_rejects_delivery_compressed_size_before_read(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)

    with pytest.raises(SafetyError, match="compressed delivery size limit"):
        preflight_delivery(
            [artifact],
            limits=PreflightLimits(max_delivery_file_bytes=artifact.stat().st_size - 1),
        )


def test_preflight_applies_artifact_member_limit_to_direct_apk(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)

    with pytest.raises(SafetyError, match="artifact member size limit"):
        preflight_delivery(
            [artifact],
            limits=PreflightLimits(max_member_bytes=artifact.stat().st_size - 1),
        )


def test_preflight_rejects_archive_compressed_total_before_expansion(tmp_path: Path) -> None:
    inner = tmp_path / "base.apk"
    _native_apk(inner)
    delivery = tmp_path / "delivery.xapk"
    with zipfile.ZipFile(delivery, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("base.apk", inner.read_bytes())

    with pytest.raises(SafetyError, match="archive compressed-size limit"):
        preflight_delivery(
            [delivery],
            limits=PreflightLimits(max_archive_compressed_bytes=1),
        )


def test_zip_member_limit_is_checked_before_zipfile_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)

    def reject_constructor(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ZipFile constructor must not run")

    monkeypatch.setattr(legacy_preflight.zipfile, "ZipFile", reject_constructor)

    with pytest.raises(SafetyError, match="invalid APK member"):
        preflight_delivery([artifact], limits=PreflightLimits(max_archive_members=0))


def test_preflight_does_not_change_source_access_time(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    node = artifact.stat()
    old_atime = 1_600_000_000_000_000_000
    os.utime(artifact, ns=(old_atime, node.st_mtime_ns))

    result = preflight_delivery([artifact])

    assert result.decision.status == "BLOCKED"
    assert artifact.stat().st_atime_ns == old_atime


def test_cache_verify_rejects_symlink_member_without_following(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    object_dir = cache.store(result)
    manifest = cache.verify(result.artifact_digest)
    member = object_dir / "members" / manifest["members"][0]["stored_name"]
    external = tmp_path / "external.apk"
    external.write_bytes(member.read_bytes())
    member.unlink()
    member.symlink_to(external)

    with pytest.raises(CacheIntegrityError, match="missing or unsafe"):
        cache.verify(result.artifact_digest)


def test_cache_verify_rechecks_exact_membership_after_hashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    object_dir = cache.store(result)
    original_hash = legacy_preflight._hash_fd_exact
    injected = False

    def inject_member(descriptor: int, expected_size: int) -> tuple[str, int]:
        nonlocal injected
        if not injected:
            injected = True
            (object_dir / "members" / "unexpected.apk").write_bytes(b"unexpected")
        return original_hash(descriptor, expected_size)

    monkeypatch.setattr(legacy_preflight, "_hash_fd_exact", inject_member)

    with pytest.raises(CacheIntegrityError, match="changed while verifying"):
        cache.verify(result.artifact_digest)


def test_cache_verify_rechecks_membership_after_logical_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    object_dir = cache.store(result)
    original_digest = legacy_preflight._digest_manifest

    def inject_after_digest(domain: str, items: Sequence[Mapping[str, object]]) -> str:
        digest = original_digest(domain, items)
        if domain == "artifact":
            (object_dir / "members" / "late-extra.apk").write_bytes(b"late")
        return digest

    monkeypatch.setattr(legacy_preflight, "_digest_manifest", inject_after_digest)

    with pytest.raises(CacheIntegrityError, match="changed while verifying"):
        cache.verify(result.artifact_digest)


def test_deep_cache_manifest_is_rejected_without_recursion_crash(tmp_path: Path) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    object_dir = cache.store(result)
    hostile = b"[" * 100_000 + b"0" + b"]" * 100_000
    (object_dir / "manifest.json").write_bytes(hostile)
    digest = hashlib.sha256(hostile).hexdigest()
    (object_dir / "OBJECT.COMPLETE").write_text(f"{digest}  manifest.json\n", encoding="utf-8")

    with pytest.raises(CacheIntegrityError, match="manifest is invalid"):
        cache.verify(result.artifact_digest)


def test_materialization_is_built_privately_then_atomically_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "base.apk"
    _native_apk(artifact)
    result = preflight_delivery([artifact])
    cache = ArtifactCache(tmp_path / "cache")
    cache.store(result)
    destination = tmp_path / "materialized"
    original_publish = legacy_preflight._rename_noreplace
    observations: list[tuple[bool, bool]] = []

    def inspect_publish(source: Path, target: Path) -> None:
        observations.append(
            (
                target.exists(),
                (source / "MATERIALIZED.COMPLETE").is_file(),
            )
        )
        original_publish(source, target)

    monkeypatch.setattr(legacy_preflight, "_rename_noreplace", inspect_publish)

    cache.materialize(result.artifact_digest, destination)

    assert observations == [(False, True)]
    assert (destination / "MATERIALIZED.COMPLETE").is_file()
