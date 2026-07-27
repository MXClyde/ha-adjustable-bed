"""Tests for the fresh-advertisement gate (issue #458)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.adjustable_bed.bluetooth_freshness import (
    MAX_ADVERTISEMENT_AGE_SECONDS,
    RSSI_INVALIDATED,
    FreshnessStatus,
    async_check_advertisement,
    async_gate_connection,
    async_wait_for_advertisement,
)

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"

_FRESHNESS = "custom_components.adjustable_bed.bluetooth_freshness"

# A fixed "now" keeps the age arithmetic exact instead of racing the clock.
NOW = 10_000.0


@pytest.fixture(autouse=True)
def _frozen_clock():
    """Freeze the coarse monotonic clock the gate compares against."""
    with patch(f"{_FRESHNESS}._monotonic", return_value=NOW):
        yield


@pytest.fixture(autouse=True)
def _no_path_lookup():
    """Transport labelling is covered by its own tests."""
    with patch(f"{_FRESHNESS}.async_path_for_source", return_value=None):
        yield


def _service_info(*, age: float | None, rssi: int | None = -60, source: str = "hci0") -> Any:
    """Build a stand-in advertisement snapshot with a chosen age."""
    return SimpleNamespace(
        source=source,
        rssi=rssi,
        time=None if age is None else NOW - age,
        address=TEST_ADDRESS,
    )


def _patch_last_service_info(service_info: Any):
    return patch(
        f"{_FRESHNESS}.bluetooth.async_last_service_info",
        return_value=service_info,
    )


class TestFreshnessStatus:
    """The gate must only pass evidence it can actually stand behind."""

    async def test_recent_advertisement_is_fresh(self, hass: HomeAssistant) -> None:
        with _patch_last_service_info(_service_info(age=2.0)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.FRESH
        assert evidence.is_fresh
        assert evidence.age_seconds == pytest.approx(2.0)

    async def test_expired_advertisement_is_stale(self, hass: HomeAssistant) -> None:
        with _patch_last_service_info(
            _service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS + 1)
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.STALE
        assert not evidence.is_fresh

    async def test_fresh_non_connectable_view_wins_over_stale_connectable_history(
        self, hass: HomeAssistant
    ) -> None:
        """A stale strict snapshot must not hide a proxy's current fallback view."""

        def lookup(
            _hass: HomeAssistant, _address: str, *, connectable: bool
        ) -> Any:
            return _service_info(
                age=1.0 if not connectable else MAX_ADVERTISEMENT_AGE_SECONDS + 1,
                source="proxy",
            )

        with patch(
            f"{_FRESHNESS}.bluetooth.async_last_service_info",
            side_effect=lookup,
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)

        assert evidence.status is FreshnessStatus.FRESH
        assert evidence.source == "proxy"
        assert evidence.age_seconds == pytest.approx(1.0)

    async def test_advertisement_exactly_at_the_limit_still_passes(
        self, hass: HomeAssistant
    ) -> None:
        with _patch_last_service_info(_service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.FRESH

    async def test_missing_history_is_missing(self, hass: HomeAssistant) -> None:
        with _patch_last_service_info(None):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.MISSING

    async def test_invalidated_rssi_blocks_even_when_recent(
        self, hass: HomeAssistant
    ) -> None:
        """BlueZ publishes -127 when it has no reading; that is not a weak signal."""
        with _patch_last_service_info(_service_info(age=1.0, rssi=RSSI_INVALIDATED)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.RSSI_INVALID
        assert evidence.rssi == RSSI_INVALIDATED

    async def test_rssi_below_the_invalidation_value_also_blocks(
        self, hass: HomeAssistant
    ) -> None:
        with _patch_last_service_info(_service_info(age=1.0, rssi=-130)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.RSSI_INVALID

    async def test_a_weak_but_valid_signal_passes(self, hass: HomeAssistant) -> None:
        with _patch_last_service_info(_service_info(age=1.0, rssi=-126)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.FRESH

    async def test_missing_rssi_does_not_block_a_recent_advertisement(
        self, hass: HomeAssistant
    ) -> None:
        with _patch_last_service_info(_service_info(age=1.0, rssi=None)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.FRESH
        assert evidence.rssi is None

    @pytest.mark.parametrize(
        "bad_time", [None, "not-a-number", object(), float("nan"), float("inf")]
    )
    async def test_malformed_timestamp_is_unproven(
        self, hass: HomeAssistant, bad_time: Any
    ) -> None:
        """Without a usable timestamp we cannot claim freshness, so we do not."""
        info = _service_info(age=1.0)
        info.time = bad_time
        with _patch_last_service_info(info):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.INVALID_TIMESTAMP
        assert not evidence.is_fresh

    async def test_future_timestamp_is_unproven(self, hass: HomeAssistant) -> None:
        """A timestamp from a different clock is not evidence of a fresh sighting."""
        with _patch_last_service_info(_service_info(age=-30.0)):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.INVALID_TIMESTAMP

    async def test_history_lookup_failure_is_treated_as_missing(
        self, hass: HomeAssistant
    ) -> None:
        with patch(
            f"{_FRESHNESS}.bluetooth.async_last_service_info",
            side_effect=RuntimeError("no manager"),
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)
        assert evidence.status is FreshnessStatus.MISSING


class TestPerScannerFreshness:
    """A chosen adapter's own view is what decides whether it can reach the bed."""

    def _scanner_device(self, source: str, *, seen_at: float | None, rssi: int) -> Any:
        return SimpleNamespace(
            scanner=SimpleNamespace(
                source=source,
                discovered_device_timestamps=(
                    {} if seen_at is None else {TEST_ADDRESS: seen_at}
                ),
            ),
            advertisement=SimpleNamespace(rssi=rssi),
        )

    async def test_selected_scanner_view_is_used(self, hass: HomeAssistant) -> None:
        devices = [
            self._scanner_device("proxy", seen_at=NOW - 1.0, rssi=-50),
            self._scanner_device("hci0", seen_at=NOW - 5.0, rssi=-70),
        ]
        with patch(
            f"{_FRESHNESS}.bluetooth.async_scanner_devices_by_address",
            return_value=devices,
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS, source="hci0")
        assert evidence.status is FreshnessStatus.FRESH
        assert evidence.rssi == -70
        assert evidence.age_seconds == pytest.approx(5.0)

    async def test_stale_on_the_selected_scanner_blocks_despite_another_seeing_it(
        self, hass: HomeAssistant
    ) -> None:
        """Another proxy hearing the bed says nothing about the chosen adapter."""
        devices = [
            self._scanner_device("proxy", seen_at=NOW - 1.0, rssi=-50),
            self._scanner_device(
                "hci0", seen_at=NOW - MAX_ADVERTISEMENT_AGE_SECONDS - 10, rssi=-90
            ),
        ]
        with patch(
            f"{_FRESHNESS}.bluetooth.async_scanner_devices_by_address",
            return_value=devices,
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS, source="hci0")
        assert evidence.status is FreshnessStatus.STALE

    async def test_selected_scanner_no_longer_sees_the_bed(
        self, hass: HomeAssistant
    ) -> None:
        """Distinct from MISSING: the bed is there, the chosen adapter is not."""
        devices = [self._scanner_device("proxy", seen_at=NOW - 1.0, rssi=-50)]
        with patch(
            f"{_FRESHNESS}.bluetooth.async_scanner_devices_by_address",
            return_value=devices,
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS, source="hci0")
        assert evidence.status is FreshnessStatus.SOURCE_UNAVAILABLE


class TestConflictingScannerViews:
    """One scanner saying "no signal" must not discard another's real reading."""

    def _scanner_device(self, source: str, *, seen_at: float, rssi: int) -> Any:
        return SimpleNamespace(
            scanner=SimpleNamespace(
                source=source,
                discovered_device_timestamps={TEST_ADDRESS: seen_at},
            ),
            advertisement=SimpleNamespace(rssi=rssi),
        )

    async def test_a_newer_invalidated_rssi_does_not_hide_a_valid_reading(
        self, hass: HomeAssistant
    ) -> None:
        """A -127 published a second later would otherwise block a reachable bed."""
        with patch(
            f"{_FRESHNESS}.bluetooth.async_last_service_info",
            side_effect=lambda _hass, _address, connectable=True: (
                _service_info(age=5.0, rssi=-60, source="hci0")
                if connectable
                else _service_info(age=1.0, rssi=RSSI_INVALIDATED, source="proxy")
            ),
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)

        assert evidence.status is FreshnessStatus.FRESH
        assert evidence.rssi == -60
        assert evidence.source == "hci0"

    async def test_an_invalidated_rssi_still_wins_when_it_is_all_there_is(
        self, hass: HomeAssistant
    ) -> None:
        """Nothing can hear the bed, and that is the honest answer."""
        with patch(
            f"{_FRESHNESS}.bluetooth.async_last_service_info",
            side_effect=lambda _hass, _address, connectable=True: _service_info(
                age=1.0, rssi=RSSI_INVALIDATED
            ),
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS)

        assert evidence.status is FreshnessStatus.RSSI_INVALID

    async def test_the_selected_adapter_also_prefers_its_usable_view(
        self, hass: HomeAssistant
    ) -> None:
        """The per-scanner path compares two views of one adapter the same way."""
        devices = [self._scanner_device("hci0", seen_at=NOW - 5.0, rssi=-70)]
        invalid = [self._scanner_device("hci0", seen_at=NOW - 1.0, rssi=RSSI_INVALIDATED)]
        with patch(
            f"{_FRESHNESS}.bluetooth.async_scanner_devices_by_address",
            side_effect=lambda _hass, _address, connectable=True: (
                invalid if connectable else devices
            ),
        ):
            evidence = async_check_advertisement(hass, TEST_ADDRESS, source="hci0")

        assert evidence.status is FreshnessStatus.FRESH
        assert evidence.rssi == -70


class TestGateConnection:
    """The BLEDevice must be resolved after the gate, never before it."""

    async def test_fresh_evidence_resolves_a_current_device(
        self, hass: HomeAssistant
    ) -> None:
        device = SimpleNamespace(address=TEST_ADDRESS)
        with (
            _patch_last_service_info(_service_info(age=1.0)),
            patch(
                f"{_FRESHNESS}.async_resolve_ble_device", return_value=device
            ) as resolve,
        ):
            evidence, resolved = async_gate_connection(hass, TEST_ADDRESS)
        assert evidence.is_fresh
        assert resolved is device
        resolve.assert_called_once()

    async def test_stale_evidence_never_resolves_a_device(
        self, hass: HomeAssistant
    ) -> None:
        """Nothing may reach establish_connection once the gate has refused."""
        with (
            _patch_last_service_info(
                _service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS + 60)
            ),
            patch(f"{_FRESHNESS}.async_resolve_ble_device") as resolve,
        ):
            evidence, resolved = async_gate_connection(hass, TEST_ADDRESS)
        assert not evidence.is_fresh
        assert resolved is None
        resolve.assert_not_called()

    async def test_missing_evidence_never_resolves_a_device(
        self, hass: HomeAssistant
    ) -> None:
        with (
            _patch_last_service_info(None),
            patch(f"{_FRESHNESS}.async_resolve_ble_device") as resolve,
        ):
            _evidence, resolved = async_gate_connection(hass, TEST_ADDRESS)
        assert resolved is None
        resolve.assert_not_called()

    async def test_a_fresh_advertisement_after_a_refusal_allows_the_next_attempt(
        self, hass: HomeAssistant
    ) -> None:
        """Retry must work without restarting the flow."""
        stale = _service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS + 5)
        fresh = _service_info(age=1.0)
        # Each check consults both scanner views, so two lookups per call.
        with patch(
            f"{_FRESHNESS}.bluetooth.async_last_service_info",
            side_effect=[stale, stale, fresh, fresh],
        ):
            assert not async_check_advertisement(hass, TEST_ADDRESS).is_fresh
            assert async_check_advertisement(hass, TEST_ADDRESS).is_fresh


class TestWaitForAdvertisement:
    """A bed between advertising intervals deserves a second look, not a refusal."""

    @staticmethod
    def _advancing_clock(step: float = 1.0):
        """Return a monotonic stand-in that moves forward on every read.

        The module-level fixture freezes time so ages are exact; the wait loop
        needs the opposite, since its only exit condition is the clock.
        """
        state = {"now": NOW}

        def _now() -> float:
            value = state["now"]
            state["now"] += step
            return value

        return patch(f"{_FRESHNESS}._monotonic", side_effect=_now)

    async def test_an_already_fresh_bed_returns_without_waiting(
        self, hass: HomeAssistant
    ) -> None:
        device = SimpleNamespace(address=TEST_ADDRESS)
        with (
            _patch_last_service_info(_service_info(age=1.0)) as lookup,
            patch(f"{_FRESHNESS}.async_resolve_ble_device", return_value=device),
        ):
            evidence, resolved = await async_wait_for_advertisement(hass, TEST_ADDRESS)
        assert evidence.is_fresh
        assert resolved is device
        # One glance at the history - both scanner views, but no polling.
        assert lookup.call_count == 2

    async def test_an_advertisement_arriving_during_the_wait_is_accepted(
        self, hass: HomeAssistant
    ) -> None:
        device = SimpleNamespace(address=TEST_ADDRESS)
        stale = _service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS + 5)
        fresh = _service_info(age=1.0)
        with (
            self._advancing_clock(step=0.0),
            patch(
                f"{_FRESHNESS}.bluetooth.async_last_service_info",
                side_effect=[stale, stale, fresh],
            ),
            patch(f"{_FRESHNESS}.async_resolve_ble_device", return_value=device),
        ):
            evidence, resolved = await async_wait_for_advertisement(
                hass, TEST_ADDRESS, wait_timeout=60.0, poll_interval=0.001
            )
        assert evidence.is_fresh
        assert resolved is device

    async def test_fresh_evidence_without_a_device_keeps_waiting(
        self, hass: HomeAssistant
    ) -> None:
        """Scanner history can change between freshness and device resolution."""
        device = SimpleNamespace(address=TEST_ADDRESS)
        with (
            self._advancing_clock(step=0.0),
            _patch_last_service_info(_service_info(age=1.0)),
            patch(
                f"{_FRESHNESS}.async_resolve_ble_device",
                side_effect=[None, device],
            ) as resolve,
        ):
            evidence, resolved = await async_wait_for_advertisement(
                hass, TEST_ADDRESS, wait_timeout=60.0, poll_interval=0.001
            )

        assert evidence.is_fresh
        assert resolved is device
        assert resolve.call_count == 2

    async def test_a_silent_bed_gives_up_without_resolving_a_device(
        self, hass: HomeAssistant
    ) -> None:
        with (
            self._advancing_clock(step=5.0),
            _patch_last_service_info(
                _service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS + 5)
            ),
            patch(f"{_FRESHNESS}.async_resolve_ble_device") as resolve,
        ):
            evidence, resolved = await async_wait_for_advertisement(
                hass, TEST_ADDRESS, wait_timeout=10.0, poll_interval=0.001
            )
        assert evidence.status is FreshnessStatus.STALE
        assert resolved is None
        resolve.assert_not_called()

    async def test_progress_is_reported_over_the_known_wait(
        self, hass: HomeAssistant
    ) -> None:
        """The wait is the only setup phase whose duration is genuinely known."""
        seen: list[float] = []
        with (
            self._advancing_clock(step=2.0),
            _patch_last_service_info(
                _service_info(age=MAX_ADVERTISEMENT_AGE_SECONDS + 5)
            ),
        ):
            await async_wait_for_advertisement(
                hass,
                TEST_ADDRESS,
                wait_timeout=20.0,
                poll_interval=0.001,
                on_progress=seen.append,
            )
        assert seen
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen == sorted(seen)
        assert seen[-1] == 1.0
