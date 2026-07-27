"""Repair issues and fix flows for the Adjustable Bed integration.

Surfaces the Dual Bed combine suggestion and a guided fix for the
``pairing_required`` issue: the latter walks the user through putting the base
into Bluetooth pairing mode, follows the controller-specific connection/bond
ordering, and verifies the bond by reading an auth-gated characteristic before
resolving the issue.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .adapter import get_discovered_service_info
from .address_lock import async_get_connect_lock
from .ble_auth import is_ble_authentication_error
from .bluetooth_transport import (
    TransportClass,
    async_path_for_source,
    client_source,
)
from .bond_verification import (
    CONF_BLE_BOND_CONTEXT,
    BondEvidence,
    BondOwner,
    BondVerificationStatus,
    bond_context_matches,
    build_bond_context,
)
from .const import (
    ADAPTER_AUTO,
    CONF_BED_TYPE,
    CONF_BLE_BOND_ESTABLISHED,
    CONF_PREFERRED_ADAPTER,
    CONF_PROTOCOL_VARIANT,
    DEVICE_INFO_CHARS,
    DOMAIN,
    grants_one_connection_per_pairing_window,
)
from .pairing_candidates import (
    active_pairing_candidates,
    build_pair_selection_schema,
)

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

COMBINE_BEDS_ISSUE_ID = "combine_two_beds"


@callback
def async_refresh_combine_beds_issue(hass: HomeAssistant) -> None:
    """Create or clear the Dual Bed suggestion from current entry state."""
    candidates = active_pairing_candidates(hass)
    if len(candidates) < 2:
        async_delete_issue(hass, DOMAIN, COMBINE_BEDS_ISSUE_ID)
        return

    async_create_issue(
        hass,
        DOMAIN,
        COMBINE_BEDS_ISSUE_ID,
        is_fixable=True,
        is_persistent=True,
        severity=IssueSeverity.WARNING,
        translation_key="combine_two_beds",
        data={"entry_count": len(candidates)},
    )


@callback
def async_setup_combine_beds_issue(hass: HomeAssistant) -> None:
    """Reconcile the suggestion once startup entry loading has settled.

    A persistent issue retains the user's dismissed state across restarts. Do
    not delete it while config entries are only temporarily not loaded during
    startup, because recreating it would make a dismissed suggestion nag again.
    """
    if hass.state is CoreState.running:
        async_refresh_combine_beds_issue(hass)
        return

    @callback
    def refresh_after_start(_: Event) -> None:
        async_refresh_combine_beds_issue(hass)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, refresh_after_start)


@callback
def async_track_combine_beds_issue(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Refresh the suggestion whenever this entry changes lifecycle state."""

    @callback
    def refresh() -> None:
        if hass.state is CoreState.running:
            async_refresh_combine_beds_issue(hass)

    entry.async_on_unload(entry.async_on_state_change(refresh))
    refresh()


class CombineBedsRepairFlow(RepairsFlow):
    """Route a Repairs suggestion through the canonical pairing config flow."""

    def __init__(self) -> None:
        """Track the delegated config flow across validation retries."""
        self._pairing_flow_id: str | None = None

    def _description_placeholders(self) -> dict[str, str]:
        """Describe the currently active candidates without exposing addresses."""
        candidates = active_pairing_candidates(self.hass)
        return {
            "count": str(len(candidates)),
            "names": ", ".join(entry.title for entry in candidates),
        }

    def _schema(self) -> vol.Schema:
        """Build ordered side assignments without any same-bed choices."""
        return build_pair_selection_schema(
            active_pairing_candidates(self.hass)
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Open the pairing selection directly from Repairs."""
        # RepairsFlowManager passes its internal {"issue_id": ...} payload to
        # the init step. It is flow metadata, not a submitted side assignment.
        return await self.async_step_pair_beds()

    async def async_step_pair_beds(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select sides and delegate validation/creation to the config flow."""
        if len(active_pairing_candidates(self.hass)) < 2:
            self._pairing_flow_id = None
            async_refresh_combine_beds_issue(self.hass)
            return self.async_abort(reason="not_enough_beds")

        if user_input is None:
            return self.async_show_form(
                step_id="pair_beds",
                data_schema=self._schema(),
                description_placeholders=self._description_placeholders(),
            )

        if self._pairing_flow_id is None:
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_USER},
                data={CONF_ADDRESS: "pair_beds"},
            )
            if (
                result.get("type") is not FlowResultType.FORM
                or result.get("step_id") != "pair_beds"
            ):
                return self.async_abort(
                    reason=result.get("reason") or "pairing_flow_failed"
                )
            self._pairing_flow_id = result["flow_id"]

        result = await self.hass.config_entries.flow.async_configure(
            self._pairing_flow_id, user_input
        )
        if result.get("type") is FlowResultType.CREATE_ENTRY:
            self._pairing_flow_id = None
            return self.async_create_entry(title="", data={})
        if result.get("type") is FlowResultType.FORM:
            self._pairing_flow_id = (
                result.get("flow_id") or self._pairing_flow_id
            )
            return self.async_show_form(
                step_id="pair_beds",
                data_schema=result.get("data_schema") or self._schema(),
                errors=result.get("errors"),
                description_placeholders=self._description_placeholders(),
            )
        self._pairing_flow_id = None
        return self.async_abort(reason=result.get("reason") or "pairing_flow_failed")


class PairingRequiredRepairFlow(RepairsFlow):
    """Guided flow to (re-)pair a bed that requires Bluetooth bonding."""

    def __init__(
        self,
        address: str,
        name: str,
        entry_id: str | None,
        evidence: BondEvidence | None = None,
    ) -> None:
        """Store the target bed details from the issue data."""
        self._address = address
        self._name = name
        self._entry_id = entry_id
        self._evidence = evidence

    def _paired_entry_data(self, verified_owner: BondOwner | None) -> dict[str, Any] | None:
        """Return entry data for a repaired bond without stale ownership."""
        if self._entry_id is None:
            return None
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return None

        data = {**entry.data, CONF_BLE_BOND_ESTABLISHED: True}
        if verified_owner is not None and verified_owner.transport is not TransportClass.UNKNOWN:
            prior_status = (
                str(self._evidence.status)
                if self._evidence is not None
                else "unknown"
            )
            context = build_bond_context(
                BondEvidence(
                    status=BondVerificationStatus.VERIFIED,
                    owner=verified_owner,
                    operation=f"repair_authenticated_read_after_{prior_status}",
                    observed_at=datetime.now(UTC).isoformat(),
                )
            )
            stored = entry.data.get(CONF_BLE_BOND_CONTEXT)
            # A coordinator that proved the bond has already recorded the same
            # owner. Restating it with a fresh timestamp is still a real change
            # to the entry, and this write is not tagged as an internal
            # bond-marker update, so the reload it triggers would drop the link
            # a one-connection-per-pairing-window bed will not grant again.
            data[CONF_BLE_BOND_CONTEXT] = (
                stored if bond_context_matches(stored, context) else context
            )
        else:
            # Pairing may have succeeded while the auth probe was inconclusive.
            # The old context describes the bond that was just replaced and can
            # no longer authorize host-side removal.
            data.pop(CONF_BLE_BOND_CONTEXT, None)
        return data

    def _persist_repaired_bond(self, verified_owner: BondOwner | None) -> None:
        """Persist a successful repair and its freshly verified owner."""
        if self._entry_id is None:
            return
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        data = self._paired_entry_data(verified_owner)
        if entry is not None and data is not None and data != dict(entry.data):
            self.hass.config_entries.async_update_entry(entry, data=data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point — show pairing instructions and a confirm button."""
        return await self.async_step_confirm()

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
                paired = bool(await coordinator.async_pair_now())
            except Exception as err:  # noqa: BLE001 - any failure means "not paired"
                _LOGGER.warning("Repair: pairing failed for %s: %s", self._address, err)
                return False
            if paired:
                # What matters is whether this pairing was proven, not whether
                # the stored context changed. The coordinator deliberately skips
                # rewriting provenance when the owner is identical, so comparing
                # contexts would read a correctly re-verified same-adapter bond
                # as "nothing was established" and delete a valid record.
                evidence = getattr(coordinator, "last_bond_evidence", None)
                if isinstance(evidence, BondEvidence) and evidence.proves_bond:
                    self._persist_repaired_bond(evidence.owner)
                else:
                    # Nothing established an owner this time, so the stored
                    # context still describes the pre-repair bond and can no
                    # longer authorize a host-side removal.
                    self._persist_repaired_bond(None)
            return paired

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
        bonded = self._bonded_now()
        if bonded:
            # Same rule as the coordinator-driven branch above: an unchanged
            # context is the expected result of re-verifying the same adapter,
            # so ask the reloaded coordinator what it actually proved rather
            # than reading "unchanged" as "unproven".
            reloaded = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
            evidence = getattr(reloaded, "last_bond_evidence", None)
            if isinstance(evidence, BondEvidence) and evidence.proves_bond:
                self._persist_repaired_bond(evidence.owner)
            else:
                self._persist_repaired_bond(None)
        return bonded

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
        verified_owner: BondOwner | None = None
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
                    source = client_source(client)
                    path = async_path_for_source(self.hass, source) if source else None
                    if path is not None:
                        verified_owner = BondOwner.from_path(path)
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
                        self._persist_repaired_bond(verified_owner)
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
    if issue_id == COMBINE_BEDS_ISSUE_ID:
        return CombineBedsRepairFlow()

    payload = data or {}
    evidence: BondEvidence | None = None
    raw_status = payload.get("evidence_status")
    status: BondVerificationStatus | None = None
    if isinstance(raw_status, str):
        try:
            status = BondVerificationStatus(raw_status)
        except ValueError:
            pass
    if status is not None:
        raw_transport = payload.get("evidence_transport")
        transport = TransportClass.UNKNOWN
        if isinstance(raw_transport, str):
            try:
                transport = TransportClass(raw_transport)
            except ValueError:
                pass
        observed_at = payload.get("evidence_observed_at")
        evidence = BondEvidence(
            status=status,
            owner=BondOwner(
                transport=transport,
                source=payload.get("evidence_source"),
                adapter=payload.get("evidence_adapter"),
            ),
            operation="pairing_required_issue",
            observed_at=(
                observed_at
                if isinstance(observed_at, str)
                else datetime.now(UTC).isoformat()
            ),
        )
    return PairingRequiredRepairFlow(
        address=payload.get("address", ""),
        name=payload.get("name", "your bed"),
        entry_id=payload.get("entry_id"),
        evidence=evidence,
    )
