"""Strict bond verification and durable bond provenance.

Two questions get conflated constantly and must not be:

*Is this link authenticated?* and *who owns the bond that authenticated it?*

The first is answered by actually exercising an authentication-gated operation.
Connecting is not evidence — the connect path deliberately keeps an unbonded
link — and neither is `client.pair()` returning, since a backend can report a
pairing request as sent without a bond ever forming. Only a read that would fail
on an unbonded link and did not fail counts.

The second is answered by the transport that carried that read. A bond made over
an ESPHome proxy lives on the proxy; a bond made over a host adapter lives in the
host's BlueZ. Recording which is why a later unpair can be safe (issue #459).

The verifier is deliberately four-valued. "Not an authentication error" is not
the same as "verified": a missing characteristic, a timeout or a generic GATT
error prove nothing either way, and treating them as success is how an entry ends
up marked bonded while the bed is still refusing every write.

Runtime behaviour for beds already configured is untouched. The coordinator
keeps its existing lenient handling, which exists because real hardware answers
this probe inconsistently. What changes is that a *new* bond marker, and any
destructive recovery, now requires a positive ``VERIFIED``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .ble_auth import is_ble_authentication_error
from .bluetooth_transport import ConnectionPath, TransportClass
from .const import (
    BED_TYPE_OKIN_CST,
    DEVICE_INFO_CHARS,
    DEVICE_INFO_READ_TIMEOUT,
    requires_pairing,
)

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

# Entry-data key holding who owns the bond and how it was proven. Kept separate
# from the legacy boolean so an entry written by an older version is never
# mistaken for one with real provenance.
CONF_BLE_BOND_CONTEXT = "ble_bond_context"

BOND_CONTEXT_VERSION = 1


class BondVerificationStatus(StrEnum):
    """How much a verification attempt actually established."""

    VERIFIED = "verified"
    AUTH_FAILED = "auth_failed"
    INCONCLUSIVE = "inconclusive"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class BondOwner:
    """Which transport stores the bond in question."""

    transport: TransportClass = TransportClass.UNKNOWN
    source: str | None = None
    adapter: str | None = None

    @property
    def is_host(self) -> bool:
        """Return True only when the bond is provably on this host."""
        return self.transport is TransportClass.LOCAL

    @classmethod
    def from_path(cls, path: ConnectionPath | None) -> BondOwner:
        """Derive ownership from the path a verified operation travelled."""
        if path is None:
            return cls()
        return cls(transport=path.transport, source=path.source, adapter=path.adapter)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for diagnostics and entry data."""
        return {
            "transport": str(self.transport),
            "source": self.source,
            "adapter": self.adapter,
        }


@dataclass(frozen=True, slots=True)
class BondEvidence:
    """One observation about a bond, and what carried it."""

    status: BondVerificationStatus
    owner: BondOwner
    operation: str
    observed_at: str
    error: str | None = None

    @property
    def proves_bond(self) -> bool:
        """Return True only for a positively verified bond."""
        return self.status is BondVerificationStatus.VERIFIED

    @property
    def proves_stale_host_bond(self) -> bool:
        """Return True when this is grounds to suspect a stale *host* bond.

        Requires both an explicit authentication failure and a transport we have
        proven is local. An authentication failure carried by a proxy says
        nothing about the host's BlueZ, and acting on it would delete a bond
        that was never involved (issue #459).
        """
        return self.status is BondVerificationStatus.AUTH_FAILED and self.owner.is_host

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for diagnostics."""
        return {
            "status": str(self.status),
            "owner": self.owner.as_dict(),
            "operation": self.operation,
            "observed_at": self.observed_at,
            "error": self.error,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def has_evidence_backed_verifier(bed_type: str | None, protocol_variant: str | None) -> bool:
    """Return True when this protocol has a known authentication-gated read.

    Only beds this integration already treats as bond-gated qualify. For those,
    an unbonded link is observed in the field to fail the Device Information
    read with GATT error 5. For anything else there is no evidence that the read
    is gated at all, so a successful read would prove nothing and must not be
    dressed up as verification.
    """
    # This receiver never answers the Device Information read, even when bonded,
    # so recovery could destroy its old bond but could never prove a replacement.
    return bed_type != BED_TYPE_OKIN_CST and requires_pairing(
        bed_type or "", protocol_variant
    )


async def async_verify_authenticated_access(
    client: BleakClient,
    *,
    bed_type: str | None,
    protocol_variant: str | None,
    path: ConnectionPath | None,
    operation: str,
) -> BondEvidence:
    """Exercise an authentication-gated read and report exactly what it showed."""
    owner = BondOwner.from_path(path)

    if not has_evidence_backed_verifier(bed_type, protocol_variant):
        return BondEvidence(
            status=BondVerificationStatus.UNSUPPORTED,
            owner=owner,
            operation=operation,
            observed_at=_now(),
        )

    if client is None or not getattr(client, "is_connected", False):
        return BondEvidence(
            status=BondVerificationStatus.INCONCLUSIVE,
            owner=owner,
            operation=operation,
            observed_at=_now(),
            error="not_connected",
        )

    try:
        await asyncio.wait_for(
            client.read_gatt_char(DEVICE_INFO_CHARS["model_number"]),
            DEVICE_INFO_READ_TIMEOUT,
        )
    except Exception as err:  # noqa: BLE001 - the failure mode is the result
        if is_ble_authentication_error(err):
            return BondEvidence(
                status=BondVerificationStatus.AUTH_FAILED,
                owner=owner,
                operation=operation,
                observed_at=_now(),
                error=str(err),
            )
        # A timeout, an absent characteristic or a generic GATT error tells us
        # nothing. The OKIN CST receiver, for one, simply never answers this
        # read even when perfectly bonded.
        _LOGGER.debug("Bond verification for %s was inconclusive: %s", operation, err)
        return BondEvidence(
            status=BondVerificationStatus.INCONCLUSIVE,
            owner=owner,
            operation=operation,
            observed_at=_now(),
            error=str(err),
        )

    return BondEvidence(
        status=BondVerificationStatus.VERIFIED,
        owner=owner,
        operation=operation,
        observed_at=_now(),
    )


def build_bond_context(evidence: BondEvidence) -> dict[str, Any]:
    """Return the entry-data provenance record for a verified bond."""
    return {
        "version": BOND_CONTEXT_VERSION,
        "transport": str(evidence.owner.transport),
        "source": evidence.owner.source,
        "adapter": evidence.owner.adapter,
        "verification": evidence.operation,
        "verified_at": evidence.observed_at,
    }


def bond_owner_from_entry(entry_data: dict[str, Any] | Any) -> BondOwner:
    """Return the recorded bond owner, or an unknown owner.

    An entry written before provenance existed carries only a boolean, so its
    owner is genuinely unknown. That must stay unknown rather than defaulting to
    "local": defaulting would let a legacy entry authorize a host-side unpair for
    a bond that may well live on a proxy.
    """
    context = None
    try:
        context = entry_data.get(CONF_BLE_BOND_CONTEXT)
    except AttributeError:
        return BondOwner()
    if not isinstance(context, dict):
        return BondOwner()
    transport = context.get("transport")
    try:
        transport_class = TransportClass(transport)
    except ValueError:
        transport_class = TransportClass.UNKNOWN
    return BondOwner(
        transport=transport_class,
        source=context.get("source"),
        adapter=context.get("adapter"),
    )
