"""Confirmed recovery from a stale bond stored on this Home Assistant host.

A bond can survive on the host while the bed no longer honours it: the bed was
factory reset, re-paired with a phone, or simply forgot. The link then connects
and every encrypted read fails, which looks identical to "pairing is broken".
The fix is to remove the host's copy and make a new one — but only ever with the
user's explicit say-so, and only when the evidence points at the host.

Three rules make that safe, and every one of them exists because getting it
wrong destroys something the user cannot easily get back.

**Only local authentication evidence counts.** A proxy keeps its own bond store
that the host cannot read, so an authentication failure carried by a proxy says
nothing about the host's BlueZ. Removing a host bond on that evidence would
delete state that was never involved. Unknown ownership is treated the same way:
not proven local means not eligible.

**Reachability is proven before anything is destroyed.** Removing first would
leave a sleeping bed with no bond and no way to make a new one until someone
walks over to it.

**Success means verified.** A new bond is only recorded when an
authentication-gated operation actually succeeded over the link. Anything else
leaves the repair open, because a repair that closes itself on an unproven fix
is worse than one that stays open.

Beds that grant one connection per pairing window are excluded entirely.
Removing their bond drops the link and the box will not grant another until it
is power-cycled, so a single background sequence cannot both remove the bond and
keep the link it was going to recover through. Those beds get guidance instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .bluetooth_bond import (
    BondSelectionStatus,
    async_read_local_bonds,
    async_remove_local_bond,
    select_local_bond,
)
from .bluetooth_freshness import ADVERTISEMENT_WAIT_SECONDS, async_wait_for_advertisement
from .bluetooth_transport import (
    ConnectionPath,
    TransportClass,
    async_path_for_source,
    async_predict_path,
    client_source,
)
from .bond_verification import (
    BondOwner,
    BondVerificationStatus,
    async_verify_authenticated_access,
    build_bond_context,
)
from .const import (
    ADAPTER_AUTO,
    CONNECTION_PROFILES,
    DEFAULT_CONNECTION_PROFILE,
    grants_one_connection_per_pairing_window,
    requires_pairing_after_service_discovery,
)
from .setup_operation import OperationOutcome, OperationResult, SetupAction

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)


class RecoveryEligibility(str):
    """Why a stale-bond recovery is or is not on offer."""

    ELIGIBLE = "eligible"
    NO_EVIDENCE = "no_evidence"
    NOT_LOCAL = "not_local"
    NO_BOND = "no_bond"
    AMBIGUOUS = "ambiguous"
    UNREADABLE = "unreadable"
    KEEPS_FIRST_LINK = "keeps_first_link"


@dataclass(frozen=True, slots=True)
class RecoveryOffer:
    """Whether recovery may be offered, and against which exact bond."""

    eligibility: str
    record: Any = None
    owner: BondOwner | None = None

    @property
    def is_eligible(self) -> bool:
        """Return True only when an exact host bond may be replaced."""
        return self.eligibility == RecoveryEligibility.ELIGIBLE and self.record is not None


def evidence_is_local_auth_failure(issue_data: dict[str, Any]) -> bool:
    """Return True when the repair was raised by a local authentication failure.

    Reads the flattened evidence the coordinator attaches to the issue. An issue
    raised before that existed carries none, which correctly reads as "not
    eligible" rather than as permission.
    """
    return (
        issue_data.get("evidence_status") == BondVerificationStatus.AUTH_FAILED.value
        and issue_data.get("evidence_transport") == TransportClass.LOCAL.value
    )


async def async_recovery_offer(
    hass: HomeAssistant,
    *,
    address: str,
    issue_data: dict[str, Any],
    bed_type: str | None,
    protocol_variant: str | None,
) -> RecoveryOffer:
    """Decide whether a stale host bond may be replaced, and which one."""
    if grants_one_connection_per_pairing_window(bed_type or "", protocol_variant):
        # Removing the bond drops the only link this box will grant until it is
        # power-cycled, so recovery cannot both remove and reconnect.
        return RecoveryOffer(eligibility=RecoveryEligibility.KEEPS_FIRST_LINK)

    if not evidence_is_local_auth_failure(issue_data):
        transport = issue_data.get("evidence_transport")
        return RecoveryOffer(
            eligibility=(
                RecoveryEligibility.NOT_LOCAL
                if transport
                else RecoveryEligibility.NO_EVIDENCE
            )
        )

    owner = BondOwner(
        transport=TransportClass.LOCAL,
        source=issue_data.get("evidence_source"),
        adapter=issue_data.get("evidence_adapter"),
    )
    inventory = await async_read_local_bonds(address)
    selection = select_local_bond(
        inventory, owner_source=owner.source, owner_adapter=owner.adapter
    )
    if selection.status is BondSelectionStatus.UNREADABLE:
        return RecoveryOffer(eligibility=RecoveryEligibility.UNREADABLE, owner=owner)
    if selection.status is BondSelectionStatus.NO_BOND:
        return RecoveryOffer(eligibility=RecoveryEligibility.NO_BOND, owner=owner)
    if not selection.is_exact:
        return RecoveryOffer(eligibility=RecoveryEligibility.AMBIGUOUS, owner=owner)
    return RecoveryOffer(
        eligibility=RecoveryEligibility.ELIGIBLE, record=selection.record, owner=owner
    )


async def async_recover_local_bond(
    hass: HomeAssistant,
    *,
    address: str,
    name: str,
    offer: RecoveryOffer,
    bed_type: str | None,
    protocol_variant: str | None,
    preferred_adapter: str = ADAPTER_AUTO,
    report_action: Callable[[SetupAction], None] | None = None,
    report_progress: Callable[[float], None] | None = None,
    report_path: Callable[[ConnectionPath | None], None] | None = None,
    track_client: Callable[[BleakClient | None], None] | None = None,
) -> OperationResult:
    """Remove one exact host bond, pair again, and prove the new bond.

    Everything happens in the calling task, because the per-address connect lock
    is reentrant per ``asyncio.Task`` and the whole client lifetime has to sit
    inside one owner.
    """
    from bleak import BleakClient
    from bleak_retry_connector import establish_connection

    from .address_lock import async_get_connect_lock

    def _report(action: SetupAction) -> None:
        if report_action is not None:
            report_action(action)

    if not offer.is_eligible:
        return OperationResult(
            outcome=OperationOutcome.UNPAIR_FAILED, detail=offer.eligibility
        )

    # Reachability first. Removing a bond from a bed that cannot answer leaves
    # the user worse off than the stale bond did.
    _report(SetupAction.LOCATING)
    evidence, device = await async_wait_for_advertisement(
        hass,
        address,
        wait_timeout=ADVERTISEMENT_WAIT_SECONDS,
        on_progress=report_progress,
    )
    if not evidence.is_fresh or device is None:
        _LOGGER.info(
            "Not recovering the bond for %s: the bed is not advertising (%s)",
            address,
            evidence.status,
        )
        return OperationResult(
            outcome=OperationOutcome.NOT_ADVERTISING, detail=str(evidence.status)
        )

    _report(SetupAction.UNPAIRING)
    removal = await async_remove_local_bond(offer.record)
    if not removal.succeeded:
        _LOGGER.warning(
            "Not recovering the bond for %s: removal failed (%s)", address, removal.error
        )
        return OperationResult(
            outcome=OperationOutcome.UNPAIR_FAILED,
            detail=removal.error or str(removal.status),
            payload=removal,
        )

    pair_after_discovery = bool(
        bed_type and requires_pairing_after_service_discovery(bed_type, protocol_variant)
    )
    prediction = async_predict_path(hass, address, preferred_adapter)

    client: BleakClient | None = None
    async with async_get_connect_lock(hass, address):
        try:
            _report(SetupAction.CONNECTING)
            client = await establish_connection(
                BleakClient,
                device,
                address,
                max_attempts=1,
                timeout=CONNECTION_PROFILES[DEFAULT_CONNECTION_PROFILE].connection_timeout,
                pair=not pair_after_discovery,
                use_services_cache=not pair_after_discovery,
            )
            if track_client is not None:
                track_client(client)

            actual_source = client_source(client)
            path = (
                async_path_for_source(hass, actual_source)
                if actual_source
                else prediction.chosen
            )
            if report_path is not None:
                report_path(path)

            if pair_after_discovery:
                _report(SetupAction.PAIRING)
                await client.pair()

            _report(SetupAction.VERIFYING_BOND)
            bond = await async_verify_authenticated_access(
                client,
                bed_type=bed_type,
                protocol_variant=protocol_variant,
                path=path,
                operation="stale_bond_recovery",
            )
        except Exception as err:  # noqa: BLE001 - a failure here is an outcome
            _LOGGER.warning("Bond recovery for %s failed: %s", name, err)
            return OperationResult(
                outcome=OperationOutcome.CONNECTION_FAILED,
                detail=str(err) or err.__class__.__name__,
            )
        finally:
            _report(SetupAction.DISCONNECTING)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    _LOGGER.debug("Disconnect after recovery failed", exc_info=True)
                if track_client is not None:
                    track_client(None)

    if not bond.proves_bond:
        # The old bond is gone and the new one is unproven. Say exactly that:
        # claiming success here would resolve a repair the user still has.
        return OperationResult(
            outcome=OperationOutcome.BOND_VERIFICATION_FAILED,
            detail=str(bond.status),
            payload=bond,
        )
    return OperationResult(
        outcome=OperationOutcome.SUCCESS, payload=bond, path=bond_path(bond)
    )


def bond_path(bond: Any) -> ConnectionPath | None:
    """Return a path describing where a verified bond ended up."""
    owner = getattr(bond, "owner", None)
    if owner is None or owner.source is None:
        return None
    return ConnectionPath(
        source=owner.source, transport=owner.transport, adapter=owner.adapter
    )


def recovery_context(bond: Any) -> dict[str, Any]:
    """Return the entry-data provenance for a bond created by recovery."""
    return build_bond_context(bond)
