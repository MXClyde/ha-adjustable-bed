"""Tests for adjustable bed cover entities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.adjustable_bed.cover import (
    AdjustableBedCover,
    AdjustableBedCoverEntityDescription,
)


async def _noop(_controller) -> None:
    return None


async def test_cover_commands_use_physical_position_axis_resource() -> None:
    """Logical Keeson motor names must share their seek scheduler resource."""
    coordinator = SimpleNamespace(
        device_info={},
        entity_translation_key=lambda key: key,
        entity_unique_id=lambda key: key,
        name="Test bed",
        async_execute_controller_command=AsyncMock(),
    )
    cover = AdjustableBedCover(
        coordinator,
        AdjustableBedCoverEntityDescription(
            key="head",
            translation_key="head",
            open_fn=_noop,
            close_fn=_noop,
            stop_fn=_noop,
            position_key="back",
        ),
    )
    cover.async_write_ha_state = MagicMock()

    await cover.async_open_cover()
    await cover.async_close_cover()
    await cover.async_stop_cover()

    assert [
        call.kwargs["resource"]
        for call in coordinator.async_execute_controller_command.await_args_list
    ] == ["motor:back", "motor:back", "motor:back"]
