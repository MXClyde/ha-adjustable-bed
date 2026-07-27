"""Tests for the Adjustable Bed repair flows."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed.bluetooth_bond import LocalBondRecord
from custom_components.adjustable_bed.bluetooth_transport import (
    ConnectionPath,
    TransportClass,
)
from custom_components.adjustable_bed.bond_verification import (
    CONF_BLE_BOND_CONTEXT,
    BondEvidence,
    BondOwner,
    BondVerificationStatus,
)
from custom_components.adjustable_bed.const import (
    BED_TYPE_LEGGETT_GEN2,
    CONF_BED_TYPE,
    CONF_BLE_BOND_ESTABLISHED,
    DOMAIN,
)
from custom_components.adjustable_bed.repairs import (
    PairingRequiredRepairFlow,
    async_create_fix_flow,
)
from custom_components.adjustable_bed.setup_operation import (
    OperationOutcome,
    OperationResult,
)

from .conftest import TEST_ADDRESS, TEST_NAME

BLEAK_DEVICE = "custom_components.adjustable_bed.repairs.bluetooth.async_ble_device_from_address"
ESTABLISH = "bleak_retry_connector.establish_connection"


async def test_async_create_fix_flow_builds_pairing_flow(hass: HomeAssistant) -> None:
    """The factory wires issue data into the pairing repair flow."""
    flow = await async_create_fix_flow(
        hass,
        f"pairing_required_{TEST_ADDRESS.replace(':', '_').lower()}",
        {
            "address": TEST_ADDRESS,
            "name": TEST_NAME,
            "entry_id": "abc123",
            "evidence_status": "auth_failed",
            "evidence_transport": "proxy",
            "evidence_source": "bedroom-proxy",
            "evidence_adapter": None,
            "evidence_observed_at": "2026-07-27T00:00:00+00:00",
        },
    )
    assert isinstance(flow, PairingRequiredRepairFlow)
    assert flow._address == TEST_ADDRESS
    assert flow._name == TEST_NAME
    assert flow._entry_id == "abc123"
    assert flow._evidence is not None
    assert flow._evidence.status is BondVerificationStatus.AUTH_FAILED
    assert flow._evidence.owner.transport is TransportClass.PROXY
    assert flow._evidence.owner.source == "bedroom-proxy"


async def test_confirm_step_shows_form_first(hass: HomeAssistant) -> None:
    """The first step presents the pairing instructions form."""
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, None)
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["address"] == TEST_ADDRESS


async def test_one_link_proxy_failure_keeps_the_guided_pairing_flow(
    hass: HomeAssistant,
) -> None:
    """A live coordinator can safely pair the one link already held via a proxy."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: BED_TYPE_LEGGETT_GEN2,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_gen2_proxy_entry",
    )
    entry.add_to_hass(hass)
    flow = PairingRequiredRepairFlow(
        TEST_ADDRESS,
        TEST_NAME,
        entry.entry_id,
        issue_data={"evidence_transport": "proxy", "evidence_source": "proxy-source"},
    )
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"


async def test_confirm_step_resolves_on_successful_pair(hass: HomeAssistant) -> None:
    """Submitting the form resolves the issue when pairing succeeds."""
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, None)
    flow.hass = hass

    with patch.object(flow, "_async_try_pair", new=AsyncMock(return_value=True)):
        result = await flow.async_step_confirm({})

    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_confirm_step_aborts_on_failed_pair(hass: HomeAssistant) -> None:
    """Submitting the form aborts (issue stays) when pairing fails."""
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, None)
    flow.hass = hass

    with patch.object(flow, "_async_try_pair", new=AsyncMock(return_value=False)):
        result = await flow.async_step_confirm({})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "pairing_failed"


async def test_stale_recovery_stops_when_the_issue_was_cleared(
    hass: HomeAssistant,
) -> None:
    """An open confirmation cannot act after its evidence-bearing issue is gone."""
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, None)
    flow.hass = hass
    flow._offer = MagicMock()

    with patch(
        "custom_components.adjustable_bed.repairs.async_recover_local_bond"
    ) as recover:
        result = await flow._async_recovery_worker()

    assert result.outcome is OperationOutcome.UNPAIR_FAILED
    assert result.detail == "pairing_issue_no_longer_exists"
    recover.assert_not_called()


def _verified_local_bond() -> BondEvidence:
    return BondEvidence(
        status=BondVerificationStatus.VERIFIED,
        owner=BondOwner(
            transport=TransportClass.LOCAL,
            source="11:22:33:44:55:66",
            adapter="hci0",
        ),
        operation="stale_bond_recovery",
        observed_at="2026-07-27T00:00:00+00:00",
    )


async def test_recovery_persistence_relies_on_the_loaded_entry_listener(
    hass: HomeAssistant,
) -> None:
    """A loaded entry's update listener must be the only reload source."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: "okimat",
        },
        unique_id=TEST_ADDRESS,
        entry_id="loaded_recovery_entry",
    )
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.consume_internal_entry_update.return_value = False
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    async def reload_listener(
        _hass: HomeAssistant, updated_entry: MockConfigEntry
    ) -> None:
        await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.add_update_listener(reload_listener)
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass
    result = OperationResult(
        outcome=OperationOutcome.SUCCESS,
        payload=_verified_local_bond(),
    )

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        await flow._async_persist_recovered_bond(result)
        await hass.async_block_till_done()

    mock_reload.assert_awaited_once_with(entry.entry_id)


async def test_recovery_persistence_reloads_an_unloaded_entry(
    hass: HomeAssistant,
) -> None:
    """A setup-retry entry has no update listener, so persistence reloads it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: "okimat",
        },
        unique_id=TEST_ADDRESS,
        entry_id="unloaded_recovery_entry",
    )
    entry.add_to_hass(hass)
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass
    result = OperationResult(
        outcome=OperationOutcome.SUCCESS,
        payload=_verified_local_bond(),
    )

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        await flow._async_persist_recovered_bond(result)

    mock_reload.assert_awaited_once_with(entry.entry_id)


def test_pairing_repair_translations_cover_every_progress_and_result() -> None:
    """The Repairs namespace must localize phases and terminal guidance."""
    root = Path(__file__).parents[1] / "custom_components/adjustable_bed"
    required_progress = {
        "locating",
        "connecting",
        "pairing",
        "verifying_bond",
        "disconnecting",
        "unpairing",
    }
    required_results = {
        "recovery_success",
        "recovery_not_run",
        "recovery_not_advertising",
        "recovery_unpair_failed",
        "recovery_failed_unchanged",
        "recovery_partial",
    }
    for relative in ("strings.json", "translations/en.json", "translations/nb.json"):
        data = json.loads((root / relative).read_text())
        pairing = data["issues"]["pairing_required"]
        flow = pairing["fix_flow"]
        assert required_progress <= flow["progress"].keys()
        assert required_results <= flow["abort"].keys()
        assert "title" in pairing
        assert "confirm" in flow["step"]
        assert "pairing_failed" in flow["abort"]


async def test_try_pair_returns_false_when_device_not_in_range(hass: HomeAssistant) -> None:
    """No reachable device means pairing cannot proceed."""
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, None)
    flow.hass = hass

    with patch(BLEAK_DEVICE, return_value=None):
        assert await flow._async_try_pair() is False


async def test_try_pair_succeeds_and_clears_marker(hass: HomeAssistant) -> None:
    """A successful pair + verified read persists the bond and reloads the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: "okimat",
            CONF_BLE_BOND_ESTABLISHED: False,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_ok_entry",
    )
    entry.add_to_hass(hass)

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    client = MagicMock()
    client._connected_scanner = MagicMock(source="bedroom-proxy")
    client.pair = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=b"Model X")
    client.disconnect = AsyncMock()

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock(return_value=client)) as mock_establish,
        patch(
            "custom_components.adjustable_bed.repairs.async_path_for_source",
            return_value=ConnectionPath(
                source="bedroom-proxy", transport=TransportClass.PROXY
            ),
        ),
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock()
        ) as mock_reload,
    ):
        result = await flow._async_try_pair()

    assert result is True
    assert entry.data[CONF_BLE_BOND_ESTABLISHED] is True
    assert entry.data[CONF_BLE_BOND_CONTEXT]["transport"] == "proxy"
    assert entry.data[CONF_BLE_BOND_CONTEXT]["source"] == "bedroom-proxy"
    assert mock_establish.await_args.kwargs["pair"] is True
    client.pair.assert_not_awaited()
    mock_reload.assert_awaited_once_with(entry.entry_id)
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("bonded", [True, False])
async def test_leggett_gen2_repair_pairs_through_the_coordinator(
    hass: HomeAssistant, bonded: bool
) -> None:
    """The repair must reuse the coordinator's connection, not spend a new one.

    LP Comfort Connect grants roughly one connection per pairing window (#385),
    so opening a second client here and closing it in ``finally`` would leave
    the reload with a box that refuses every reconnect.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: BED_TYPE_LEGGETT_GEN2,
            CONF_BLE_BOND_ESTABLISHED: True,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_gen2_coordinator_entry",
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    # async_pair_now() reports whether the bond is confirmed, not merely whether
    # a connection exists: the connect path deliberately keeps unbonded links.
    coordinator.async_pair_now = AsyncMock(return_value=bonded)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock()) as mock_establish,
    ):
        assert await flow._async_try_pair() is bonded

    coordinator.async_pair_now.assert_awaited_once_with()
    mock_establish.assert_not_awaited()


async def test_leggett_gen2_repair_with_no_coordinator_reloads_the_entry(
    hass: HomeAssistant,
) -> None:
    """With no loaded coordinator the repair must reload, not open a client.

    The pairing repair is raised from SETUP_RETRY, where setup has not stored a
    coordinator yet. A standalone client would pair and then disconnect in its
    finally block, and the reload could not obtain a second connection (#385).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: BED_TYPE_LEGGETT_GEN2,
            CONF_BLE_BOND_ESTABLISHED: True,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_leggett_gen2_entry",
    )
    entry.add_to_hass(hass)

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    async def reload(entry_id: str) -> None:
        # Setup owns the single connection; simulate it confirming the bond.
        target = hass.config_entries.async_get_entry(entry_id)
        hass.config_entries.async_update_entry(
            target, data={**target.data, CONF_BLE_BOND_ESTABLISHED: True}
        )

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock()) as mock_establish,
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(side_effect=reload)
        ) as mock_reload,
    ):
        assert await flow._async_try_pair() is True

    mock_establish.assert_not_awaited()
    mock_reload.assert_awaited_once_with(entry.entry_id)
    # The stale marker is cleared first so setup actually requests the bond.
    assert flow._bonded_now() is True


async def test_leggett_gen2_repair_reload_that_stays_unbonded_fails(
    hass: HomeAssistant,
) -> None:
    """Connecting is not pairing: an unbonded reload must not resolve the issue."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: BED_TYPE_LEGGETT_GEN2,
            CONF_BLE_BOND_ESTABLISHED: False,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_leggett_gen2_unbonded_entry",
    )
    entry.add_to_hass(hass)

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock()) as mock_establish,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()),
    ):
        # The advisory connect path keeps an unbonded link, so the entry can load
        # without a bond. That must still report the repair as unsuccessful.
        assert await flow._async_try_pair() is False

    mock_establish.assert_not_awaited()
    assert entry.data[CONF_BLE_BOND_ESTABLISHED] is False


async def test_repair_releases_its_client_when_verification_is_cancelled(
    hass: HomeAssistant,
) -> None:
    """Cancelling bond verification must still release the standalone client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: "okimat",
            CONF_BLE_BOND_ESTABLISHED: False,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_cancelled_entry",
    )
    entry.add_to_hass(hass)

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    client = MagicMock()
    client.read_gatt_char = AsyncMock(side_effect=asyncio.CancelledError())
    client.disconnect = AsyncMock()

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock(return_value=client)),
        pytest.raises(asyncio.CancelledError),
    ):
        await flow._async_try_pair()

    client.disconnect.assert_awaited_once_with()
    assert entry.data[CONF_BLE_BOND_ESTABLISHED] is False


async def test_repair_releases_its_client_when_verification_is_unauthenticated(
    hass: HomeAssistant,
) -> None:
    """A still-unbonded link must fail the repair and release the client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: "okimat",
            CONF_BLE_BOND_ESTABLISHED: False,
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_unauth_entry",
    )
    entry.add_to_hass(hass)

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    client = MagicMock()
    client.read_gatt_char = AsyncMock(
        side_effect=BleakError("Insufficient authentication")
    )
    client.disconnect = AsyncMock()

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock(return_value=client)),
    ):
        assert await flow._async_try_pair() is False

    client.disconnect.assert_awaited_once_with()
    assert entry.data[CONF_BLE_BOND_ESTABLISHED] is False



async def test_try_pair_treats_non_auth_read_error_as_success(hass: HomeAssistant) -> None:
    """A non-auth read failure (e.g. char absent) is inconclusive, not a failure.

    It must still persist the bond marker and reload the entry so the repair
    closes for good rather than re-triggering on the next connection.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_NAME,
        data={
            CONF_ADDRESS: TEST_ADDRESS,
            CONF_NAME: TEST_NAME,
            CONF_BED_TYPE: "okimat",
            CONF_BLE_BOND_ESTABLISHED: False,
            CONF_BLE_BOND_CONTEXT: {
                "version": 1,
                "transport": "local",
                "source": "11:22:33:44:55:66",
                "adapter": "hci0",
            },
        },
        unique_id=TEST_ADDRESS,
        entry_id="repair_inconclusive_entry",
    )
    entry.add_to_hass(hass)

    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, entry.entry_id)
    flow.hass = hass

    client = MagicMock()
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Characteristic not found"))
    client.disconnect = AsyncMock()

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock(return_value=client)),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as mock_reload,
    ):
        assert await flow._async_try_pair() is True

    assert entry.data[CONF_BLE_BOND_ESTABLISHED] is True
    assert CONF_BLE_BOND_CONTEXT not in entry.data
    mock_reload.assert_awaited_once_with(entry.entry_id)
    client.disconnect.assert_awaited_once()


async def test_try_pair_returns_false_on_auth_error(hass: HomeAssistant) -> None:
    """Pairing that connects but fails the encrypted read is treated as not paired."""
    flow = PairingRequiredRepairFlow(TEST_ADDRESS, TEST_NAME, None)
    flow.hass = hass

    client = MagicMock()
    client.read_gatt_char = AsyncMock(
        side_effect=BleakError("handle=24 error=5 description=Insufficient authentication")
    )
    client.disconnect = AsyncMock()

    with (
        patch(BLEAK_DEVICE, return_value=MagicMock()),
        patch(ESTABLISH, new=AsyncMock(return_value=client)),
    ):
        assert await flow._async_try_pair() is False

    client.disconnect.assert_awaited_once()


async def test_a_reconnect_between_confirm_and_removal_does_not_block_recovery(
    hass: HomeAssistant,
) -> None:
    """Volatile fields change while the dialog is open; identity does not.

    Comparing whole records made a bed that merely connected look like a
    different bond, and refused a removal the user had already approved for
    exactly this one.
    """
    pinned = LocalBondRecord(
        address=TEST_ADDRESS,
        device_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        adapter_path="/org/bluez/hci0",
        adapter_address="11:22:33:44:55:66",
        paired=True,
        bonded=True,
        connected=False,
        trusted=False,
    )
    reconnected = replace(pinned, connected=True, trusted=True)

    assert pinned.is_same_bond_as(reconnected)
    # A bond that is actually gone is still a different answer.
    assert not pinned.is_same_bond_as(replace(pinned, paired=False, bonded=False))
    # So is one on another adapter.
    assert not pinned.is_same_bond_as(
        replace(
            pinned,
            adapter_path="/org/bluez/hci1",
            device_path="/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF",
        )
    )
