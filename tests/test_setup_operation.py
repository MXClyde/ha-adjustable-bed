"""Tests for the Bluetooth setup progress plumbing (issues #457, #460)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

from custom_components.adjustable_bed.bluetooth_transport import (
    ConnectionPath,
    PathPrediction,
    TransportClass,
)
from custom_components.adjustable_bed.setup_operation import (
    BluetoothOperationMixin,
    ConnectionLifetimePolicy,
    OperationOutcome,
    OperationResult,
    SetupAction,
)


class _Flow(BluetoothOperationMixin):
    """Minimal stand-in for a Home Assistant flow handler.

    Only the handful of methods the mixin actually calls are provided, so the
    tests exercise the mixin's own logic rather than the flow machinery.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.flow_id = "test-flow"
        self.shown: list[dict[str, Any]] = []
        self.progress_values: list[float] = []
        self.manager = MagicMock()
        self.manager.async_configure = _AsyncNoop()

    def _async_flow_manager(self) -> Any:
        return self.manager

    def async_update_progress(self, progress: float) -> None:
        self.progress_values.append(progress)

    def async_show_progress(self, **kwargs: Any) -> dict[str, Any]:
        result = {"type": FlowResultType.SHOW_PROGRESS, **kwargs}
        self.shown.append(result)
        return result

    def async_show_progress_done(self, *, next_step_id: str) -> dict[str, Any]:
        result = {"type": FlowResultType.SHOW_PROGRESS_DONE, "next_step_id": next_step_id}
        self.shown.append(result)
        return result


class _AsyncNoop:
    """An awaitable stand-in that records how often it was called."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_kwargs: Any) -> None:
        self.calls += 1


@pytest.fixture
def flow(hass: HomeAssistant) -> _Flow:
    return _Flow(hass)


async def _drain(hass: HomeAssistant) -> None:
    """Let every scheduled task settle."""
    await hass.async_block_till_done()


class TestTaskLifecycle:
    """One submission means one BLE operation, however often the step re-runs."""

    async def test_the_step_shows_progress_while_the_worker_runs(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        gate = asyncio.Event()

        async def worker() -> OperationResult:
            await gate.wait()
            return OperationResult(outcome=OperationOutcome.SUCCESS)

        flow.async_begin_operation(name="Bed", address="AA:BB")
        result = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == SetupAction.LOCATING.value
        gate.set()
        await _drain(hass)

    async def test_re_entering_reuses_the_same_task(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        """A double submit must not open a second BLE connection."""
        started = 0
        gate = asyncio.Event()

        async def worker() -> OperationResult:
            nonlocal started
            started += 1
            await gate.wait()
            return OperationResult(outcome=OperationOutcome.SUCCESS)

        flow.async_begin_operation()
        first = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        second = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert first["progress_task"] is second["progress_task"]
        gate.set()
        await _drain(hass)
        assert started == 1

    async def test_completion_routes_to_the_result_step(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        async def worker() -> OperationResult:
            return OperationResult(outcome=OperationOutcome.SUCCESS, detail="ok")

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)
        done = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert done["type"] == FlowResultType.SHOW_PROGRESS_DONE
        assert done["next_step_id"] == "setup_result"
        assert flow.operation.result is not None
        assert flow.operation.result.succeeded

    async def test_the_terminal_result_is_consumed_exactly_once(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        """HA's own done-callback and a phase refresh can both land here."""
        runs = 0

        async def worker() -> OperationResult:
            nonlocal runs
            runs += 1
            return OperationResult(outcome=OperationOutcome.SUCCESS)

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)

        first = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        second = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert first["type"] == FlowResultType.SHOW_PROGRESS_DONE
        assert second["type"] == FlowResultType.SHOW_PROGRESS_DONE
        # The second pass must not restart the operation.
        assert runs == 1

    async def test_a_raising_worker_becomes_a_failure_result(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        async def worker() -> OperationResult:
            raise RuntimeError("boom")

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)
        done = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert done["type"] == FlowResultType.SHOW_PROGRESS_DONE
        assert flow.operation.result is not None
        assert flow.operation.result.outcome is OperationOutcome.CONNECTION_FAILED
        assert flow.operation.result.detail == "boom"

    async def test_a_cancelled_worker_reports_cancelled(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        async def worker() -> OperationResult:
            raise asyncio.CancelledError

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert flow.operation.result is not None
        assert flow.operation.result.outcome is OperationOutcome.CANCELLED


class TestProgressReporting:
    """Phase text re-renders the step; the numeric bar does not."""

    async def test_a_phase_change_refreshes_the_view(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        flow.async_begin_operation()
        flow.async_report_action(SetupAction.CONNECTING)
        await _drain(hass)
        assert flow.operation.action is SetupAction.CONNECTING
        assert flow.manager.async_configure.calls == 1

    async def test_an_unchanged_phase_does_not_refresh(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        flow.async_begin_operation(action=SetupAction.CONNECTING)
        flow.async_report_action(SetupAction.CONNECTING)
        await _drain(hass)
        assert flow.manager.async_configure.calls == 0

    async def test_the_numeric_bar_does_not_re_render_the_step(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        """Only the advertisement wait is determinate, and it must stay cheap."""
        flow.async_begin_operation()
        flow.async_report_progress(0.25)
        flow.async_report_progress(0.5)
        await _drain(hass)
        assert flow.progress_values == [0.25, 0.5]
        assert flow.manager.async_configure.calls == 0

    async def test_refreshes_never_overlap_and_are_coalesced(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        """Concurrent re-configures would race each other and HA's own callback.

        Three phase changes while one refresh is still in flight must collapse
        into that one plus a single follow-up carrying the latest phase - never
        three overlapping invocations.
        """
        release = asyncio.Event()
        calls = 0
        concurrent = 0
        peak = 0

        async def _slow_configure(**_kwargs: Any) -> None:
            nonlocal calls, concurrent, peak
            calls += 1
            concurrent += 1
            peak = max(peak, concurrent)
            try:
                await release.wait()
            finally:
                concurrent -= 1

        flow.manager.async_configure = _slow_configure
        flow.async_begin_operation()

        flow.async_report_action(SetupAction.CONNECTING)
        await asyncio.sleep(0)
        flow.async_report_action(SetupAction.DISCOVERING_SERVICES)
        flow.async_report_action(SetupAction.READING_CAPABILITIES)
        await asyncio.sleep(0)

        assert calls == 1
        release.set()
        await _drain(hass)

        assert peak == 1
        assert calls == 2
        # The follow-up carries the newest phase, so nothing is rendered stale.
        assert flow.operation.action is SetupAction.READING_CAPABILITIES

    async def test_a_closed_dialog_does_not_raise(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        async def _gone(**_kwargs: Any) -> None:
            raise UnknownFlow

        flow.manager.async_configure = _gone
        flow.async_begin_operation()
        flow.async_report_action(SetupAction.CONNECTING)
        await _drain(hass)

    async def test_reporting_the_actual_path_refreshes_the_view(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        flow.async_begin_operation(
            prediction=PathPrediction(
                chosen=ConnectionPath(source="hci0", transport=TransportClass.LOCAL),
                paths=(),
            )
        )
        flow.async_report_path(
            ConnectionPath(source="proxy", transport=TransportClass.PROXY)
        )
        await _drain(hass)
        assert flow.operation.path_changed is True
        assert flow.operation.transport is TransportClass.PROXY


class TestCleanup:
    """Abandoning a flow must not leave a connection or a task behind."""

    async def test_the_client_is_disconnected_when_the_worker_finishes(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        client = MagicMock()
        client.disconnect = _AsyncNoop()

        async def worker() -> OperationResult:
            flow.async_track_client(client)
            return OperationResult(outcome=OperationOutcome.SUCCESS)

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)
        assert client.disconnect.calls == 1

    async def test_the_client_is_disconnected_when_the_worker_fails(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        client = MagicMock()
        client.disconnect = _AsyncNoop()

        async def worker() -> OperationResult:
            flow.async_track_client(client)
            raise RuntimeError("boom")

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)
        assert client.disconnect.calls == 1

    async def test_removing_the_flow_cancels_the_task_and_the_refresh(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        gate = asyncio.Event()

        async def worker() -> OperationResult:
            await gate.wait()
            return OperationResult(outcome=OperationOutcome.SUCCESS)

        flow.async_begin_operation()
        result = await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        task = result["progress_task"]
        flow.async_report_action(SetupAction.CONNECTING)

        flow.async_remove()
        await _drain(hass)
        assert task.cancelled() or task.done()
        assert flow._refresh_task is None

    async def test_removing_the_flow_disconnects_a_stray_client(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        client = MagicMock()
        client.disconnect = _AsyncNoop()
        flow.async_begin_operation()
        flow.async_track_client(client)

        flow.async_remove()
        await _drain(hass)
        assert client.disconnect.calls == 1


class TestOperationState:
    """The shared state is what both the progress view and the result step read."""

    async def test_a_retry_clears_the_previous_terminal_state(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        async def worker() -> OperationResult:
            return OperationResult(outcome=OperationOutcome.NOT_ADVERTISING)

        flow.async_begin_operation()
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        await _drain(hass)
        await flow.async_run_operation_step(
            step_id="setup_progress", worker=worker, next_step_id="setup_result"
        )
        assert flow.operation.result is not None

        flow.async_begin_operation()
        assert flow.operation.result is None
        assert flow.operation.terminal_consumed is False

    async def test_the_lifetime_policy_is_carried_on_the_state(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        """Gen2 beds must be recognisable before anything opens a connection."""
        state = flow.async_begin_operation(
            policy=ConnectionLifetimePolicy.KEEP_FIRST_LINK
        )
        assert state.policy is ConnectionLifetimePolicy.KEEP_FIRST_LINK

    async def test_predicted_path_survives_until_the_real_one_is_known(
        self, hass: HomeAssistant, flow: _Flow
    ) -> None:
        predicted = ConnectionPath(source="hci0", transport=TransportClass.LOCAL)
        flow.async_begin_operation(
            prediction=PathPrediction(chosen=predicted, paths=(predicted,))
        )
        assert flow.operation.effective_path is predicted
        assert flow.operation.path_changed is False
