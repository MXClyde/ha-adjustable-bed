"""Remembers that the user said their beds are separate, not two sides of one.

The Dual Bed suggestion is a fixable Repairs issue, and Home Assistant offers no
Ignore action for those: a fixable issue opens its fix flow, so the only way out
of the dialog is to close it, which leaves the issue sitting in Repairs. For
someone who genuinely owns two beds that is a permanent, unanswerable warning.

The dismissal is recorded against the exact set of addresses that was suggested,
not as a global "never ask" flag. Two beds the user has called separate stay
separate, while adding a third bed later is a different question and gets asked
again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_combine_suggestion"
_DATA_KEY = f"{DOMAIN}_combine_suggestion"

KEY_DISMISSED = "dismissed_addresses"


def normalize_addresses(addresses: Iterable[str]) -> frozenset[str]:
    """Return a comparable address set, case and order independent."""
    return frozenset(address.upper() for address in addresses if isinstance(address, str))


class CombineSuggestionState:
    """The set of beds the user has already declared separate."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the backing store; call ``async_load`` before reading."""
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._dismissed: frozenset[str] = frozenset()
        self._loaded = False

    @property
    def dismissed(self) -> frozenset[str]:
        """Return the dismissed address set.

        Cached deliberately. The Repairs refresh runs from synchronous entry
        lifecycle callbacks, which cannot await a store read.
        """
        return self._dismissed

    async def async_load(self) -> None:
        """Read the persisted dismissal once, at integration setup."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if isinstance(loaded, dict):
            self._dismissed = normalize_addresses(loaded.get(KEY_DISMISSED) or ())
        self._loaded = True

    async def async_dismiss(self, addresses: Iterable[str]) -> None:
        """Record that this exact set of beds is not one physical bed."""
        dismissed = normalize_addresses(addresses)
        if dismissed == self._dismissed:
            return
        self._dismissed = dismissed
        await self._store.async_save({KEY_DISMISSED: sorted(dismissed)})
        _LOGGER.debug("Combine suggestion dismissed for %s", sorted(dismissed))


def _async_get_state(hass: HomeAssistant) -> CombineSuggestionState:
    """Return the singleton dismissal state for this Home Assistant instance."""
    state: CombineSuggestionState | None = hass.data.get(_DATA_KEY)
    if state is None:
        state = CombineSuggestionState(hass)
        hass.data[_DATA_KEY] = state
    return state


async def async_load_dismissal(hass: HomeAssistant) -> None:
    """Load the persisted dismissal so the sync refresh can consult it."""
    await _async_get_state(hass).async_load()


def async_is_dismissed(hass: HomeAssistant, addresses: Iterable[str]) -> bool:
    """Return True when the user already called exactly these beds separate."""
    return _async_get_state(hass).dismissed == normalize_addresses(addresses)


async def async_dismiss(hass: HomeAssistant, addresses: Iterable[str]) -> None:
    """Persist that this set of beds is separate and should not be suggested."""
    await _async_get_state(hass).async_dismiss(addresses)
