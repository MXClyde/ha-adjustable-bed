"""Leggett & Platt Okin variant bed controller implementation.

Reverse engineering by MarcusW and Richard Hopton (smartbed-mqtt).

This controller handles Leggett & Platt beds using the Okin binary protocol.

Protocol details:
    Service UUID: 62741523-52f9-8864-b1ab-3b3a8d65950b (shared with Okimat/Nectar)
    Write characteristic: 62741525-52f9-8864-b1ab-3b3a8d65950b
    Command format: 6-byte binary [0x04, 0x02, <4-byte-command-big-endian>]
    Motor timing: held keycodes stream every 100ms, released with four zero frames
    Position feedback: Not supported
    Pairing: Required before first use; handled by coordinator

Note: This shares the same BLE service UUID with Okimat and Nectar beds.
Detection uses device name patterns ("leggett", "l&p", "lp bed") to distinguish
between these bed types. See okin_protocol.py for the shared binary protocol
specification.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import Enum
from typing import TYPE_CHECKING

from bleak.exc import BleakError

from ..const import LEGGETT_OKIN_CHAR_UUID, LEGGETT_OKIN_PULSE_DEFAULTS
from .base import BedController
from .okin_protocol import build_okin_command

if TYPE_CHECKING:
    from ..coordinator import AdjustableBedCoordinator

_LOGGER = logging.getLogger(__name__)


class LeggettOkinCommands:
    """Leggett & Platt Okin keycode constants (32-bit values).

    Semantics come from the layout bindings in com.leggett.prodigy4 1.2.0, not
    from the app's own ``FBP_KEYCODE_*`` identifiers: several of those names are
    demonstrably wrong for this hardware (0x800000 is declared
    LIGHT_INTENSITY_DOWN but is bound to the head massage-down button). See
    docs/beds/leggett-okin.md.
    """

    # Presets. FLAT is a held button rather than a one-shot recall; the memory
    # slots and SNORE are one-shot recalls the control box completes on its own.
    PRESET_FLAT = 0x8000000
    PRESET_ZERO_G = 0x1000  # Deliberate alias of memory 1
    PRESET_MEMORY_1 = 0x1000
    PRESET_MEMORY_2 = 0x2000
    PRESET_MEMORY_3 = 0x4000  # Ships pre-assigned as the snore position
    PRESET_MEMORY_4 = 0x8000
    PRESET_ANTI_SNORE = 0x4000  # Deliberate alias of memory 3
    # Arms the control box to overwrite the next recalled slot. This is NOT a
    # recall: sending it alone and then a slot code reprograms that slot.
    MEMORY_STORE = 0x10000

    # Motor controls
    MOTOR_HEAD_UP = 0x1
    MOTOR_HEAD_DOWN = 0x2
    MOTOR_FEET_UP = 0x4
    MOTOR_FEET_DOWN = 0x8
    MOTOR_TILT_UP = 0x10
    MOTOR_TILT_DOWN = 0x20
    MOTOR_LUMBAR_UP = 0x40
    MOTOR_LUMBAR_DOWN = 0x80

    # Massage
    MASSAGE_HEAD_UP = 0x800
    MASSAGE_HEAD_DOWN = 0x800000
    MASSAGE_FOOT_UP = 0x400
    MASSAGE_FOOT_DOWN = 0x1000000
    MASSAGE_STEP = 0x100
    MASSAGE_WAVE_STEP = 0x10000000

    # Lights
    TOGGLE_LIGHTS = 0x20000


# The app streams a held keycode until release, then emits exactly four
# keycode-0 frames (OutputThread.runNormal, MaxZeroCount = 3). There is no
# distinct stop opcode: the release frame is an ordinary frame carrying 0.
RELEASE_FRAME_COUNT = 4
# Same 100ms as the recall cadence today, but deliberately a separate constant:
# these are independent findings about different command families, and retuning
# one must not silently retune the other.
RELEASE_FRAME_DELAY_MS = 100

# A memory recall is a fixed 10-frame burst with no terminator at all. The
# control box drives the move to completion by itself, so appending a release
# frame here could cancel the motion the recall just started.
RECALL_FRAME_COUNT = 10
RECALL_FRAME_DELAY_MS = 100

# Programming a slot is a two-stage hold, not an opcode: arm with MEMORY_STORE
# for ~5s, release, then hold the slot keycode for ~2s.
MEMORY_STORE_HOLD_S = 5.0
MEMORY_SLOT_HOLD_S = 2.0
MEMORY_PROGRAM_FRAME_DELAY_MS = 100

# FLAT is a held button with no app-defined duration - the user holds it until
# the bed is down. This is how long the integration streams it for; it is a
# usability choice, not a protocol constant.
FLAT_HOLD_S = 30.0


class MotorDirection(Enum):
    """Direction for motor movement."""

    UP = "up"
    DOWN = "down"
    STOP = "stop"


class LeggettOkinController(BedController):
    """Controller for Leggett & Platt beds using Okin protocol.

    These beds use the binary Okin protocol and require BLE pairing.
    They support motor control, presets, massage, and under-bed lighting.
    """

    def __init__(self, coordinator: AdjustableBedCoordinator) -> None:
        """Initialize the Leggett & Platt Okin controller."""
        super().__init__(coordinator)
        self._motor_state: dict[str, MotorDirection] = {}
        _LOGGER.debug("LeggettOkinController initialized")

    @property
    def control_characteristic_uuid(self) -> str:
        """Return the UUID of the control characteristic."""
        return LEGGETT_OKIN_CHAR_UUID

    # Capability properties
    @property
    def supports_preset_zero_g(self) -> bool:
        return True

    @property
    def supports_preset_anti_snore(self) -> bool:
        """Return True - 0x4000 is bound to the app's dedicated snore button."""
        return True

    @property
    def supports_lights(self) -> bool:
        """Return True - Okin beds support under-bed lighting."""
        return True

    @property
    def supports_discrete_light_control(self) -> bool:
        """Return False - Okin only supports toggle, not discrete on/off."""
        return False

    @property
    def supports_memory_presets(self) -> bool:
        """Return True - Okin beds support memory presets 1-4."""
        return True

    @property
    def memory_slot_count(self) -> int:
        """Return 4 - Okin beds support memory slots 1-4."""
        return 4

    @property
    def supports_memory_programming(self) -> bool:
        """Return True - slots are programmed by the two-stage store hold."""
        return True

    @property
    def has_tilt_support(self) -> bool:
        """Return True - Okin beds have tilt (pillow) motor control."""
        return True

    @property
    def has_lumbar_support(self) -> bool:
        """Return True - Okin beds have lumbar motor control."""
        return True

    def _build_command(self, command_value: int) -> bytes:
        """Build Okin binary command by delegating to build_okin_command.

        Args:
            command_value: 32-bit command value (0 to 0xFFFFFFFF)

        Returns:
            6-byte command: [0x04, 0x02, <4-byte-command-big-endian>]
        """
        return build_okin_command(command_value)

    def _get_move_command(self) -> int:
        """Calculate the combined motor movement command."""
        command = 0
        state = self._motor_state
        if state.get("head") == MotorDirection.UP:
            command += LeggettOkinCommands.MOTOR_HEAD_UP
        elif state.get("head") == MotorDirection.DOWN:
            command += LeggettOkinCommands.MOTOR_HEAD_DOWN
        if state.get("feet") == MotorDirection.UP:
            command += LeggettOkinCommands.MOTOR_FEET_UP
        elif state.get("feet") == MotorDirection.DOWN:
            command += LeggettOkinCommands.MOTOR_FEET_DOWN
        if state.get("tilt") == MotorDirection.UP:
            command += LeggettOkinCommands.MOTOR_TILT_UP
        elif state.get("tilt") == MotorDirection.DOWN:
            command += LeggettOkinCommands.MOTOR_TILT_DOWN
        if state.get("lumbar") == MotorDirection.UP:
            command += LeggettOkinCommands.MOTOR_LUMBAR_UP
        elif state.get("lumbar") == MotorDirection.DOWN:
            command += LeggettOkinCommands.MOTOR_LUMBAR_DOWN
        return command

    async def _move_motor(self, motor: str, direction: MotorDirection) -> None:
        """Move a motor in a direction or stop it."""
        if direction == MotorDirection.STOP:
            self._motor_state.pop(motor, None)
        else:
            self._motor_state[motor] = direction
        command = self._get_move_command()

        completed = False
        try:
            if command:
                pulse_count, pulse_delay_ms = self.motor_pulse_settings()
                await self.write_command(
                    self._build_command(command),
                    repeat_count=pulse_count,
                    repeat_delay_ms=pulse_delay_ms,
                )
            completed = True
        finally:
            self._motor_state = {}
            # The release burst is this protocol's stop. If the movement itself
            # succeeded, losing the release can leave the bed running, so it has
            # to surface. If we are already unwinding it is cleanup and must not
            # mask the original error.
            await self._send_release_frames("motor movement", raise_on_error=completed)

    async def _send_release_frames(self, context: str, *, raise_on_error: bool = False) -> None:
        """Send the release burst that ends a held keycode.

        The app emits exactly four keycode-0 frames when a button is released,
        so mirror that rather than a single frame. The burst gets a fresh cancel
        event so a stop request cannot suppress the release itself.

        ``raise_on_error`` is for callers where the burst *is* the operation, so
        a failure must reach the user. Cleanup callers leave it False: they are
        already unwinding and have their own error to report.
        """
        release = asyncio.ensure_future(
            self.write_command(
                self._build_command(0),
                repeat_count=RELEASE_FRAME_COUNT,
                repeat_delay_ms=RELEASE_FRAME_DELAY_MS,
                cancel_event=asyncio.Event(),
            )
        )
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            # Returning here would hand the command lock back mid-burst: the
            # coordinator would start the replacement command while the shielded
            # task was still emitting zero frames, and those frames would stop
            # the movement it had just started. The burst is bounded (~300ms),
            # so wait it out before propagating.
            while not release.done():
                with contextlib.suppress(asyncio.CancelledError, BleakError, ConnectionError):
                    await asyncio.shield(release)
            raise
        except (BleakError, ConnectionError):
            # The release burst is this protocol's only stop, so a failure can
            # leave the bed still moving. That is worth surfacing, not hiding.
            _LOGGER.warning(
                "Failed to send release frames after %s; the bed may still be moving",
                context,
                exc_info=True,
            )
            if raise_on_error:
                raise

    # Motor control methods
    async def move_head_up(self) -> None:
        """Move head up."""
        await self._move_motor("head", MotorDirection.UP)

    async def move_head_down(self) -> None:
        """Move head down."""
        await self._move_motor("head", MotorDirection.DOWN)

    async def move_head_stop(self) -> None:
        """Stop head motor."""
        await self._move_motor("head", MotorDirection.STOP)

    async def move_back_up(self) -> None:
        """Move back up (same as head)."""
        await self.move_head_up()

    async def move_back_down(self) -> None:
        """Move back down (same as head)."""
        await self.move_head_down()

    async def move_back_stop(self) -> None:
        """Stop back motor."""
        await self.move_head_stop()

    async def move_legs_up(self) -> None:
        """Move legs up."""
        await self._move_motor("feet", MotorDirection.UP)

    async def move_legs_down(self) -> None:
        """Move legs down."""
        await self._move_motor("feet", MotorDirection.DOWN)

    async def move_legs_stop(self) -> None:
        """Stop legs motor."""
        await self._move_motor("feet", MotorDirection.STOP)

    async def move_feet_up(self) -> None:
        """Move feet up."""
        await self.move_legs_up()

    async def move_feet_down(self) -> None:
        """Move feet down."""
        await self.move_legs_down()

    async def move_feet_stop(self) -> None:
        """Stop feet motor."""
        await self.move_legs_stop()

    async def stop_all(self) -> None:
        """Stop all motors by sending the release burst.

        An explicit stop must not report success when it never reached the bed,
        so failures propagate here rather than being logged and swallowed.
        """
        self._motor_state = {}
        await self._send_release_frames("stop_all", raise_on_error=True)

    # Preset methods
    _MEMORY_SLOTS = {
        1: LeggettOkinCommands.PRESET_MEMORY_1,
        2: LeggettOkinCommands.PRESET_MEMORY_2,
        3: LeggettOkinCommands.PRESET_MEMORY_3,
        4: LeggettOkinCommands.PRESET_MEMORY_4,
    }

    async def _recall(self, command: int) -> None:
        """Send a one-shot recall burst.

        Recall is 10 frames at 100ms and then silence: the control box drives
        the move to completion on its own. This is the one command family the
        app deliberately leaves unterminated, so no release frames follow -
        they could cancel the motion the recall just started.
        """
        await self.write_command(
            self._build_command(command),
            repeat_count=RECALL_FRAME_COUNT,
            repeat_delay_ms=RECALL_FRAME_DELAY_MS,
        )

    async def preset_flat(self) -> None:
        """Go to flat position.

        Unlike the memory slots, FLAT is a held button rather than an
        autonomous recall: the bed moves only while frames keep arriving, so
        this streams for roughly the time a full recline takes and then
        releases.
        """
        # The setup flows accept any integer for the pulse delay, and this hold
        # is a fixed duration, so a small or nonpositive value would expand it
        # into tens of thousands of sequential writes and flood the proxy (a
        # stored 0 would divide by zero outright). Streaming faster than the
        # protocol's proven cadence buys nothing here, so floor it at that.
        _, pulse_delay_ms = self.motor_pulse_settings()
        pulse_delay_ms = max(pulse_delay_ms, LEGGETT_OKIN_PULSE_DEFAULTS[1])
        repeat_count = max(1, round(FLAT_HOLD_S * 1000 / pulse_delay_ms))
        completed = False
        try:
            await self.write_command(
                self._build_command(LeggettOkinCommands.PRESET_FLAT),
                repeat_count=repeat_count,
                repeat_delay_ms=pulse_delay_ms,
            )
            completed = True
        finally:
            await self._send_release_frames("preset_flat", raise_on_error=completed)

    async def preset_memory(self, memory_num: int) -> None:
        """Go to memory preset."""
        command = self._MEMORY_SLOTS.get(memory_num)
        if command is None:
            _LOGGER.warning("Invalid memory slot for recall: %d", memory_num)
            return
        await self._recall(command)

    async def program_memory(self, memory_num: int) -> None:
        """Store the current position into a memory slot.

        There is no program opcode. The box is armed by holding MEMORY_STORE
        for ~5s, then records whichever slot keycode is held for the following
        ~2s. Both stages are ordinary held keycodes, so each ends with the
        normal release burst.
        """
        command = self._MEMORY_SLOTS.get(memory_num)
        if command is None:
            _LOGGER.warning("Invalid memory slot for programming: %d", memory_num)
            return

        _LOGGER.debug("Arming memory store for slot %d", memory_num)
        completed = False
        try:
            await self._hold_keycode(LeggettOkinCommands.MEMORY_STORE, MEMORY_STORE_HOLD_S)
            # This release is a stage boundary, not cleanup: without the zero
            # frames the box never leaves the arm stage, so continuing to the
            # slot hold would run an invalid sequence and still report success.
            await self._send_release_frames("memory store arm", raise_on_error=True)
            await self._hold_keycode(command, MEMORY_SLOT_HOLD_S)
            completed = True
        finally:
            # On the success path this release ends the sequence, so a failure
            # means the slot keycode may still be asserted and must surface. If
            # we are already unwinding it is cleanup, and must not mask the
            # exception that got us here.
            await self._send_release_frames("memory store slot", raise_on_error=completed)

    async def _hold_keycode(self, command: int, hold_seconds: float) -> None:
        """Stream a keycode for a fixed duration, as a held button would."""
        repeat_count = max(1, round(hold_seconds * 1000 / MEMORY_PROGRAM_FRAME_DELAY_MS))
        await self.write_command(
            self._build_command(command),
            repeat_count=repeat_count,
            repeat_delay_ms=MEMORY_PROGRAM_FRAME_DELAY_MS,
        )

    async def preset_zero_g(self) -> None:
        """Go to zero gravity position (memory slot 1 on this protocol)."""
        await self._recall(LeggettOkinCommands.PRESET_ZERO_G)

    async def preset_anti_snore(self) -> None:
        """Go to anti-snore position (memory slot 3 on this protocol)."""
        await self._recall(LeggettOkinCommands.PRESET_ANTI_SNORE)

    async def _tap_keycode(self, command: int, context: str) -> None:
        """Send a keycode as a short press, then release it.

        Lights and massage are ordinary held keycodes in the app, not one-shot
        recalls: a tap is one frame followed by the zero burst. Sending the
        frame alone can leave the key asserted, so the next press of the same
        control may not register.
        """
        completed = False
        try:
            await self.write_command(self._build_command(command))
            completed = True
        finally:
            await self._send_release_frames(context, raise_on_error=completed)

    # Light methods
    async def lights_toggle(self) -> None:
        """Toggle lights."""
        await self._tap_keycode(LeggettOkinCommands.TOGGLE_LIGHTS, "lights_toggle")

    async def lights_on(self) -> None:
        """Turn on lights (via toggle - no discrete control)."""
        await self.lights_toggle()

    async def lights_off(self) -> None:
        """Turn off lights (via toggle - no discrete control)."""
        await self.lights_toggle()

    # Massage methods
    #
    # There is deliberately no ``massage_off`` override: massage power is a
    # single toggle keycode with no discrete off. ``supports_massage_off_control``
    # detects the capability by checking whether the subclass overrides
    # ``massage_off``, so overriding it just to raise NotImplementedError would
    # advertise a massage-off button that can only ever fail (issue #368).
    async def massage_head_up(self) -> None:
        """Increase head massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_HEAD_UP, "massage_head_up")

    async def massage_head_down(self) -> None:
        """Decrease head massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_HEAD_DOWN, "massage_head_down")

    async def massage_foot_up(self) -> None:
        """Increase foot massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_FOOT_UP, "massage_foot_up")

    async def massage_foot_down(self) -> None:
        """Decrease foot massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_FOOT_DOWN, "massage_foot_down")

    async def massage_toggle(self) -> None:
        """Toggle massage / step through modes."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_STEP, "massage_toggle")

    async def massage_wave_step(self) -> None:
        """Step through massage wave patterns."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_WAVE_STEP, "massage_wave_step")

    # Tilt motor control
    async def move_tilt_up(self) -> None:
        """Move tilt (pillow) motor up."""
        await self._move_motor("tilt", MotorDirection.UP)

    async def move_tilt_down(self) -> None:
        """Move tilt (pillow) motor down."""
        await self._move_motor("tilt", MotorDirection.DOWN)

    async def move_tilt_stop(self) -> None:
        """Stop tilt motor."""
        await self._move_motor("tilt", MotorDirection.STOP)

    # Lumbar motor control
    async def move_lumbar_up(self) -> None:
        """Move lumbar motor up."""
        await self._move_motor("lumbar", MotorDirection.UP)

    async def move_lumbar_down(self) -> None:
        """Move lumbar motor down."""
        await self._move_motor("lumbar", MotorDirection.DOWN)

    async def move_lumbar_stop(self) -> None:
        """Stop lumbar motor."""
        await self._move_motor("lumbar", MotorDirection.STOP)
