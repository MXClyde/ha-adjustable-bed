"""Repair flows for the Adjustable Bed integration.

Currently provides a guided fix for the ``pairing_required`` issue: it walks the
user through putting the base into Bluetooth pairing mode, follows the
controller-specific connection/bond ordering, and verifies the bond by reading
an auth-gated characteristic before resolving the issue.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.repairs import RepairsFlow, repairs_flow_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry
from homeassistant.helpers.translation import async_get_translations

from .adapter import get_discovered_service_info
from .address_lock import async_get_connect_lock
from .ble_auth import is_ble_authentication_error
from .bluetooth_transport import TransportClass
from .bond_recovery import (
    RecoveryOffer,
    async_recover_local_bond,
    async_recovery_offer,
    recovery_context,
)
from .bond_verification import CONF_BLE_BOND_CONTEXT
from .const import (
    ADAPTER_AUTO,
    CONF_BED_TYPE,
    CONF_BLE_BOND_ESTABLISHED,
    CONF_BLE_BOND_MARKER_UNRELIABLE,
    CONF_PREFERRED_ADAPTER,
    CONF_PROTOCOL_VARIANT,
    DEVICE_INFO_CHARS,
    DOMAIN,
    grants_one_connection_per_pairing_window,
)
from .setup_operation import (
    BluetoothOperationMixin,
    OperationOutcome,
    OperationResult,
    SetupAction,
)

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)


class PairingRequiredRepairFlow(BluetoothOperationMixin, RepairsFlow):
    """Guided flow to (re-)pair a bed that requires Bluetooth bonding.

    Two branches. The ordinary one puts the bed back into pairing mode and bonds
    it. The other replaces a bond this host is still holding but the bed no
    longer honours, and it is offered only when the evidence that raised this
    repair actually points at the host (issue #459).
    """

    def __init__(
        self,
        address: str,
        name: str,
        entry_id: str | None,
        issue_data: dict[str, Any] | None = None,
    ) -> None:
        """Store the target bed details from the issue data."""
        self._address = address
        self._name = name
        self._entry_id = entry_id
        self._issue_data = dict(issue_data or {})
        self._offer: RecoveryOffer | None = None
        self._result_shown = False

    def _async_flow_manager(self) -> Any:
        """Repairs flows are driven by their own manager, not the config one."""
        return repairs_flow_manager(self.hass)

    def _entry(self) -> ConfigEntry | None:
        """Return the config entry this repair belongs to, if it still exists."""
        if self._entry_id is None:
            return None
        return self.hass.config_entries.async_get_entry(self._entry_id)

    def _bed_type(self) -> tuple[str | None, str | None]:
        """Return the bed type and protocol variant for this bed."""
        entry = self._entry()
        if entry is None:
            return None, None
        return entry.data.get(CONF_BED_TYPE), entry.data.get(CONF_PROTOCOL_VARIANT)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point — offer the branch that fits the evidence."""
        bed_type, variant = self._bed_type()
        self._offer = await async_recovery_offer(
            self.hass,
            address=self._address,
            issue_data=self._issue_data,
            bed_type=bed_type,
            protocol_variant=variant,
        )
        if self._offer.is_eligible:
            return await self.async_step_stale_bond_confirm()
        if self._issue_data.get("evidence_transport") == TransportClass.PROXY.value:
            # The bond lives on a proxy. Nothing here can clear it, and offering
            # a host-side action would only look like it had.
            return await self.async_step_proxy_bond()
        return await self.async_step_confirm()

    async def async_step_proxy_bond(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Explain a proxy-owned bond rather than pretending to fix it."""
        if user_input is not None:
            return self.async_abort(reason="proxy_bond_guidance")
        return self.async_show_form(
            step_id="proxy_bond",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._name,
                "address": self._address,
                "transport": self._issue_data.get("evidence_source") or "a Bluetooth proxy",
            },
        )

    async def async_step_stale_bond_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm replacing a host bond the bed no longer honours."""
        offer = self._offer
        if offer is None or not offer.is_eligible:
            return await self.async_step_confirm()

        if user_input is not None:
            self._result_shown = False
            self.async_begin_operation(
                name=self._name,
                address=self._address,
                action=SetupAction.LOCATING,
                placeholders={"name": self._name, "address": self._address},
            )
            return await self.async_step_stale_bond_progress()

        return self.async_show_form(
            step_id="stale_bond_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._name,
                "address": self._address,
                "transport": (
                    offer.record.adapter_address or offer.record.adapter_path
                ),
            },
        )

    async def _async_recovery_worker(self) -> OperationResult:
        """Remove the stale host bond and make a verified new one."""
        previous_offer = self._offer
        assert previous_offer is not None
        bed_type, variant = self._bed_type()
        entry = self._entry()
        issue_id = f"pairing_required_{self._address.replace(':', '_').lower()}"
        issue = async_get_issue_registry(self.hass).async_get_issue(DOMAIN, issue_id)
        if issue is None:
            return OperationResult(
                outcome=OperationOutcome.UNPAIR_FAILED,
                detail="pairing_issue_no_longer_exists",
            )
        current_offer = await async_recovery_offer(
            self.hass,
            address=self._address,
            issue_data=dict(issue.data or {}),
            bed_type=bed_type,
            protocol_variant=variant,
        )
        if (
            not current_offer.is_eligible
            or current_offer.record != previous_offer.record
            or current_offer.owner != previous_offer.owner
        ):
            return OperationResult(
                outcome=OperationOutcome.UNPAIR_FAILED,
                detail="pairing_evidence_changed",
            )
        self._offer = current_offer

        coordinator = (
            self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if entry is not None
            else None
        )
        return await async_recover_local_bond(
            self.hass,
            address=self._address,
            name=self._name,
            offer=current_offer,
            bed_type=bed_type,
            protocol_variant=variant,
            transport_operation=(
                coordinator.async_transport_operation
                if coordinator is not None
                else None
            ),
            on_verified=self._async_persist_recovered_bond,
            report_action=self.async_report_action,
            report_progress=self.async_report_progress,
            report_path=self.async_report_path,
            track_client=self.async_track_client,
        )

    async def async_step_stale_bond_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run the recovery behind a live progress view."""
        return await self.async_run_operation_step(
            step_id="stale_bond_progress",
            worker=self._async_recovery_worker,
            next_step_id="stale_bond_result",
        )

    async def async_step_stale_bond_result(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Resolve the repair only when the new bond was actually proven."""
        result = self.operation.result
        succeeded = result is not None and result.succeeded

        if user_input is not None and self._result_shown:
            if succeeded:
                return self.async_create_entry(title="", data={})
            return self.async_abort(reason="stale_bond_recovery_failed")

        self._result_shown = True
        return self.async_show_form(
            step_id="stale_bond_result",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._name,
                "outcome": await self._async_recovery_note(result, succeeded),
            },
        )

    async def _async_recovery_note(
        self, result: OperationResult | None, succeeded: bool
    ) -> str:
        """Return the localized description of what recovery achieved."""
        if succeeded:
            note_key = "recovery_success"
        elif result is None or result.outcome is OperationOutcome.CANCELLED:
            note_key = "recovery_not_run"
        elif result.outcome is OperationOutcome.NOT_ADVERTISING:
            note_key = "recovery_not_advertising"
        elif result.outcome is OperationOutcome.BOND_VERIFICATION_FAILED and (
            result.detail != "authentication_state_unconfirmed"
        ):
            note_key = "recovery_partial"
        elif result.outcome is OperationOutcome.UNPAIR_FAILED:
            note_key = "recovery_unpair_failed"
        else:
            note_key = "recovery_failed_unchanged"

        translations = await async_get_translations(
            self.hass,
            self.hass.config.language,
            "issues",
            integrations={DOMAIN},
        )
        return translations.get(
            f"component.{DOMAIN}.issues.pairing_required.fix_flow.abort.{note_key}",
            note_key,
        )

    async def _async_persist_recovered_bond(self, result: OperationResult | None) -> None:
        """Record the new bond and its owner, then reload the entry."""
        entry = self._entry()
        if entry is None or result is None or result.payload is None:
            return
        data = {
            **entry.data,
            CONF_BLE_BOND_ESTABLISHED: True,
            CONF_BLE_BOND_CONTEXT: recovery_context(result.payload),
        }
        data.pop(CONF_BLE_BOND_MARKER_UNRELIABLE, None)
        self.hass.config_entries.async_update_entry(entry, data=data)
        await self.hass.config_entries.async_reload(entry.entry_id)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pair with the bed when the user confirms."""
        if user_input is not None:
            if await self._async_try_pair():
                return self.async_create_entry(title="", data={})
            return self.async_abort(reason="pairing_failed")

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
        )

    def _find_device(self) -> BLEDevice | None:
        """Find the BLE device, honoring the entry's preferred adapter.

        BLE bonds live on the adapter/proxy that performed pairing, so a repair
        must pair on the same source the coordinator will use — otherwise it can
        bond one source, mark the entry bonded, and leave the configured source
        still unauthenticated.
        """
        preferred = ADAPTER_AUTO
        if self._entry_id is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                preferred = entry.data.get(CONF_PREFERRED_ADAPTER, ADAPTER_AUTO)

        if not preferred or preferred == ADAPTER_AUTO:
            return bluetooth.async_ble_device_from_address(
                self.hass, self._address, connectable=True
            )

        address_upper = self._address.upper()
        for service_info in get_discovered_service_info(
            self.hass, include_non_connectable=True
        ):
            if service_info.address.upper() != address_upper:
                continue
            if getattr(service_info, "source", None) == preferred:
                return service_info.device
        return None

    def _bonded_now(self) -> bool:
        """Return True when the entry currently records a confirmed BLE bond."""
        if self._entry_id is None:
            return False
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return bool(entry is not None and entry.data.get(CONF_BLE_BOND_ESTABLISHED))

    async def _async_pair_via_coordinator(self) -> bool | None:
        """Pair without ever opening a throwaway connection.

        Returns True/False for a bed that grants one connection per pairing
        window, or None when this repair may use its own client.

        For such a bed, opening a second client here would consume the single
        connection and then close it in ``finally``, so the reload afterwards
        would find the box refusing every reconnect. Two routes avoid that:

        * A loaded coordinator pairs on its own link via ``async_pair_now()``,
          which bonds an already-live link instead of reconnecting.
        * With no loaded coordinator the entry is typically in SETUP_RETRY (the
          very state that raises this repair), so reloading it lets
          ``async_setup_entry`` make exactly one connection that connects,
          discovers, bonds and stays up.

        Success is reported only when the bond is actually confirmed. Connecting
        is not the same as pairing: the connect path deliberately keeps an
        unbonded link, so treating a connection as success would resolve the
        pairing issue while the bed is still unbonded.
        """
        if self._entry_id is None:
            return None
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return None
        if not grants_one_connection_per_pairing_window(
            entry.data.get(CONF_BED_TYPE) or "",
            entry.data.get(CONF_PROTOCOL_VARIANT),
        ):
            return None

        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
        if coordinator is not None:
            _LOGGER.info(
                "Repair: pairing %s through the existing coordinator so the "
                "bed's single connection is kept rather than spent",
                self._address,
            )
            try:
                # async_pair_now() clears the runtime bond marker itself, which
                # editing entry.data alone would not do.
                return bool(await coordinator.async_pair_now())
            except Exception as err:  # noqa: BLE001 - any failure means "not paired"
                _LOGGER.warning("Repair: pairing failed for %s: %s", self._address, err)
                return False

        # No coordinator: the entry failed setup and is retrying. Clear the bond
        # marker so the next setup requests the bond, then let setup own the one
        # connection instead of racing it with a client of our own.
        if entry.data.get(CONF_BLE_BOND_ESTABLISHED):
            self.hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_BLE_BOND_ESTABLISHED: False},
            )
        _LOGGER.info(
            "Repair: reloading %s so its setup makes the single pairing "
            "connection (no coordinator is loaded to drive)",
            self._address,
        )
        try:
            await self.hass.config_entries.async_reload(self._entry_id)
        except Exception as err:  # noqa: BLE001 - any failure means "not paired"
            _LOGGER.warning("Repair: pairing failed for %s: %s", self._address, err)
            return False
        return self._bonded_now()

    async def _async_try_pair(self) -> bool:
        """Create a bond for beds that pair at connect time, and verify it.

        Beds that must bond after service discovery never reach this path: they
        are one-connection beds and are handled by _async_pair_via_coordinator,
        which refuses to open a throwaway client at all.
        """
        from bleak import BleakClient
        from bleak.exc import BleakError
        from bleak_retry_connector import establish_connection

        via_coordinator = await self._async_pair_via_coordinator()
        if via_coordinator is not None:
            return via_coordinator

        device = self._find_device()
        if device is None:
            _LOGGER.warning(
                "Repair: bed %s not reachable on the configured adapter — cannot pair",
                self._address,
            )
            return False

        client: BleakClient | None = None
        reload_entry_id: str | None = None
        # Hold the address lock for the whole client lifetime. Releasing it after
        # the connect would let the disconnect below land inside another caller's
        # connect attempt, where bleak's cleanup can abort it.
        async with async_get_connect_lock(self.hass, self._address):
            try:
                client = await establish_connection(
                    BleakClient,
                    device,
                    self._name,
                    max_attempts=1,
                    pair=True,
                    use_services_cache=False,
                )
            except Exception as err:  # noqa: BLE001 - any failure means "not paired"
                _LOGGER.warning("Repair: pairing failed for %s: %s", self._address, err)
                return False

            try:
                bonded = False
                try:
                    # Verify the bond by reading a known auth-gated characteristic. A
                    # still-unbonded link fails with GATT error=5; non-auth errors
                    # (e.g. the characteristic is absent) are inconclusive, not failures.
                    await client.read_gatt_char(DEVICE_INFO_CHARS["model_number"])
                    bonded = True
                except BleakError as err:
                    if is_ble_authentication_error(err):
                        _LOGGER.warning(
                            "Repair: bond verification failed for %s: %s",
                            self._address,
                            err,
                        )
                    else:
                        _LOGGER.debug(
                            "Repair: bond verification inconclusive for %s: %s",
                            self._address,
                            err,
                        )
                        bonded = True
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Repair: bond verification inconclusive for %s: %s",
                        self._address,
                        err,
                    )
                    bonded = True

                if not bonded:
                    return False

                # Persist the confirmed bond and reload so the coordinator reuses it
                # (and does not try to re-pair on top of the existing bond).
                if self._entry_id is not None:
                    entry = self.hass.config_entries.async_get_entry(self._entry_id)
                    if entry is not None:
                        if not entry.data.get(CONF_BLE_BOND_ESTABLISHED):
                            self.hass.config_entries.async_update_entry(
                                entry,
                                data={**entry.data, CONF_BLE_BOND_ESTABLISHED: True},
                            )
                        reload_entry_id = self._entry_id
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

        if reload_entry_id is not None:
            await self.hass.config_entries.async_reload(reload_entry_id)

        _LOGGER.info("Repair: pairing succeeded for %s", self._address)
        return True


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the repair flow for a fixable issue."""
    payload = data or {}
    return PairingRequiredRepairFlow(
        address=payload.get("address", ""),
        name=payload.get("name", "your bed"),
        entry_id=payload.get("entry_id"),
        issue_data=payload,
    )
