"""OKIN CSTProtocol bed controller implementation.

CSTProtocol uses a 14-byte command format with two separate 32-bit fields:
- Primary field (bytes 2-5): Motor control and several remote button actions
- Secondary field (bytes 6-9): Discrete light and massage-wave actions

Format: [0x0C, 0x02, motor[4], control[4], 0x00, 0x00, 0x00, 0x00]

Most command values are identical to existing OKIN UUID values, but the MFirm
app routes remote actions across both CST fields. Do not infer field placement
from the feature type alone.

Protocol reverse-engineered from com.okin.bedding.rizemf900 app (CSTProtocol.java).
Known devices: Rize MF900, other CSTProtocol-based Okin beds.

Uses standard OKIN service: 62741523-52f9-8864-b1ab-3b3a8d65950b
Requires BLE pairing before use (same as OkinUuidController).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from bleak.exc import BleakError

from ..const import OKIMAT_NOTIFY_CHAR_UUID, OKIMAT_WRITE_CHAR_UUID
from .base import BedController, MotorControlSpec
from .okin_protocol import build_cst_command

if TYPE_CHECKING:
    from ..coordinator import AdjustableBedCoordinator

_LOGGER = logging.getLogger(__name__)

_PRESET_REPEAT_COUNT = 6
_PRESET_REPEAT_DELAY_MS = 100
_BUTTON_PRESS_REPEAT_COUNT = 6
_BUTTON_PRESS_REPEAT_DELAY_MS = 100
_STOP_REPEAT_COUNT = 2
_STOP_REPEAT_DELAY_MS = 100


class CstMotorCommands:
    """Motor field command values (bytes 2-5)."""

    STOP = 0x00000000
    HEAD_UP = 0x00000001
    HEAD_DOWN = 0x00000002
    FOOT_UP = 0x00000004
    FOOT_DOWN = 0x00000008
    LUMBAR_UP = 0x00000010
    LUMBAR_DOWN = 0x00000020


class CstRemoteCommands:
    """Remote action command values.

    The CST app chooses the first or second 32-bit field per action. Call sites
    pass these values to build_cst_command() using the matching field.
    """

    STOP = 0x00000000
    FLAT = 0x08000000
    ZERO_G = 0x00001000
    LOUNGE = 0x00002000
    INCLINE = 0x00004000
    ANTI_SNORE = 0x00008000
    SAVE_ZERO_G = FLAT | ZERO_G
    SAVE_LOUNGE = FLAT | LOUNGE
    SAVE_INCLINE = FLAT | INCLINE
    LIGHT_TOGGLE = 0x00020000
    LIGHT_ON = 0x00000040
    LIGHT_OFF = 0x00000080
    MASSAGE_OFF = 0x02000000
    MASSAGE_INTENSITY = 0x00000C00
    MASSAGE_INTENSITY_MINUS = 0x01800000
    MASSAGE_WAVE_1 = 0x00080000
    MASSAGE_WAVE_2 = 0x00100000
    MASSAGE_WAVE_3 = 0x00200000


CstControlCommands = CstRemoteCommands


class OkinCstController(BedController):
    """Controller for OKIN CSTProtocol beds (Rize MF900, etc.).

    Uses 14-byte packets with separate motor and control fields.
    Requires BLE pairing before use.
    """

    def __init__(self, coordinator: AdjustableBedCoordinator) -> None:
        """Initialize the OKIN CST controller."""
        super().__init__(coordinator)
        self._motor_state: dict[str, int] = {}
        self._massage_wave_index = 0

        _LOGGER.debug("OkinCstController initialized")

    @property
    def control_characteristic_uuid(self) -> str:
        """Return the UUID of the control characteristic."""
        return OKIMAT_WRITE_CHAR_UUID

    # Capability properties

    @property
    def supports_preset_zero_g(self) -> bool:
        return True

    @property
    def supports_preset_anti_snore(self) -> bool:
        return True

    @property
    def supports_preset_lounge(self) -> bool:
        return True

    @property
    def supports_preset_incline(self) -> bool:
        return True

    @property
    def supports_memory_presets(self) -> bool:
        return True

    @property
    def memory_slot_count(self) -> int:
        return 3

    @property
    def supports_memory_programming(self) -> bool:
        return True

    @property
    def supports_lights(self) -> bool:
        return True

    @property
    def supports_discrete_light_control(self) -> bool:
        return True

    @property
    def supports_massage(self) -> bool:
        return True

    @property
    def has_lumbar_support(self) -> bool:
        return True

    @property
    def supports_stop_all(self) -> bool:
        return True

    @property
    def motor_control_specs(self) -> tuple[MotorControlSpec, ...]:
        """Expose the app's fixed three-axis head, foot, and lumbar layout."""
        return (
            MotorControlSpec(
                key="head",
                translation_key="head",
                open_fn=lambda ctrl: ctrl.move_head_up(),
                close_fn=lambda ctrl: ctrl.move_head_down(),
                stop_fn=lambda ctrl: ctrl.move_head_stop(),
            ),
            MotorControlSpec(
                key="feet",
                translation_key="feet",
                open_fn=lambda ctrl: ctrl.move_feet_up(),
                close_fn=lambda ctrl: ctrl.move_feet_down(),
                stop_fn=lambda ctrl: ctrl.move_feet_stop(),
                max_angle=45,
            ),
            MotorControlSpec(
                key="lumbar",
                translation_key="lumbar",
                open_fn=lambda ctrl: ctrl.move_lumbar_up(),
                close_fn=lambda ctrl: ctrl.move_lumbar_down(),
                stop_fn=lambda ctrl: ctrl.move_lumbar_stop(),
                max_angle=30,
            ),
        )

    @property
    def stale_motor_entity_keys(self) -> frozenset[str]:
        """Remove motor covers exposed by the former four-axis assumption."""
        return frozenset({"back", "legs", "tilt"})

    async def start_notify(
        self, callback: Callable[[str, float], None] | None = None
    ) -> None:
        """Subscribe to CST notifications for raw diagnostic capture."""
        self._notify_callback = callback
        client = self.client
        if client is None or not client.is_connected:
            _LOGGER.warning("Cannot start CST notifications: not connected")
            return

        try:
            async with self._ble_lock:
                await client.start_notify(
                    OKIMAT_NOTIFY_CHAR_UUID,
                    self._handle_notification,
                )
        except BleakError as err:
            _LOGGER.debug("Could not start CST notifications: %s", err)

    def _handle_notification(self, _: object, data: bytearray) -> None:
        """Forward CST notifications without interpreting them as positions."""
        self.forward_raw_notification(OKIMAT_NOTIFY_CHAR_UUID, bytes(data))

    async def stop_notify(self) -> None:
        """Stop the raw CST diagnostic notification subscription."""
        client = self.client
        if client is None or not client.is_connected:
            return

        try:
            async with self._ble_lock:
                await client.stop_notify(OKIMAT_NOTIFY_CHAR_UUID)
        except BleakError as err:
            _LOGGER.debug("Could not stop CST notifications: %s", err)

    # Motor movement helpers

    def _get_motor_command(self) -> int:
        """Calculate combined motor command from active motor states."""
        command = 0
        for value in self._motor_state.values():
            command |= value
        return command

    async def _move_motor(self, motor: str, command_value: int | None) -> None:
        """Move a motor or stop it."""
        if command_value is None or command_value == 0:
            self._motor_state.pop(motor, None)
        else:
            self._motor_state[motor] = command_value

        combined = self._get_motor_command()
        pulse_count, pulse_delay = self.motor_pulse_settings()

        try:
            if combined:
                await self.write_command(
                    build_cst_command(motor_value=combined),
                    repeat_count=pulse_count,
                    repeat_delay_ms=pulse_delay,
                )
        finally:
            self._motor_state.pop(motor, None)
            if not self._motor_state:
                await self._send_stop_sequence()

    async def _send_stop_sequence(self) -> None:
        """Send the app-style CST STOP sequence."""
        stop_event = asyncio.Event()
        for _ in range(_STOP_REPEAT_COUNT):
            await asyncio.sleep(_STOP_REPEAT_DELAY_MS / 1000)
            await self.write_command(build_cst_command(), cancel_event=stop_event)

    async def _send_repeated_command(
        self,
        *,
        motor_value: int = 0,
        control_value: int = 0,
        repeat_count: int,
        repeat_delay_ms: int,
    ) -> None:
        """Send a CST command with stop cleanup."""
        try:
            await self.write_command(
                build_cst_command(motor_value=motor_value, control_value=control_value),
                repeat_count=repeat_count,
                repeat_delay_ms=repeat_delay_ms,
            )
        finally:
            try:
                await self._send_stop_sequence()
            except (TimeoutError, BleakError, ConnectionError):
                _LOGGER.debug("Failed to send STOP during CST cleanup", exc_info=True)

    async def _send_preset(self, motor_value: int) -> None:
        """Send a long-running preset recall command."""
        await self._send_repeated_command(
            motor_value=motor_value,
            repeat_count=_PRESET_REPEAT_COUNT,
            repeat_delay_ms=_PRESET_REPEAT_DELAY_MS,
        )

    async def _send_button_press(
        self, *, motor_value: int = 0, control_value: int = 0
    ) -> None:
        """Send a short app-style button press."""
        await self._send_repeated_command(
            motor_value=motor_value,
            control_value=control_value,
            repeat_count=_BUTTON_PRESS_REPEAT_COUNT,
            repeat_delay_ms=_BUTTON_PRESS_REPEAT_DELAY_MS,
        )

    # Motor control - Back/Head (primary)

    async def move_head_up(self) -> None:
        """Move head/back up."""
        await self._move_motor("back", CstMotorCommands.HEAD_UP)

    async def move_head_down(self) -> None:
        """Move head/back down."""
        await self._move_motor("back", CstMotorCommands.HEAD_DOWN)

    async def move_head_stop(self) -> None:
        """Stop head/back motor."""
        await self._move_motor("back", None)

    async def move_back_up(self) -> None:
        """Move back up."""
        await self._move_motor("back", CstMotorCommands.HEAD_UP)

    async def move_back_down(self) -> None:
        """Move back down."""
        await self._move_motor("back", CstMotorCommands.HEAD_DOWN)

    async def move_back_stop(self) -> None:
        """Stop back motor."""
        await self._move_motor("back", None)

    # Motor control - Legs/Feet

    async def move_legs_up(self) -> None:
        """Move legs up."""
        await self._move_motor("legs", CstMotorCommands.FOOT_UP)

    async def move_legs_down(self) -> None:
        """Move legs down."""
        await self._move_motor("legs", CstMotorCommands.FOOT_DOWN)

    async def move_legs_stop(self) -> None:
        """Stop legs motor."""
        await self._move_motor("legs", None)

    async def move_feet_up(self) -> None:
        """Move feet up."""
        await self._move_motor("legs", CstMotorCommands.FOOT_UP)

    async def move_feet_down(self) -> None:
        """Move feet down."""
        await self._move_motor("legs", CstMotorCommands.FOOT_DOWN)

    async def move_feet_stop(self) -> None:
        """Stop feet motor."""
        await self._move_motor("legs", None)

    # Motor control - Lumbar

    async def move_lumbar_up(self) -> None:
        """Move lumbar up."""
        await self._move_motor("lumbar", CstMotorCommands.LUMBAR_UP)

    async def move_lumbar_down(self) -> None:
        """Move lumbar down."""
        await self._move_motor("lumbar", CstMotorCommands.LUMBAR_DOWN)

    async def move_lumbar_stop(self) -> None:
        """Stop lumbar motor."""
        await self._move_motor("lumbar", None)

    async def stop_all(self) -> None:
        """Stop all motors."""
        self._motor_state = {}
        await self._send_stop_sequence()

    # Presets

    async def preset_flat(self) -> None:
        """Go to flat position."""
        await self._send_preset(CstRemoteCommands.FLAT)

    async def preset_zero_g(self) -> None:
        """Go to zero gravity position."""
        await self._send_preset(CstRemoteCommands.ZERO_G)

    async def preset_anti_snore(self) -> None:
        """Go to anti-snore position."""
        await self._send_preset(CstRemoteCommands.ANTI_SNORE)

    async def preset_lounge(self) -> None:
        """Go to lounge position."""
        await self._send_preset(CstRemoteCommands.LOUNGE)

    async def preset_incline(self) -> None:
        """Go to incline/TV position."""
        await self._send_preset(CstRemoteCommands.INCLINE)

    async def preset_memory(self, memory_num: int) -> None:
        """Go to a user-programmable preset memory."""
        commands = {
            1: CstRemoteCommands.ZERO_G,
            2: CstRemoteCommands.INCLINE,
            3: CstRemoteCommands.LOUNGE,
        }
        if command := commands.get(memory_num):
            await self._send_preset(command)
        else:
            _LOGGER.warning("Invalid memory number %d (valid: 1-3)", memory_num)

    async def program_memory(self, memory_num: int) -> None:
        """Program the current position to a user-programmable preset memory."""
        commands = {
            1: CstRemoteCommands.SAVE_ZERO_G,
            2: CstRemoteCommands.SAVE_INCLINE,
            3: CstRemoteCommands.SAVE_LOUNGE,
        }
        if command := commands.get(memory_num):
            await self._send_button_press(motor_value=command)
        else:
            _LOGGER.warning("Invalid memory number %d (valid: 1-3)", memory_num)

    # Lights

    async def lights_on(self) -> None:
        """Turn on lights."""
        await self._send_button_press(control_value=CstRemoteCommands.LIGHT_ON)

    async def lights_off(self) -> None:
        """Turn off lights."""
        await self._send_button_press(control_value=CstRemoteCommands.LIGHT_OFF)

    async def lights_toggle(self) -> None:
        """Toggle lights."""
        await self._send_button_press(motor_value=CstRemoteCommands.LIGHT_TOGGLE)

    # Massage

    async def massage_off(self) -> None:
        """Turn massage off."""
        await self._send_button_press(motor_value=CstRemoteCommands.MASSAGE_OFF)

    async def massage_intensity_up(self) -> None:
        """Increase overall massage intensity."""
        await self._send_button_press(motor_value=CstRemoteCommands.MASSAGE_INTENSITY)

    async def massage_intensity_down(self) -> None:
        """Decrease overall massage intensity."""
        await self._send_button_press(
            motor_value=CstRemoteCommands.MASSAGE_INTENSITY_MINUS
        )

    async def massage_mode_step(self) -> None:
        """Step through massage wave modes."""
        commands = (
            CstRemoteCommands.MASSAGE_WAVE_1,
            CstRemoteCommands.MASSAGE_WAVE_2,
            CstRemoteCommands.MASSAGE_WAVE_3,
        )
        command = commands[self._massage_wave_index]
        self._massage_wave_index = (self._massage_wave_index + 1) % len(commands)
        await self._send_button_press(control_value=command)
