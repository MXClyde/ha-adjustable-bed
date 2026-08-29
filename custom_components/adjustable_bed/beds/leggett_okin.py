"""Leggett & Platt Okin variant bed controller implementation.

Reverse engineering by MarcusW and Richard Hopton (smartbed-mqtt).

This controller handles Leggett & Platt beds using the Okin binary protocol.

Protocol details:
    Service UUID: 62741523-52f9-8864-b1ab-3b3a8d65950b (shared with Okimat/Nectar)
    Write characteristic: 62741525-52f9-8864-b1ab-3b3a8d65950b
    Command format: runtime-selected 6-byte R1 or checksummed 8-byte R0 frame
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
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError

from ..const import (
    LEGGETT_OKIN_CHAR_UUID,
    LEGGETT_OKIN_NOTIFY_CHAR_UUID,
    LEGGETT_OKIN_PULSE_DEFAULTS,
    LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID,
    LEGGETT_OKIN_SERVICE_UUID,
    OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID,
    OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID,
)
from .base import BedController, MotorControlSpec
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

    # Persistent handset-control mode settings
    CONTROL_MODE_PRESS_AND_HOLD = 0x08010000
    CONTROL_MODE_PRESS_AND_RELEASE = 0x01800000


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

# The settings dialog schedules 55 attempts at the normal cadence and then one
# explicit zero frame. This is a distinct lifecycle from an ordinary held key,
# whose release is four zero frames.
CONTROL_MODE_FRAME_COUNT = 55
CONTROL_MODE_FRAME_DELAY_MS = 100


def _build_revision_0_command(command_value: int) -> bytes:
    """Build the APK's revision-0 E5 FE 16 command frame."""
    keycode = build_okin_command(command_value)[2:]
    frame = b"\xe5\xfe\x16" + keycode
    return frame + bytes(((~sum(frame)) & 0xFF,))


def parse_leggett_okin_feedback(data: bytes) -> tuple[int, int] | None:
    """Reconstruct the Prodigy CE LED/status pair from a notification."""
    if len(data) < 4:
        return None

    size = data[0] & 0x0F
    if len(data) < size + 3:
        return None

    led_mask = 0
    for index in range(min(max(size - 2, 0), 4)):
        led_mask = (led_mask << 8) | data[index + 2]

    status = data[6] if size > 5 else -1
    if status >= 0x80:
        status -= 0x100

    def read_unsigned(offset: int, count: int) -> int:
        value = 0
        for index in range(count):
            value = (value << 8) | data[offset + index]
        return value

    opcode = data[2]
    if opcode == 6:
        led_mask |= read_unsigned(3, size)
    elif opcode in (7, 9):
        led_mask &= ~read_unsigned(3, size)
    elif opcode == 8 and size != 6:
        led_mask ^= read_unsigned(3, size)
    elif opcode == 11:
        half = size >> 1
        offset = 3 + (size & 1)
        led_mask = (
            read_unsigned(offset + half, half)
            & read_unsigned(offset, half)
            & ~0x200
        ) | (led_mask & 0x200)

    return led_mask & 0xFFFFFFFF, status


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

    # CU170 hardware accepts unconfirmed writes. Waiting for a response and then
    # sleeping the configured interval pushes the stream beyond its 217-218 ms
    # motion watchdog on ESPHome proxies.
    _write_with_response = False

    def __init__(self, coordinator: AdjustableBedCoordinator) -> None:
        """Initialize the Leggett & Platt Okin controller."""
        super().__init__(coordinator)
        self._motor_state: dict[str, MotorDirection] = {}
        self._protocol_revision = self._detect_protocol_revision()
        self._notification_led_mask: int | None = None
        self._notification_status: int | None = None
        self._notify_started: set[str] = set()
        self._settings_initialized = False
        _LOGGER.debug(
            "LeggettOkinController initialized (protocol revision: %s)",
            self._protocol_revision if self._protocol_revision is not None else "unknown",
        )

    @property
    def control_characteristic_uuid(self) -> str:
        """Return the UUID of the control characteristic."""
        return LEGGETT_OKIN_CHAR_UUID

    # Capability properties
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
    def memory_slot_names(self) -> tuple[str | None, ...]:
        """Return the three editable favorites and fixed Snore entry."""
        return ("Favorite 1", "Favorite 2", "Snore", "Favorite 3")

    def is_memory_slot_programmable(self, memory_num: int) -> bool:
        """Return False for the APK's fixed Snore entry in slot 3."""
        return memory_num in (1, 2, 4)

    @property
    def requires_notification_channel(self) -> bool:
        """Subscribe to the app's status channels even without position sensing."""
        return True

    @property
    def protocol_diagnostics(self) -> dict[str, Any]:
        """Report resolved framing and the APK-parsed opaque status mask."""
        led_mask = self._notification_led_mask
        return {
            "protocol_revision": self._protocol_revision,
            "revision_selector_present": (
                self._protocol_revision == 1 if self._protocol_revision is not None else None
            ),
            "notification_led_mask": f"0x{led_mask:08x}" if led_mask is not None else None,
            "notification_status": self._notification_status,
            "alarm_armed": bool(led_mask & 0x4000) if led_mask is not None else None,
            "sleep_timer_armed": bool(led_mask & 0x8000) if led_mask is not None else None,
            "notification_characteristics": sorted(self._notify_started),
            "settings_initialized": self._settings_initialized,
        }

    @property
    def has_pillow_support(self) -> bool:
        """Return True - the third actuator moves the pillow platform."""
        return True

    @property
    def has_lumbar_support(self) -> bool:
        """Return True - Okin beds have lumbar motor control."""
        return True

    @property
    def motor_control_specs(self) -> tuple[MotorControlSpec, ...]:
        """Expose the CU170's four physical actuators without aliases."""
        return (
            MotorControlSpec(
                key="head",
                translation_key="head",
                open_fn=lambda ctrl: ctrl.move_head_up(),
                close_fn=lambda ctrl: ctrl.move_head_down(),
                stop_fn=lambda ctrl: ctrl.move_head_stop(),
            ),
            MotorControlSpec(
                key="lumbar",
                translation_key="lumbar",
                open_fn=lambda ctrl: ctrl.move_lumbar_up(),
                close_fn=lambda ctrl: ctrl.move_lumbar_down(),
                stop_fn=lambda ctrl: ctrl.move_lumbar_stop(),
            ),
            MotorControlSpec(
                key="pillow",
                translation_key="pillow",
                open_fn=lambda ctrl: ctrl.move_pillow_up(),
                close_fn=lambda ctrl: ctrl.move_pillow_down(),
                stop_fn=lambda ctrl: ctrl.move_pillow_stop(),
            ),
            MotorControlSpec(
                key="feet",
                translation_key="feet",
                open_fn=lambda ctrl: ctrl.move_feet_up(),
                close_fn=lambda ctrl: ctrl.move_feet_down(),
                stop_fn=lambda ctrl: ctrl.move_feet_stop(),
            ),
        )

    @property
    def stale_motor_entity_keys(self) -> frozenset[str]:
        """Remove duplicate aliases and the former tilt label."""
        return frozenset({"back", "legs", "tilt"})

    def _available_characteristic_uuids(self) -> frozenset[str] | None:
        """Return discovered characteristic UUIDs, or None before discovery."""
        client = self.client
        services = getattr(client, "services", None) if client is not None else None
        if services is None:
            return None

        try:
            return frozenset(
                str(characteristic.uuid).lower()
                for service in services
                for characteristic in getattr(service, "characteristics", ())
            )
        except TypeError:
            return None

    def _detect_protocol_revision(self) -> int | None:
        """Select R1 only when the APK's selector characteristic is present."""
        client = self.client
        services = getattr(client, "services", None) if client is not None else None
        if services is None:
            return None

        try:
            service = next(
                (
                    service
                    for service in services
                    if str(getattr(service, "uuid", "")).lower()
                    == LEGGETT_OKIN_SERVICE_UUID.lower()
                ),
                None,
            )
        except TypeError:
            return None
        if service is None:
            return None

        characteristic_uuids = {
            str(characteristic.uuid).lower()
            for characteristic in getattr(service, "characteristics", ())
        }
        if LEGGETT_OKIN_CHAR_UUID.lower() not in characteristic_uuids:
            return None
        return int(LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID.lower() in characteristic_uuids)

    def _build_command(self, command_value: int) -> bytes:
        """Build the runtime-selected revision-0 or revision-1 command."""
        if self._protocol_revision is None:
            self._protocol_revision = self._detect_protocol_revision()
        if self._protocol_revision == 0:
            return _build_revision_0_command(command_value)
        return build_okin_command(command_value)

    async def write_command(
        self,
        command: bytes,
        repeat_count: int = 1,
        repeat_delay_ms: int = 100,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Write an unconfirmed stream paced from each write's start time."""
        await self._write_gatt_with_retry(
            self.control_characteristic_uuid,
            command,
            repeat_count=repeat_count,
            repeat_delay_ms=repeat_delay_ms,
            cancel_event=cancel_event,
            response=False,
            wall_clock_pacing=True,
        )

    async def start_notify(
        self, callback: Callable[[str, float], None] | None = None
    ) -> None:
        """Subscribe to the status channels recovered from Prodigy CE."""
        self._notify_callback = callback
        client = self.client
        if client is None or not client.is_connected:
            _LOGGER.warning("Cannot start Leggett Okin notifications: not connected")
            return

        characteristic_uuids = self._available_characteristic_uuids() or frozenset()
        candidates = (
            LEGGETT_OKIN_NOTIFY_CHAR_UUID,
            OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID,
        )
        settings_started_now = False
        for characteristic_uuid in candidates:
            normalized_uuid = characteristic_uuid.lower()
            if (
                normalized_uuid not in characteristic_uuids
                or normalized_uuid in self._notify_started
            ):
                continue
            try:
                async with self._ble_lock:
                    await client.start_notify(characteristic_uuid, self._handle_notification)
                self._notify_started.add(normalized_uuid)
                settings_started_now |= (
                    normalized_uuid == OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID.lower()
                )
            except BleakError as err:
                _LOGGER.debug(
                    "Could not start Leggett Okin notifications on %s: %s",
                    characteristic_uuid,
                    err,
                )

        if (
            (settings_started_now or OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID.lower() in self._notify_started)
            and not self._settings_initialized
            and OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID.lower() in characteristic_uuids
        ):
            try:
                await self._write_gatt_with_retry(
                    OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID,
                    b"\x01\x02",
                    response=True,
                    log_errors=False,
                )
                self._settings_initialized = True
            except (BleakError, ConnectionError) as err:
                _LOGGER.debug("Could not initialize Leggett Okin settings: %s", err)

    def _handle_notification(self, sender: object, data: bytearray) -> None:
        """Forward raw data and retain the APK's opaque LED/status result."""
        characteristic_uuid = str(getattr(sender, "uuid", sender)).lower()
        payload = bytes(data)
        self.forward_raw_notification(characteristic_uuid, payload)
        parsed = parse_leggett_okin_feedback(payload)
        if parsed is None:
            return
        self._notification_led_mask, self._notification_status = parsed

    async def stop_notify(self) -> None:
        """Stop every active Prodigy CE status subscription."""
        self._notify_callback = None
        client = self.client
        if client is None or not client.is_connected:
            self._notify_started.clear()
            self._settings_initialized = False
            return

        for characteristic_uuid in tuple(self._notify_started):
            try:
                async with self._ble_lock:
                    await client.stop_notify(characteristic_uuid)
            except BleakError as err:
                _LOGGER.debug(
                    "Could not stop Leggett Okin notifications on %s: %s",
                    characteristic_uuid,
                    err,
                )
        self._notify_started.clear()
        self._settings_initialized = False

    def motor_pulse_settings(self) -> tuple[int, int]:
        """Keep movement streams at the proven CU170 cadence."""
        pulse_count, _ = super().motor_pulse_settings()
        return pulse_count, LEGGETT_OKIN_PULSE_DEFAULTS[1]

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
        if state.get("pillow") == MotorDirection.UP:
            command += LeggettOkinCommands.MOTOR_TILT_UP
        elif state.get("pillow") == MotorDirection.DOWN:
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

    async def _send_release_frames(
        self,
        context: str,
        *,
        raise_on_error: bool = False,
        repeat_count: int = RELEASE_FRAME_COUNT,
    ) -> None:
        """Send the release burst that ends a held keycode.

        Ordinary button release emits exactly four keycode-0 frames. Callers
        with a protocol-specific lifecycle can select another proven count. The
        release gets a fresh cancel event so a stop request cannot suppress it.

        ``raise_on_error`` is for callers where the burst *is* the operation, so
        a failure must reach the user. Cleanup callers leave it False: they are
        already unwinding and have their own error to report.
        """
        release = asyncio.ensure_future(
            self.write_command(
                self._build_command(0),
                repeat_count=repeat_count,
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
        if not self.is_memory_slot_programmable(memory_num):
            _LOGGER.warning("Memory slot %d is fixed and cannot be programmed", memory_num)
            return

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

    async def preset_anti_snore(self) -> None:
        """Go to anti-snore position (memory slot 3 on this protocol)."""
        await self._recall(LeggettOkinCommands.PRESET_ANTI_SNORE)

    async def _set_control_mode(self, command: int, context: str) -> None:
        """Send one of the APK's persistent handset-control mode commands."""
        completed = False
        try:
            await self.write_command(
                self._build_command(command),
                repeat_count=CONTROL_MODE_FRAME_COUNT,
                repeat_delay_ms=CONTROL_MODE_FRAME_DELAY_MS,
            )
            completed = True
        finally:
            await self._send_release_frames(
                context,
                raise_on_error=completed,
                repeat_count=1,
            )

    async def set_control_mode_press_and_hold(self) -> None:
        """Require a control to remain held while its action runs."""
        await self._set_control_mode(
            LeggettOkinCommands.CONTROL_MODE_PRESS_AND_HOLD,
            "press-and-hold control mode",
        )

    async def set_control_mode_press_and_release(self) -> None:
        """Allow an action to continue after its control is released."""
        await self._set_control_mode(
            LeggettOkinCommands.CONTROL_MODE_PRESS_AND_RELEASE,
            "press-and-release control mode",
        )

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

    async def massage_mode_step(self) -> None:
        """Step through massage wave patterns."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_WAVE_STEP, "massage_mode_step")

    # Pillow motor control. Keep the tilt methods as compatibility aliases for
    # service calls or stale entities created by older releases.
    async def move_pillow_up(self) -> None:
        """Move the pillow motor up."""
        await self._move_motor("pillow", MotorDirection.UP)

    async def move_pillow_down(self) -> None:
        """Move the pillow motor down."""
        await self._move_motor("pillow", MotorDirection.DOWN)

    async def move_pillow_stop(self) -> None:
        """Stop the pillow motor."""
        await self._move_motor("pillow", MotorDirection.STOP)

    async def move_tilt_up(self) -> None:
        await self.move_pillow_up()

    async def move_tilt_down(self) -> None:
        await self.move_pillow_down()

    async def move_tilt_stop(self) -> None:
        await self.move_pillow_stop()

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
