"""Tests for the transport-aware Bluetooth path model (issue #456)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.adjustable_bed.bluetooth_transport import (
    ConnectionPath,
    TransportClass,
    async_connection_paths,
    async_path_for_source,
    async_predict_path,
    async_resolve_ble_device,
    classify_scanner,
)

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"

_TRANSPORT = "custom_components.adjustable_bed.bluetooth_transport"


def _scanner(
    source: str,
    *,
    scanner_type: str,
    name: str | None = None,
    adapter: str | None = None,
    connectable: bool = True,
) -> SimpleNamespace:
    """Build a stand-in for a Home Assistant scanner object.

    Deliberately not a habluetooth instance: that forces ``classify_scanner``
    down its declared-type path, which is the branch a non-habluetooth scanner
    (or a future scanner class) would take.
    """
    return SimpleNamespace(
        source=source,
        name=name or source,
        adapter=adapter,
        connectable=connectable,
        details=SimpleNamespace(scanner_type=scanner_type),
    )


def _scanner_device(scanner: Any, rssi: int | None, score: float | None = None) -> SimpleNamespace:
    """Build a stand-in for habluetooth's BluetoothScannerDevice."""
    return SimpleNamespace(
        scanner=scanner,
        ble_device=SimpleNamespace(address=TEST_ADDRESS),
        advertisement=SimpleNamespace(rssi=rssi),
        score_connection_path=(lambda _diff, score=score: score) if score is not None else None,
    )


def _patch_scanner_devices(devices: list[Any]):
    """Patch the HA lookup that returns every scanner seeing an address."""
    return patch(
        f"{_TRANSPORT}.bluetooth.async_scanner_devices_by_address",
        return_value=devices,
    )


@pytest.fixture(autouse=True)
def _no_scanner_registrations():
    """Skip config-entry lookups; naming is not what these tests exercise."""
    with patch(f"{_TRANSPORT}.async_scanner_registrations", return_value={}):
        yield


class TestClassifyScanner:
    """Transport class must come from scanner objects, never from name shape."""

    def test_remote_scanner_type_is_a_proxy(self) -> None:
        assert classify_scanner(_scanner("x", scanner_type="remote")) is TransportClass.PROXY

    @pytest.mark.parametrize("scanner_type", ["usb", "uart"])
    def test_host_adapter_types_are_local(self, scanner_type: str) -> None:
        assert classify_scanner(_scanner("x", scanner_type=scanner_type)) is TransportClass.LOCAL

    def test_unclassifiable_scanner_is_unknown(self) -> None:
        """Unknown must not silently become local: it gates destructive actions."""
        scanner = _scanner("x", scanner_type="something-new")
        assert classify_scanner(scanner) is TransportClass.UNKNOWN

    def test_local_adapter_mac_source_is_not_mistaken_for_a_proxy(self) -> None:
        """A host adapter's source is a MAC, which only looks remote."""
        scanner = _scanner("AA:BB:CC:11:22:33", scanner_type="usb")
        assert classify_scanner(scanner) is TransportClass.LOCAL

    def test_proxy_named_without_esphome_is_still_a_proxy(self) -> None:
        """Substring matching on "esphome" misses proxies from other integrations."""
        scanner = _scanner("shelly-hall", scanner_type="remote", name="Hall Shelly")
        assert classify_scanner(scanner) is TransportClass.PROXY

    def test_real_remote_scanner_class_is_detected_without_details(self) -> None:
        """The class hierarchy is authoritative when habluetooth is in play."""
        from habluetooth import BaseHaRemoteScanner

        class _Remote(BaseHaRemoteScanner):
            pass

        scanner = _Remote.__new__(_Remote)
        assert classify_scanner(scanner) is TransportClass.PROXY


class TestConnectionPaths:
    """Ranking must reproduce Home Assistant's own connection routing."""

    async def test_no_scanner_data_returns_no_paths(self, hass: HomeAssistant) -> None:
        with _patch_scanner_devices([]):
            assert async_connection_paths(hass, TEST_ADDRESS) == ()

    async def test_local_only(self, hass: HomeAssistant) -> None:
        devices = [_scanner_device(_scanner("hci0", scanner_type="usb"), -55)]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert [path.transport for path in paths] == [TransportClass.LOCAL]
        assert paths[0].rssi == -55

    async def test_proxy_only(self, hass: HomeAssistant) -> None:
        devices = [_scanner_device(_scanner("proxy", scanner_type="remote"), -70)]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert [path.transport for path in paths] == [TransportClass.PROXY]

    async def test_mixed_paths_rank_by_signal(self, hass: HomeAssistant) -> None:
        devices = [
            _scanner_device(_scanner("hci0", scanner_type="usb"), -80),
            _scanner_device(_scanner("proxy", scanner_type="remote"), -50),
        ]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert [path.source for path in paths] == ["proxy", "hci0"]

    async def test_score_overrides_raw_signal(self, hass: HomeAssistant) -> None:
        """A busy adapter with the best RSSI loses, exactly as it does in HA."""
        devices = [
            _scanner_device(_scanner("busy", scanner_type="usb"), -50, score=-127.0),
            _scanner_device(_scanner("free", scanner_type="remote"), -60, score=-60.0),
        ]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert [path.source for path in paths] == ["free", "busy"]

    async def test_each_path_is_scored_once(self, hass: HomeAssistant) -> None:
        """Ranking and path construction must share one slot-state snapshot."""
        scorers = [MagicMock(return_value=-40.0), MagicMock(return_value=-60.0)]
        devices = [
            SimpleNamespace(
                scanner=_scanner("hci0", scanner_type="usb"),
                ble_device=SimpleNamespace(address=TEST_ADDRESS),
                advertisement=SimpleNamespace(rssi=-40),
                score_connection_path=scorers[0],
            ),
            SimpleNamespace(
                scanner=_scanner("proxy", scanner_type="remote"),
                ble_device=SimpleNamespace(address=TEST_ADDRESS),
                advertisement=SimpleNamespace(rssi=-60),
                score_connection_path=scorers[1],
            ),
        ]
        with _patch_scanner_devices(devices):
            async_connection_paths(hass, TEST_ADDRESS)
        assert [scorer.call_count for scorer in scorers] == [1, 1]

    async def test_missing_rssi_sorts_last_and_reports_none(
        self, hass: HomeAssistant
    ) -> None:
        devices = [
            _scanner_device(_scanner("no-signal", scanner_type="usb"), None),
            _scanner_device(_scanner("proxy", scanner_type="remote"), -90),
        ]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert [path.source for path in paths] == ["proxy", "no-signal"]
        assert paths[1].rssi is None

    async def test_unscorable_devices_keep_signal_order(self, hass: HomeAssistant) -> None:
        """A partially scorable set must not be reshuffled by a missing score."""
        devices = [
            _scanner_device(_scanner("weak", scanner_type="usb"), -90, score=10.0),
            _scanner_device(_scanner("strong", scanner_type="remote"), -40),
        ]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert [path.source for path in paths] == ["strong", "weak"]

    async def test_lookup_failure_is_not_fatal(self, hass: HomeAssistant) -> None:
        with patch(
            f"{_TRANSPORT}.bluetooth.async_scanner_devices_by_address",
            side_effect=RuntimeError("no manager"),
        ):
            assert async_connection_paths(hass, TEST_ADDRESS) == ()

    async def test_empty_connectable_view_uses_non_connectable_proxy_paths(
        self, hass: HomeAssistant
    ) -> None:
        """Prediction must describe the same fallback path resolution can use."""
        scanner_device = _scanner_device(
            _scanner("proxy", scanner_type="remote", connectable=False), -55
        )

        def lookup(
            _hass: HomeAssistant, _address: str, *, connectable: bool
        ) -> list[Any]:
            return [] if connectable else [scanner_device]

        with patch(
            f"{_TRANSPORT}.bluetooth.async_scanner_devices_by_address",
            side_effect=lookup,
        ):
            paths = async_connection_paths(hass, TEST_ADDRESS)

        assert len(paths) == 1
        assert paths[0].source == "proxy"
        assert paths[0].transport is TransportClass.PROXY
        assert paths[0].connectable is False


class TestPathPrediction:
    """The prediction shown before setup must match what HA will really do."""

    async def test_best_path_is_chosen_automatically(self, hass: HomeAssistant) -> None:
        devices = [
            _scanner_device(_scanner("hci0", scanner_type="usb"), -80),
            _scanner_device(_scanner("proxy", scanner_type="remote"), -50),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.chosen is not None
        assert prediction.chosen.source == "proxy"
        assert prediction.proxy_pairing_risk is True

    async def test_local_alternative_is_offered_when_a_proxy_wins(
        self, hass: HomeAssistant
    ) -> None:
        devices = [
            _scanner_device(_scanner("hci0", scanner_type="usb"), -80),
            _scanner_device(_scanner("proxy", scanner_type="remote"), -50),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.local_alternative is not None
        assert prediction.local_alternative.source == "hci0"

    async def test_no_local_alternative_when_local_already_wins(
        self, hass: HomeAssistant
    ) -> None:
        devices = [
            _scanner_device(_scanner("hci0", scanner_type="usb"), -40),
            _scanner_device(_scanner("proxy", scanner_type="remote"), -70),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.local_alternative is None
        assert prediction.proxy_pairing_risk is False

    async def test_preferred_adapter_overrides_ranking(self, hass: HomeAssistant) -> None:
        devices = [
            _scanner_device(_scanner("hci0", scanner_type="usb"), -80),
            _scanner_device(_scanner("proxy", scanner_type="remote"), -50),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "hci0")
        assert prediction.chosen is not None
        assert prediction.chosen.source == "hci0"
        assert prediction.preferred_available is True

    async def test_preferred_adapter_that_cannot_see_the_bed_is_reported(
        self, hass: HomeAssistant
    ) -> None:
        """A disappeared preferred scanner must be visible, not silently replaced."""
        devices = [_scanner_device(_scanner("proxy", scanner_type="remote"), -50)]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "hci0")
        assert prediction.preferred_available is False
        assert prediction.chosen is not None
        assert prediction.chosen.source == "proxy"

    async def test_no_visible_scanner_predicts_nothing(self, hass: HomeAssistant) -> None:
        with _patch_scanner_devices([]):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.chosen is None
        assert prediction.local_alternative is None
        assert prediction.proxy_pairing_risk is False


class TestDeviceResolution:
    """Freshness fallbacks must still produce a device that can be connected."""

    async def test_preferred_source_uses_the_non_connectable_view(
        self, hass: HomeAssistant
    ) -> None:
        device = SimpleNamespace(address=TEST_ADDRESS)
        scanner_device = SimpleNamespace(
            scanner=_scanner("proxy", scanner_type="remote"),
            ble_device=device,
        )

        def scanner_lookup(
            _hass: HomeAssistant, _address: str, *, connectable: bool
        ) -> list[Any]:
            return [] if connectable else [scanner_device]

        with patch(
            f"{_TRANSPORT}.bluetooth.async_scanner_devices_by_address",
            side_effect=scanner_lookup,
        ):
            resolved = async_resolve_ble_device(hass, TEST_ADDRESS, "proxy")
        assert resolved is device

    async def test_generic_lookup_uses_the_non_connectable_view(
        self, hass: HomeAssistant
    ) -> None:
        device = SimpleNamespace(address=TEST_ADDRESS)
        lookup = MagicMock(side_effect=[None, device])
        with patch(
            f"{_TRANSPORT}.bluetooth.async_ble_device_from_address",
            lookup,
        ):
            resolved = async_resolve_ble_device(hass, TEST_ADDRESS)
        assert resolved is device
        assert [call.kwargs["connectable"] for call in lookup.call_args_list] == [
            True,
            False,
        ]


class TestActualPath:
    """After connecting, the real transport must be resolvable from its source."""

    async def test_source_resolves_to_a_classified_path(self, hass: HomeAssistant) -> None:
        with patch(
            f"{_TRANSPORT}.bluetooth.async_scanner_by_source",
            return_value=_scanner("proxy", scanner_type="remote", name="Hall proxy"),
        ):
            path = async_path_for_source(hass, "proxy", rssi=-61)
        assert path is not None
        assert path.transport is TransportClass.PROXY
        assert path.scanner_name == "Hall proxy"
        assert path.rssi == -61

    async def test_unknown_source_still_yields_a_path(self, hass: HomeAssistant) -> None:
        """A source HA no longer knows is reported, not dropped."""
        with patch(f"{_TRANSPORT}.bluetooth.async_scanner_by_source", return_value=None):
            path = async_path_for_source(hass, "gone")
        assert path is not None
        assert path.transport is TransportClass.UNKNOWN

    async def test_missing_source_is_none(self, hass: HomeAssistant) -> None:
        assert async_path_for_source(hass, None) is None
        assert async_path_for_source(hass, "unknown") is None


class TestBondOwnership:
    """Only a proven local path may be treated as owning a host bond."""

    @pytest.mark.parametrize(
        ("transport", "expected"),
        [
            (TransportClass.LOCAL, True),
            (TransportClass.PROXY, False),
            (TransportClass.UNKNOWN, False),
        ],
    )
    def test_owns_host_bond(self, transport: TransportClass, expected: bool) -> None:
        assert ConnectionPath(source="s", transport=transport).owns_host_bond is expected


class TestConnectionAvailability:
    """A path with no free connection slot is not a usable prediction."""

    def _busy_local(self, source: str, free: int, slots: int = 3) -> SimpleNamespace:
        scanner = _scanner(source, scanner_type="usb")
        scanner.get_allocations = lambda: SimpleNamespace(slots=slots, free=free)
        return scanner

    def _proxy(self, source: str, *, can_connect: bool) -> SimpleNamespace:
        scanner = _scanner(source, scanner_type="remote")
        scanner.connector = SimpleNamespace(can_connect=lambda: can_connect)
        return scanner

    async def test_a_full_local_adapter_is_skipped_for_a_free_proxy(
        self, hass: HomeAssistant
    ) -> None:
        devices = [
            _scanner_device(self._busy_local("hci0", free=0), -40),
            _scanner_device(self._proxy("proxy", can_connect=True), -80),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.chosen is not None
        assert prediction.chosen.source == "proxy"

    async def test_a_full_proxy_is_skipped_for_a_free_local_adapter(
        self, hass: HomeAssistant
    ) -> None:
        devices = [
            _scanner_device(self._proxy("proxy", can_connect=False), -40),
            _scanner_device(self._busy_local("hci0", free=2), -80),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.chosen is not None
        assert prediction.chosen.source == "hci0"

    async def test_a_full_preferred_adapter_is_skipped_for_a_free_path(
        self, hass: HomeAssistant
    ) -> None:
        """A preference cannot override HA's live connection-capacity gate."""
        devices = [
            _scanner_device(self._proxy("proxy", can_connect=False), -40),
            _scanner_device(self._busy_local("hci0", free=2), -80),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "proxy")
        assert prediction.preferred_available is False
        assert prediction.chosen is not None
        assert prediction.chosen.source == "hci0"

    async def test_all_paths_busy_still_predicts_the_best_one(
        self, hass: HomeAssistant
    ) -> None:
        """"Everything is full" is a truer answer than "nothing can see it"."""
        devices = [
            _scanner_device(self._busy_local("hci0", free=0), -40),
            _scanner_device(self._proxy("proxy", can_connect=False), -80),
        ]
        with _patch_scanner_devices(devices):
            prediction = async_predict_path(hass, TEST_ADDRESS, "auto")
        assert prediction.chosen is not None
        assert prediction.chosen.source == "hci0"
        assert not any(path.can_connect for path in prediction.paths)

    async def test_an_unknowable_slot_state_is_treated_as_available(
        self, hass: HomeAssistant
    ) -> None:
        """Hiding a usable path is worse than showing one that turns out busy."""
        devices = [_scanner_device(_scanner("hci0", scanner_type="usb"), -40)]
        with _patch_scanner_devices(devices):
            paths = async_connection_paths(hass, TEST_ADDRESS)
        assert paths[0].can_connect is True


class TestAdapterPickerLabels:
    """The adapter picker must not label a host adapter as a proxy."""

    async def test_local_adapter_with_a_mac_source_is_labelled_local(
        self, hass: HomeAssistant
    ) -> None:
        """The old test was `":" in source`, which every local MAC satisfies."""
        from custom_components.adjustable_bed.validators import get_available_adapters

        scanner = _scanner("AA:BB:CC:11:22:33", scanner_type="usb")
        scanner.name = scanner.source  # no friendlier name available
        with patch(
            "homeassistant.components.bluetooth.async_current_scanners",
            return_value=[scanner],
        ):
            adapters = get_available_adapters(hass)
        assert adapters["AA:BB:CC:11:22:33"] == "Local Adapter (AA:BB:CC:11:22:33)"

    async def test_remote_scanner_is_labelled_a_proxy(self, hass: HomeAssistant) -> None:
        from custom_components.adjustable_bed.validators import get_available_adapters

        scanner = _scanner("proxy-1", scanner_type="remote")
        scanner.name = scanner.source
        with patch(
            "homeassistant.components.bluetooth.async_current_scanners",
            return_value=[scanner],
        ):
            adapters = get_available_adapters(hass)
        assert adapters["proxy-1"] == "Bluetooth Proxy (proxy-1)"

    async def test_a_friendly_name_is_still_preferred(self, hass: HomeAssistant) -> None:
        from custom_components.adjustable_bed.validators import get_available_adapters

        scanner = _scanner("proxy-1", scanner_type="remote", name="Hall proxy")
        with patch(
            "homeassistant.components.bluetooth.async_current_scanners",
            return_value=[scanner],
        ):
            adapters = get_available_adapters(hass)
        assert adapters["proxy-1"] == "Hall proxy (proxy-1)"
