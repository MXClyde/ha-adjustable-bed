"""Tests for exact host-side bond inspection and removal (issues #455, #459)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.adjustable_bed.bluetooth_bond import (
    BluezReadStatus,
    BondRemovalStatus,
    LocalBondRecord,
    async_read_local_bonds,
    async_remove_host_bond,
    async_remove_local_bond,
)

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"

_BOND = "custom_components.adjustable_bed.bluetooth_bond"

_ADAPTER_0 = "/org/bluez/hci0"
_ADAPTER_1 = "/org/bluez/hci1"
_DEVICE_0 = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
_DEVICE_1 = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"


def _objects(
    *,
    devices: list[dict[str, Any]] | None = None,
    adapters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a BlueZ managed-objects tree."""
    tree: dict[str, Any] = {}
    for path, mac in (adapters or {_ADAPTER_0: "11:22:33:44:55:66"}).items():
        tree[path] = {"org.bluez.Adapter1": {"Address": mac}}
    for device in devices or []:
        path = device.pop("path")
        tree[path] = {"org.bluez.Device1": device}
    return tree


def _device(
    *,
    path: str = _DEVICE_0,
    adapter: str = _ADAPTER_0,
    address: str = TEST_ADDRESS,
    paired: bool = True,
    bonded: bool = True,
    connected: bool = False,
) -> dict[str, Any]:
    return {
        "path": path,
        "Address": address,
        "Adapter": adapter,
        "Paired": paired,
        "Bonded": bonded,
        "Trusted": False,
        "Connected": connected,
    }


def _patch_objects(tree: dict[str, Any] | None):
    """Patch the raw BlueZ read. ``None`` means the read failed."""
    return patch(f"{_BOND}._async_managed_objects", AsyncMock(return_value=tree))


class TestReadingBonds:
    """A failed read must never be mistaken for "there is no bond"."""

    async def test_a_bonded_device_is_reported(self) -> None:
        with _patch_objects(_objects(devices=[_device()])):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert inventory.readable
        assert len(inventory.records) == 1
        record = inventory.records[0]
        assert record.device_path == _DEVICE_0
        assert record.adapter_path == _ADAPTER_0
        assert record.adapter_address == "11:22:33:44:55:66"
        assert record.has_bond

    async def test_an_unreadable_bus_is_not_an_empty_result(self) -> None:
        """The whole reason this module does not use the permissive helper."""
        with _patch_objects(None):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert inventory.status is BluezReadStatus.UNAVAILABLE
        assert not inventory.readable
        assert inventory.records == ()

    async def test_a_known_but_unbonded_device_is_not_a_bond(self) -> None:
        with _patch_objects(_objects(devices=[_device(paired=False, bonded=False)])):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert inventory.readable
        assert inventory.records
        assert inventory.bonded_records == ()

    async def test_paired_alone_still_counts_as_a_bond(self) -> None:
        with _patch_objects(_objects(devices=[_device(paired=True, bonded=False)])):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert inventory.bonded_records

    async def test_other_addresses_are_ignored(self) -> None:
        with _patch_objects(
            _objects(devices=[_device(address="11:11:11:11:11:11", path="/org/bluez/hci0/dev_x")])
        ):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert inventory.records == ()

    async def test_the_address_property_is_matched_not_the_path(self) -> None:
        """A path built from an address would break on any format mismatch."""
        device = _device(path="/org/bluez/hci0/dev_something_else")
        with _patch_objects(_objects(devices=[device])):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert len(inventory.records) == 1
        assert inventory.records[0].device_path == "/org/bluez/hci0/dev_something_else"

    async def test_two_adapters_bonded_to_the_same_bed_are_both_reported(self) -> None:
        tree = _objects(
            devices=[_device(), _device(path=_DEVICE_1, adapter=_ADAPTER_1)],
            adapters={_ADAPTER_0: "11:22:33:44:55:66", _ADAPTER_1: "AA:AA:AA:AA:AA:AA"},
        )
        with _patch_objects(tree):
            inventory = await async_read_local_bonds(TEST_ADDRESS)
        assert len(inventory.bonded_records) == 2
        assert inventory.sole_bond is None


class TestRemovingOneBond:
    """A removal is only a success once it has been confirmed."""

    def _record(self) -> LocalBondRecord:
        return LocalBondRecord(
            address=TEST_ADDRESS,
            device_path=_DEVICE_0,
            adapter_path=_ADAPTER_0,
            adapter_address="11:22:33:44:55:66",
            paired=True,
            bonded=True,
        )

    def _patch_bus(self, *, error_name: str | None = None, raises: Exception | None = None):
        """Patch the D-Bus call used for RemoveDevice."""
        from dbus_fast import MessageType

        class _Bus:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def connect(self) -> None:
                return None

            async def call(self, _message: Any) -> Any:
                if raises is not None:
                    raise raises
                return type(
                    "_Reply",
                    (),
                    {
                        "message_type": (
                            MessageType.ERROR if error_name else MessageType.METHOD_RETURN
                        ),
                        "error_name": error_name,
                        "body": [],
                    },
                )()

            def disconnect(self) -> None:
                return None

        return patch("dbus_fast.aio.MessageBus", _Bus)

    async def test_a_confirmed_removal_succeeds(self) -> None:
        with (
            self._patch_bus(),
            patch(
                f"{_BOND}._async_managed_objects",
                AsyncMock(return_value=_objects(devices=[])),
            ),
        ):
            result = await async_remove_local_bond(self._record())
        assert result.status is BondRemovalStatus.REMOVED
        assert result.succeeded

    async def test_a_still_paired_device_is_a_failure(self) -> None:
        """BlueZ accepting the call is not evidence that the bond went away."""
        with (
            self._patch_bus(),
            patch(
                f"{_BOND}._async_managed_objects",
                AsyncMock(return_value=_objects(devices=[_device()])),
            ),
        ):
            result = await async_remove_local_bond(self._record())
        assert result.status is BondRemovalStatus.VERIFICATION_FAILED
        assert not result.succeeded

    async def test_a_device_left_behind_without_a_bond_is_a_success(self) -> None:
        with (
            self._patch_bus(),
            patch(
                f"{_BOND}._async_managed_objects",
                AsyncMock(return_value=_objects(devices=[_device(paired=False, bonded=False)])),
            ),
        ):
            result = await async_remove_local_bond(self._record())
        assert result.status is BondRemovalStatus.REMOVED

    async def test_an_unreadable_bus_after_removal_cannot_confirm(self) -> None:
        """This is the false-success the permissive D-Bus helper would produce."""
        with (
            self._patch_bus(),
            patch(f"{_BOND}._async_managed_objects", AsyncMock(return_value=None)),
        ):
            result = await async_remove_local_bond(self._record())
        assert result.status is BondRemovalStatus.VERIFICATION_FAILED
        assert result.error == "bluez_unreadable_after_removal"

    async def test_a_refused_call_is_a_failure(self) -> None:
        with self._patch_bus(error_name="org.bluez.Error.DoesNotExist"):
            result = await async_remove_local_bond(self._record())
        assert result.status is BondRemovalStatus.RPC_FAILED
        assert result.error == "org.bluez.Error.DoesNotExist"

    async def test_a_raising_bus_is_a_failure(self) -> None:
        with self._patch_bus(raises=RuntimeError("bus is gone")):
            result = await async_remove_local_bond(self._record())
        assert result.status is BondRemovalStatus.RPC_FAILED
        assert not result.succeeded


class TestChoosingWhichBondToRemove:
    """Never guess which adapter's bond the user meant."""

    async def test_an_unreadable_backend_refuses(self) -> None:
        with _patch_objects(None):
            result = await async_remove_host_bond(TEST_ADDRESS)
        assert result.status is BondRemovalStatus.BACKEND_UNAVAILABLE

    async def test_no_bond_is_reported_as_already_absent(self) -> None:
        with _patch_objects(_objects(devices=[])):
            result = await async_remove_host_bond(TEST_ADDRESS)
        assert result.status is BondRemovalStatus.ALREADY_ABSENT
        assert not result.succeeded

    async def test_two_bonded_adapters_without_a_choice_refuse(self) -> None:
        tree = _objects(
            devices=[_device(), _device(path=_DEVICE_1, adapter=_ADAPTER_1)],
            adapters={_ADAPTER_0: "11:22:33:44:55:66", _ADAPTER_1: "AA:AA:AA:AA:AA:AA"},
        )
        with (
            _patch_objects(tree),
            patch(f"{_BOND}.async_remove_local_bond") as removal,
        ):
            result = await async_remove_host_bond(TEST_ADDRESS)
        assert result.status is BondRemovalStatus.AMBIGUOUS_OWNER
        removal.assert_not_called()

    async def test_naming_the_adapter_resolves_the_ambiguity(self) -> None:
        tree = _objects(
            devices=[_device(), _device(path=_DEVICE_1, adapter=_ADAPTER_1)],
            adapters={_ADAPTER_0: "11:22:33:44:55:66", _ADAPTER_1: "AA:AA:AA:AA:AA:AA"},
        )
        with (
            _patch_objects(tree),
            patch(f"{_BOND}.async_remove_local_bond", AsyncMock()) as removal,
        ):
            await async_remove_host_bond(TEST_ADDRESS, adapter_address="AA:AA:AA:AA:AA:AA")
        removal.assert_awaited_once()
        assert removal.await_args[0][0].adapter_path == _ADAPTER_1

    async def test_a_named_adapter_with_no_bond_is_already_absent(self) -> None:
        with (
            _patch_objects(_objects(devices=[_device()])),
            patch(f"{_BOND}.async_remove_local_bond") as removal,
        ):
            result = await async_remove_host_bond(
                TEST_ADDRESS, adapter_address="99:99:99:99:99:99"
            )
        assert result.status is BondRemovalStatus.ALREADY_ABSENT
        removal.assert_not_called()

    async def test_a_single_bond_needs_no_adapter_named(self) -> None:
        with (
            _patch_objects(_objects(devices=[_device()])),
            patch(f"{_BOND}.async_remove_local_bond", AsyncMock()) as removal,
        ):
            await async_remove_host_bond(TEST_ADDRESS)
        removal.assert_awaited_once()


@pytest.mark.parametrize(
    ("paired", "bonded", "expected"),
    [(True, True, True), (True, False, True), (False, True, True), (False, False, False)],
)
def test_bond_presence(paired: bool, bonded: bool, expected: bool) -> None:
    record = LocalBondRecord(
        address=TEST_ADDRESS,
        device_path=_DEVICE_0,
        adapter_path=_ADAPTER_0,
        paired=paired,
        bonded=bonded,
    )
    assert record.has_bond is expected
