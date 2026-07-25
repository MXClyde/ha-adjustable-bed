"""Service registration for the Adjustable Bed integration.

Handlers live here as module-level functions rather than closures so they can be
read, tested, and type-checked independently of integration setup. Each one
recovers Home Assistant from ``call.hass``.

Every motion service is *sided*: a paired (Dual Bed) target fans out to one or
both sides, while a single bed behaves exactly as it always has.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (
    BED_TYPE_ERGOMOTION,
    BED_TYPE_KAIDI,
    BED_TYPE_KEESON,
    BED_TYPE_OKIN_CST,
    BED_TYPE_SLEEPYS_BOX25,
    CONF_BED_TYPE,
    CONF_MOTOR_COUNT,
    CONF_PROTOCOL_VARIANT,
    DEFAULT_MOTOR_COUNT,
    DOMAIN,
    OKIN_CST_POSITION_AXES,
    SIDE_BOTH,
    SIDE_LEFT,
    SIDE_RIGHT,
    bed_type_has_position_feedback,
)
from .coordinator import AdjustableBedCoordinator
from .paired_coordinator import PairedBedCoordinator, SingleAddressPairedCoordinator
from .pairing import is_paired, pair_member_addresses

if TYPE_CHECKING:
    from .beds.base import BedController

_LOGGER = logging.getLogger(__name__)

# A service target: a single bed's coordinator, or a paired bed's parent.
BedTarget = AdjustableBedCoordinator | PairedBedCoordinator
# (target, physical side) pairs connected purely to validate a call, so a failed
# pre-flight can hand them back their idle timer.
PreflightedSides = list[tuple[BedTarget, AdjustableBedCoordinator]]


# Service names
SERVICE_GOTO_PRESET = "goto_preset"
SERVICE_GENERATE_SUPPORT_BUNDLE = "generate_support_bundle"
SERVICE_SAVE_PRESET = "save_preset"
SERVICE_SET_POSITION = "set_position"
SERVICE_STOP_ALL = "stop_all"
SERVICE_TIMED_MOVE = "timed_move"

# Service call attributes
ATTR_PRESET = "preset"
ATTR_MOTOR = "motor"
ATTR_POSITION = "position"
ATTR_TARGET_ADDRESS = "target_address"
ATTR_CAPTURE_DURATION = "capture_duration"
ATTR_INCLUDE_LOGS = "include_logs"
ATTR_DIRECTION = "direction"
ATTR_DURATION_MS = "duration_ms"
ATTR_SIDE = "side"

TIMED_MOVE_MOTOR_OPTIONS = (
    "tv_lift",
    "back",
    "legs",
    "head",
    "feet",
    "tilt",
    "lumbar",
    "bed_height",
    "stair",
)

# Default capture duration for diagnostics (seconds)
DEFAULT_CAPTURE_DURATION = 120
MIN_CAPTURE_DURATION = 10
MAX_CAPTURE_DURATION = 300

# Timed move duration limits (milliseconds)
MIN_TIMED_MOVE_DURATION_MS = 100
MAX_TIMED_MOVE_DURATION_MS = 30000  # 30 seconds max


# Optional left/right/both target (paired beds). No default: when omitted, a call
# that targets one side's child device acts on just that side, otherwise it falls
# back to 'both' - so single-bed automations are unchanged.
SIDE_FIELD = {vol.Optional(ATTR_SIDE): vol.In([SIDE_LEFT, SIDE_RIGHT, SIDE_BOTH])}


def _resolve_sided_target(
    hass: HomeAssistant, device_id: str
) -> tuple[BedTarget, str | None] | None:
    """Resolve (coordinator, inferred_side) for a sided service target.

    ``inferred_side`` is the left/right of a targeted paired child sub-device
    (matched by its MAC identifier), or ``None`` for a single bed or the
    paired parent device. Lets a caller targeting one side's device act on
    just that side without passing ``side`` explicitly.
    """
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if not device:
        return None
    coordinator: BedTarget | None = None
    for entry_id in device.config_entries:
        if entry_id in hass.data.get(DOMAIN, {}):
            coordinator = hass.data[DOMAIN][entry_id]
            break
    if coordinator is None:
        return None
    inferred_side: str | None = None
    if isinstance(coordinator, PairedBedCoordinator) and not isinstance(
        coordinator, SingleAddressPairedCoordinator
    ):
        macs = {ident[1].upper() for ident in device.identifiers if ident[0] == DOMAIN}
        for side, child in coordinator.children.items():
            if child.address.upper() in macs:
                inferred_side = side
                break
    return coordinator, inferred_side


def _resolve_sided_targets(
    hass: HomeAssistant,
    device_ids: list[str],
    explicit_side: str | None,
) -> tuple[list[tuple[BedTarget, str]], list[str]]:
    """Group sided-service targets by coordinator, merging inferred sides.

    Targeting both of a pair's child devices (or the parent) in one call
    collapses to a single ``both`` fan-out - preserving the both-failure
    contract - instead of two separate side commands. Each coordinator
    appears once in first-seen order. Returns (targets, missing_device_ids).
    """
    ordered: list[int] = []
    by_key: dict[int, tuple[BedTarget, set[str | None]]] = {}
    missing: list[str] = []
    for device_id in device_ids:
        resolved = _resolve_sided_target(hass, device_id)
        if resolved is None:
            missing.append(device_id)
            continue
        coordinator, inferred_side = resolved
        key = id(coordinator)
        if key not in by_key:
            by_key[key] = (coordinator, set())
            ordered.append(key)
        by_key[key][1].add(inferred_side)

    targets: list[tuple[BedTarget, str]] = []
    for key in ordered:
        coordinator, sides = by_key[key]
        if explicit_side is not None:
            side = explicit_side
        elif sides == {SIDE_LEFT}:
            side = SIDE_LEFT
        elif sides == {SIDE_RIGHT}:
            side = SIDE_RIGHT
        else:
            # parent device (None), both children, or a mix -> whole bed.
            side = SIDE_BOTH
        targets.append((coordinator, side))
    return targets, missing


def _missing_device_error(device_id: str) -> ServiceValidationError:
    """Build the error for a service target that resolved to no bed."""
    return ServiceValidationError(
        f"Could not find Adjustable Bed device with ID {device_id}",
        translation_domain=DOMAIN,
        translation_key="device_not_found",
        translation_placeholders={"device_id": device_id},
    )


def _get_support_bundle_target_from_device(
    hass: HomeAssistant, device_id: str
) -> tuple[str, AdjustableBedCoordinator | None, ConfigEntry] | None:
    """Resolve support-bundle target details from a device registry ID."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if not device:
        return None

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue

        address = entry.data.get(CONF_ADDRESS)
        if not isinstance(address, str) and is_paired(entry.data):
            # Paired entries keep addresses only in pair_children. Resolve to
            # the targeted child sub-device's MAC (else the first member) so
            # the bundle can still capture BLE/GATT for that side by address.
            members = pair_member_addresses(entry.data)
            device_macs = {ident[1].upper() for ident in device.identifiers if ident[0] == DOMAIN}
            address = next((m for m in members if m in device_macs), None)
            if address is None and members:
                # The synthetic parent device (pair_id identifier) covers both
                # sides; a bundle is per-address, so make the user pick one
                # side's device instead of silently capturing only the first.
                raise ServiceValidationError(
                    f"{entry.title} is a paired bed; target one side's device "
                    "for the support bundle.",
                    translation_domain=DOMAIN,
                    translation_key="bundle_needs_side_for_paired",
                    translation_placeholders={"device_name": entry.title},
                )
        if not isinstance(address, str):
            continue

        coordinator: AdjustableBedCoordinator | None = None
        stored = hass.data.get(DOMAIN, {}).get(entry_id)
        if isinstance(stored, PairedBedCoordinator):
            # Reuse the matching live child coordinator so the bundle pauses
            # and reuses its connection instead of opening a second BLE link
            # (single-connection beds can't take two).
            for child in stored.children.values():
                if child.address.upper() == address.upper():
                    coordinator = child
                    break
        else:
            coordinator = cast("AdjustableBedCoordinator | None", stored)
        return address, coordinator, entry

    return None


async def _get_controller_for_service(
    coordinator: AdjustableBedCoordinator,
) -> BedController:
    """Return an active controller for service validation/execution.

    Service calls may arrive while the coordinator is idle-disconnected and
    controller is None. Reconnect first so capability checks don't fail with
    a false "not supported" error.
    """
    controller = coordinator.controller
    if controller is not None:
        return controller

    _LOGGER.debug(
        "No active controller for %s during service call; attempting reconnect",
        coordinator.name,
    )
    connected = await coordinator.async_ensure_connected(reset_timer=False)
    controller = coordinator.controller
    if not connected or controller is None:
        raise ServiceValidationError(
            f"Device '{coordinator.name}' is currently unavailable (unable to connect)",
        )
    return controller


async def _validation_controller(
    coordinator: BedTarget,
    target: AdjustableBedCoordinator,
    preflighted: PreflightedSides,
) -> BedController:
    """Return a controller for capability VALIDATION without opening a BLE link
    when avoidable.

    Prefer the side's ``capability_controller`` - the live controller, or a
    client-free one minted from config/snapshot - so we read capabilities
    without connecting. That matters for single-connection (Octo) pairs: the
    preflight validates EVERY targeted side before commanding any, and
    connecting each side to validate would momentarily hold two BLE links,
    which the sequential profile must never do. Only connect (and track the
    side in ``preflighted`` for release on failure) when no capability
    controller exists - a non-offline-mintable bed that is currently
    disconnected.
    """
    controller = target.capability_controller
    if controller is not None:
        return controller
    controller = await _get_controller_for_service(target)
    preflighted.append((coordinator, target))
    return controller


def _command_targets(coordinator: BedTarget, side: str) -> list[AdjustableBedCoordinator]:
    """Return the per-side coordinators a sided command must validate.

    For a paired bed this is the child coordinator(s) for ``side``; the
    caller validates each (pre-flight all sides before commanding any) and
    then executes via the paired coordinator's fan-out. For a single bed,
    ``left``/``right`` is rejected and ``both`` maps to the one controller.
    """
    if isinstance(coordinator, PairedBedCoordinator):
        if side == SIDE_BOTH:
            return list(coordinator.children.values())
        child = coordinator.child_for_side(side)
        if child is None:
            raise ServiceValidationError(
                f"This bed has no {side} side",
                translation_domain=DOMAIN,
                translation_key="side_not_available",
                translation_placeholders={"side": side},
            )
        return [child]

    if side != SIDE_BOTH:
        raise ServiceValidationError(
            "This is a single bed; the Left/Right/Both option only applies to paired beds.",
            translation_domain=DOMAIN,
            translation_key="side_not_supported",
        )
    return [coordinator]


async def _execute_sided(
    coordinator: BedTarget,
    side: str,
    command_fn: Callable[[BedController], Coroutine[Any, Any, None]],
    *,
    cancel_running: bool = True,
) -> None:
    """Run a command on the targeted side(s).

    A paired bed fans out (with the both-failure stop-the-other contract); a
    single bed runs exactly as before.
    """
    if isinstance(coordinator, PairedBedCoordinator):
        await coordinator.async_execute_controller_command(
            command_fn, side=side, cancel_running=cancel_running
        )
    else:
        await coordinator.async_execute_controller_command(
            command_fn, cancel_running=cancel_running
        )


async def _release_preflighted(preflighted: PreflightedSides) -> None:
    """Give every bed/side connected during a failed pre-flight a normal idle
    disconnect, so a validation abort doesn't leave a BLE link open with no
    idle timer (the command finalizer that would reset it never ran). Applies
    to single beds too, not just paired sides - both reconnect with
    reset_timer=False during validation."""
    for _coordinator, target in preflighted:
        if target.is_connected:
            with contextlib.suppress(Exception):
                await target.async_ensure_connected(reset_timer=True)


@contextlib.asynccontextmanager
async def _release_idle_on_validation_failure(
    coordinator: AdjustableBedCoordinator,
) -> AsyncIterator[None]:
    """Release a bed reconnected for a per-motor service if validation fails.

    _get_controller_for_service reconnects with reset_timer=False; without
    this an invalid set_position/timed_move would leave the BLE link open
    with no idle timer (the preset preflight path already guards this way)."""
    try:
        yield
    except ServiceValidationError:
        if coordinator.is_connected:
            with contextlib.suppress(Exception):
                await coordinator.async_ensure_connected(reset_timer=True)
        raise


def _plan_key(target: AdjustableBedCoordinator) -> int:
    """Stable per-side key for a validated plan (a proxy shares its child's identity)."""
    return getattr(target, "operation_identity", id(target))


async def handle_goto_preset(call: ServiceCall) -> None:
    """Handle goto_preset service call with sided all-target preflight."""
    hass = call.hass
    preset = call.data[ATTR_PRESET]
    device_ids = call.data.get(CONF_DEVICE_ID, [])
    explicit_side = call.data.get(ATTR_SIDE)

    _LOGGER.info("Service goto_preset called: preset=%d (side=%s)", preset, explicit_side)

    targets, missing = _resolve_sided_targets(hass, device_ids, explicit_side)
    if missing:
        raise _missing_device_error(missing[0])

    # Phase 1: validate the preset on EVERY targeted side before moving any
    # bed, so a multi-target call never half-executes.
    preflighted: PreflightedSides = []
    try:
        for coordinator, side in targets:
            for target in _command_targets(coordinator, side):
                controller = await _validation_controller(coordinator, target, preflighted)
                if not controller.supports_memory_presets:
                    raise ServiceValidationError(
                        f"Device '{target.name}' does not support memory presets",
                        translation_domain=DOMAIN,
                        translation_key="memory_presets_not_supported",
                        translation_placeholders={"device_name": target.name},
                    )
                # Validate preset against controller's memory slot count
                slot_count = controller.memory_slot_count
                if preset > slot_count:
                    raise ServiceValidationError(
                        f"Device '{target.name}' only supports memory presets 1-{slot_count}. "
                        f"Preset {preset} is not available for this bed type.",
                        translation_domain=DOMAIN,
                        translation_key="invalid_preset_number",
                        translation_placeholders={
                            "device_name": target.name,
                            "max_preset": str(slot_count),
                            "requested_preset": str(preset),
                        },
                    )
    except ServiceValidationError:
        await _release_preflighted(preflighted)
        raise

    # Phase 2: every target validated - now move them. If one bed's command
    # fails, release the still-connected preflighted beds that never ran (and
    # so never reset their idle timer) before propagating.
    try:
        for coordinator, side in targets:
            await _execute_sided(
                coordinator,
                side,
                lambda ctrl, p=preset: ctrl.preset_memory(p),  # type: ignore[misc]
            )
    except Exception:
        await _release_preflighted(preflighted)
        raise


async def handle_save_preset(call: ServiceCall) -> None:
    """Handle save_preset service call with sided all-target preflight."""
    hass = call.hass
    preset = call.data[ATTR_PRESET]
    device_ids = call.data.get(CONF_DEVICE_ID, [])
    explicit_side = call.data.get(ATTR_SIDE)

    _LOGGER.info("Service save_preset called: preset=%d (side=%s)", preset, explicit_side)

    targets, missing = _resolve_sided_targets(hass, device_ids, explicit_side)
    if missing:
        raise _missing_device_error(missing[0])

    # Phase 1: validate that every targeted side can program this slot before
    # programming any, so a multi-target call never half-executes.
    preflighted: PreflightedSides = []
    try:
        for coordinator, side in targets:
            for target in _command_targets(coordinator, side):
                controller = await _validation_controller(coordinator, target, preflighted)
                if not controller.supports_memory_programming:
                    raise ServiceValidationError(
                        f"Device '{target.name}' does not support programming memory presets",
                        translation_domain=DOMAIN,
                        translation_key="memory_programming_not_supported",
                        translation_placeholders={"device_name": target.name},
                    )
                # Validate preset against controller's memory slot count
                slot_count = controller.memory_slot_count
                if preset > slot_count:
                    raise ServiceValidationError(
                        f"Device '{target.name}' only supports memory presets 1-{slot_count}. "
                        f"Preset {preset} is not available for this bed type.",
                        translation_domain=DOMAIN,
                        translation_key="invalid_preset_number",
                        translation_placeholders={
                            "device_name": target.name,
                            "max_preset": str(slot_count),
                            "requested_preset": str(preset),
                        },
                    )
    except ServiceValidationError:
        await _release_preflighted(preflighted)
        raise

    # Phase 2: every target validated - now program them. Release any
    # still-connected preflighted bed that never ran if one fails.
    try:
        for coordinator, side in targets:
            await _execute_sided(
                coordinator,
                side,
                lambda ctrl, p=preset: ctrl.program_memory(p),  # type: ignore[misc]
                cancel_running=False,
            )
    except Exception:
        await _release_preflighted(preflighted)
        raise


async def handle_stop_all(call: ServiceCall) -> None:
    """Handle stop_all service call."""
    hass = call.hass
    device_ids = call.data.get(CONF_DEVICE_ID, [])
    explicit_side = call.data.get(ATTR_SIDE)

    _LOGGER.info("Service stop_all called (side=%s)", explicit_side)

    targets, missing_device_ids = _resolve_sided_targets(hass, device_ids, explicit_side)

    async def _stop_one(coordinator: BedTarget, side: str) -> None:
        # Validate that side applies (rejects left/right on a single bed).
        _command_targets(coordinator, side)
        if isinstance(coordinator, PairedBedCoordinator):
            await coordinator.async_stop_command(side=side)
        else:
            await coordinator.async_stop_command()

    # STOP is a safety action: attempt every target before surfacing an
    # error, so one bed's failure never leaves another still moving.
    results = await asyncio.gather(
        *(_stop_one(coordinator, side) for coordinator, side in targets),
        return_exceptions=True,
    )
    stop_errors = [r for r in results if isinstance(r, BaseException)]

    if missing_device_ids:
        raise ServiceValidationError(
            f"Could not find Adjustable Bed device(s) with ID(s): {', '.join(missing_device_ids)}",
            translation_domain=DOMAIN,
            translation_key="devices_not_found",
            translation_placeholders={"device_ids": ", ".join(missing_device_ids)},
        )

    if stop_errors:
        # Every target was attempted; surface the first failure.
        raise stop_errors[0]


async def _set_position_plan(
    parent: BedTarget,
    coordinator: AdjustableBedCoordinator,
    preflighted: PreflightedSides,
    motor: str,
    position: float,
) -> dict[str, Any]:
    """Validate one physical side and return its seek configuration."""
    async with _release_idle_on_validation_failure(coordinator):
        controller = await _validation_controller(parent, coordinator, preflighted)

        # Bed type / motor count come from the coordinator's own entry (the
        # child's ChildEntryView for a paired-side target - children aren't in
        # hass.data, so don't scan it).
        entry = coordinator.entry
        bed_type = entry.data.get(CONF_BED_TYPE)
        motor_count = entry.data.get(CONF_MOTOR_COUNT, DEFAULT_MOTOR_COUNT)
        protocol_variant = entry.data.get(CONF_PROTOCOL_VARIANT)
        supports_direct_position_control = controller.supports_direct_position_control

        # Validate bed supports position feedback
        if (
            not bed_type_has_position_feedback(bed_type, protocol_variant)
            and not supports_direct_position_control
        ):
            raise ServiceValidationError(
                f"Device '{coordinator.name}' (type: {bed_type}) does not support position feedback",
                translation_domain=DOMAIN,
                translation_key="position_feedback_not_supported",
                translation_placeholders={
                    "device_name": coordinator.name,
                    "bed_type": bed_type or "unknown",
                },
            )

        # Validate angle sensing is enabled
        if coordinator.disable_angle_sensing:
            raise ServiceValidationError(
                f"Angle sensing is disabled for device '{coordinator.name}'",
                translation_domain=DOMAIN,
                translation_key="angle_sensing_disabled",
                translation_placeholders={"device_name": coordinator.name},
            )

        # Define motor configurations.
        # For Keeson/Ergomotion: only head and feet are valid, they map to back/legs keys.
        # For BOX25: only head and feet are valid, using direct percentage positions.
        # For CST: only back and legs publish position feedback.
        # For Kaidi: direct position writes expose back/legs percentage targets.
        # For standard beds: based on motor_count (2=back/legs, 3=+head, 4=+feet).
        uses_percentage_positions = bed_type in (
            BED_TYPE_KEESON,
            BED_TYPE_ERGOMOTION,
            BED_TYPE_SLEEPYS_BOX25,
        ) or (bed_type == BED_TYPE_KAIDI and supports_direct_position_control)

        if bed_type == BED_TYPE_KAIDI and supports_direct_position_control:
            valid_motors = {"back", "legs"}
            motor_configs = {
                "back": {
                    "position_key": "back",
                    "move_up_fn": lambda ctrl: ctrl.move_back_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_back_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_back_stop(),
                    "max_value": 100.0,
                },
                "legs": {
                    "position_key": "legs",
                    "move_up_fn": lambda ctrl: ctrl.move_legs_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_legs_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_legs_stop(),
                    "max_value": 100.0,
                },
            }
        elif bed_type in (BED_TYPE_KEESON, BED_TYPE_ERGOMOTION):
            # Keeson/Ergomotion only have head and feet motors
            valid_motors = {"head", "feet"}
            motor_configs = {
                "head": {
                    "position_key": "back",  # Maps to "back" in position_data
                    "move_up_fn": lambda ctrl: ctrl.move_head_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_head_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_head_stop(),
                    "max_value": 100.0,  # Percentage
                },
                "feet": {
                    "position_key": "legs",  # Maps to "legs" in position_data
                    "move_up_fn": lambda ctrl: ctrl.move_feet_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_feet_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_feet_stop(),
                    "max_value": 100.0,  # Percentage
                },
            }
        elif bed_type == BED_TYPE_SLEEPYS_BOX25:
            valid_motors = {"head", "feet"}
            motor_configs = {
                "head": {
                    "position_key": "head",
                    "move_up_fn": lambda ctrl: ctrl.move_head_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_head_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_head_stop(),
                    "max_value": 100.0,
                },
                "feet": {
                    "position_key": "feet",
                    "move_up_fn": lambda ctrl: ctrl.move_feet_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_feet_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_feet_stop(),
                    "max_value": 100.0,
                },
            }
        elif bed_type == BED_TYPE_OKIN_CST:
            valid_motors = set(OKIN_CST_POSITION_AXES)
            motor_configs = {
                "back": {
                    "position_key": "back",
                    "move_up_fn": lambda ctrl: ctrl.move_back_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_back_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_back_stop(),
                    "max_value": coordinator.get_max_angle("back"),  # Degrees
                },
                "legs": {
                    "position_key": "legs",
                    "move_up_fn": lambda ctrl: ctrl.move_legs_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_legs_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_legs_stop(),
                    "max_value": coordinator.get_max_angle("legs"),  # Degrees
                },
            }
        else:
            # Standard beds: motor availability depends on motor_count
            motor_configs = {
                "back": {
                    "position_key": "back",
                    "move_up_fn": lambda ctrl: ctrl.move_back_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_back_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_back_stop(),
                    "max_value": coordinator.get_max_angle("back"),  # Degrees
                    "min_motors": 2,
                },
                "legs": {
                    "position_key": "legs",
                    "move_up_fn": lambda ctrl: ctrl.move_legs_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_legs_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_legs_stop(),
                    "max_value": coordinator.get_max_angle("legs"),  # Degrees
                    "min_motors": 2,
                },
                "head": {
                    "position_key": "head",
                    "move_up_fn": lambda ctrl: ctrl.move_head_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_head_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_head_stop(),
                    "max_value": coordinator.get_max_angle("head"),  # Degrees
                    "min_motors": 3,
                },
                "feet": {
                    "position_key": "feet",
                    "move_up_fn": lambda ctrl: ctrl.move_feet_up(),
                    "move_down_fn": lambda ctrl: ctrl.move_feet_down(),
                    "move_stop_fn": lambda ctrl: ctrl.move_feet_stop(),
                    "max_value": coordinator.get_max_angle("feet"),  # Degrees
                    "min_motors": 4,
                },
            }
            # Filter to valid motors based on motor_count
            valid_motors = {
                m for m, cfg in motor_configs.items() if motor_count >= cfg.get("min_motors", 2)
            }

        # Validate motor is valid for this bed
        if motor not in valid_motors:
            raise ServiceValidationError(
                f"Motor '{motor}' is not valid for device '{coordinator.name}'. "
                f"Valid motors: {', '.join(sorted(valid_motors))}",
                translation_domain=DOMAIN,
                translation_key="invalid_motor_for_bed_type",
                translation_placeholders={
                    "motor": motor,
                    "device_name": coordinator.name,
                    "valid_motors": ", ".join(sorted(valid_motors)),
                },
            )

        config = motor_configs[motor]
        max_value = config["max_value"]

        # Validate position is in range
        if position < 0 or position > max_value:
            unit = "%" if uses_percentage_positions else "°"
            raise ServiceValidationError(
                f"Position {position} is out of range for motor '{motor}'. "
                f"Valid range: 0-{max_value}{unit}",
                translation_domain=DOMAIN,
                translation_key="invalid_position_range",
                translation_placeholders={
                    "position": str(position),
                    "motor": motor,
                    "max_value": str(max_value),
                    "unit": unit,
                },
            )

        return config


async def handle_set_position(call: ServiceCall) -> None:
    """Handle set_position service call with sided all-target preflight."""
    hass = call.hass
    device_ids = call.data.get(CONF_DEVICE_ID, [])
    motor = call.data[ATTR_MOTOR]
    position = call.data[ATTR_POSITION]
    explicit_side = call.data.get(ATTR_SIDE)

    _LOGGER.info(
        "Service set_position called: motor=%s, position=%.1f%% (side=%s)",
        motor,
        position,
        explicit_side,
    )
    targets, missing = _resolve_sided_targets(hass, device_ids, explicit_side)
    if missing:
        raise _missing_device_error(missing[0])

    preflighted: PreflightedSides = []
    plans: dict[int, dict[str, Any]] = {}
    try:
        for coordinator, side in targets:
            for target in _command_targets(coordinator, side):
                plans[_plan_key(target)] = await _set_position_plan(
                    coordinator, target, preflighted, motor, position
                )
    except ServiceValidationError:
        await _release_preflighted(preflighted)
        raise

    async def seek(target: AdjustableBedCoordinator) -> None:
        config = plans[_plan_key(target)]
        await target.async_seek_position(
            position_key=cast(str, config["position_key"]),
            target_angle=position,
            move_up_fn=config["move_up_fn"],  # type: ignore[arg-type]
            move_down_fn=config["move_down_fn"],  # type: ignore[arg-type]
            move_stop_fn=config["move_stop_fn"],  # type: ignore[arg-type]
        )

    try:
        for coordinator, side in targets:
            if isinstance(coordinator, PairedBedCoordinator):
                await coordinator.async_run_child_operation("set position", seek, side=side)
            else:
                await seek(coordinator)
    except Exception:
        await _release_preflighted(preflighted)
        raise


async def _timed_move_plan(
    parent: BedTarget,
    coordinator: AdjustableBedCoordinator,
    preflighted: PreflightedSides,
    motor: str,
    direction: str,
    duration_ms: int,
) -> Callable[[BedController], Coroutine[Any, Any, None]]:
    """Validate one physical side and build its timed command."""
    # Create a narrowed reference for use in closures (mypy doesn't narrow across closures)
    coordinator_: AdjustableBedCoordinator = coordinator
    async with _release_idle_on_validation_failure(coordinator):
        controller = await _validation_controller(parent, coordinator, preflighted)

        motor_configs = {
            spec.key: {
                "move_up_fn": spec.open_fn,
                "move_down_fn": spec.close_fn,
                "move_stop_fn": spec.stop_fn,
            }
            for spec in controller.motor_control_specs
            if spec.key in TIMED_MOVE_MOTOR_OPTIONS
        }
        valid_motors = set(motor_configs)

        # Validate motor is valid for this bed
        if motor not in valid_motors:
            raise ServiceValidationError(
                f"Motor '{motor}' is not valid for device '{coordinator.name}'. "
                f"Valid motors: {', '.join(sorted(valid_motors))}",
                translation_domain=DOMAIN,
                translation_key="invalid_motor_for_bed_type",
                translation_placeholders={
                    "motor": motor,
                    "device_name": coordinator.name,
                    "valid_motors": ", ".join(sorted(valid_motors)),
                },
            )

        config = motor_configs[motor]

        # Get the appropriate move function based on direction
        move_fn = config["move_up_fn"] if direction == "up" else config["move_down_fn"]
        stop_fn = config["move_stop_fn"]

        # Execute timed movement
        # Calculate repeat count: duration_ms / pulse_delay_ms
        # Example: 3500ms on Octo (350ms delay) = 10 repeats
        _, pulse_delay_ms = controller.motor_pulse_settings()
        if pulse_delay_ms <= 0:
            _LOGGER.warning(
                "Invalid motor_pulse_delay_ms (%d) for device %s, using default 100ms",
                pulse_delay_ms,
                coordinator.name,
            )
            pulse_delay_ms = 100  # DEFAULT_MOTOR_PULSE_DELAY_MS
        # The first write is immediate, so one additional repeat is needed
        # after the requested number of delay intervals.
        calculated_repeat_count = max(
            2,
            (duration_ms + pulse_delay_ms - 1) // pulse_delay_ms + 1,
        )

        _LOGGER.debug(
            "Timed move: duration=%dms, pulse_delay=%dms, repeat_count=%d",
            duration_ms,
            pulse_delay_ms,
            calculated_repeat_count,
        )

        # Store original pulse settings to restore after
        original_pulse_count = coordinator.motor_pulse_count
        original_pulse_delay_ms = coordinator.motor_pulse_delay_ms

        # Bind closure variables as defaults to avoid late-binding bugs
        async def timed_movement(
            ctrl: BedController,
            *,
            _coordinator: AdjustableBedCoordinator = coordinator_,
            _move_fn: Callable[..., Coroutine[Any, Any, None]] = move_fn,
            _stop_fn: Callable[..., Coroutine[Any, Any, None]] = stop_fn,
            _calculated_repeat_count: int = calculated_repeat_count,
            _pulse_delay_ms: int = pulse_delay_ms,
            _original_pulse_count: int = original_pulse_count,
            _original_pulse_delay_ms: int = original_pulse_delay_ms,
        ) -> None:
            """Execute movement for specified duration, always sending stop."""
            try:
                # Temporarily set the effective pulse settings
                # This is safe because we're inside the command lock
                _coordinator._motor_pulse_count = _calculated_repeat_count
                _coordinator._motor_pulse_delay_ms = _pulse_delay_ms

                # Call the movement function (uses coordinator's pulse settings)
                await _move_fn(ctrl)
            finally:
                # Restore original pulse settings
                _coordinator._motor_pulse_count = _original_pulse_count
                _coordinator._motor_pulse_delay_ms = _original_pulse_delay_ms

                # Always send stop command
                await asyncio.shield(_stop_fn(ctrl))

        return timed_movement


async def handle_timed_move(call: ServiceCall) -> None:
    """Handle timed_move service call with sided all-target preflight."""
    hass = call.hass
    device_ids = call.data.get(CONF_DEVICE_ID, [])
    motor = call.data[ATTR_MOTOR]
    direction = call.data[ATTR_DIRECTION]
    duration_ms = call.data[ATTR_DURATION_MS]
    explicit_side = call.data.get(ATTR_SIDE)

    _LOGGER.info(
        "Service timed_move called: motor=%s, direction=%s, duration_ms=%d (side=%s)",
        motor,
        direction,
        duration_ms,
        explicit_side,
    )
    targets, missing = _resolve_sided_targets(hass, device_ids, explicit_side)
    if missing:
        raise _missing_device_error(missing[0])

    preflighted: PreflightedSides = []
    plans: dict[int, Callable[[BedController], Coroutine[Any, Any, None]]] = {}
    try:
        for coordinator, side in targets:
            for target in _command_targets(coordinator, side):
                plans[_plan_key(target)] = await _timed_move_plan(
                    coordinator, target, preflighted, motor, direction, duration_ms
                )
    except ServiceValidationError:
        await _release_preflighted(preflighted)
        raise

    async def move(target: AdjustableBedCoordinator) -> None:
        await target.async_execute_controller_command(plans[_plan_key(target)])

    try:
        for coordinator, side in targets:
            if isinstance(coordinator, PairedBedCoordinator):
                await coordinator.async_run_child_operation("timed move", move, side=side)
            else:
                await move(coordinator)
    except Exception:
        await _release_preflighted(preflighted)
        raise


async def handle_generate_support_bundle(call: ServiceCall) -> None:
    """Handle generate_support_bundle service call."""
    hass = call.hass
    from homeassistant.components.persistent_notification import async_create

    from .download import register_download
    from .support_bundle import generate_support_bundle, save_support_bundle

    device_ids = call.data.get(CONF_DEVICE_ID, [])
    target_address = call.data.get(ATTR_TARGET_ADDRESS)
    capture_duration = call.data.get(ATTR_CAPTURE_DURATION, DEFAULT_CAPTURE_DURATION)
    include_logs = call.data.get(ATTR_INCLUDE_LOGS, True)

    address: str | None = None
    coordinator: AdjustableBedCoordinator | None = None
    entry: ConfigEntry | None = None
    selected_device_id: str | None = None

    if target_address:
        from .config_flow import is_valid_mac_address

        address = str(target_address).upper().replace("-", ":")
        if not is_valid_mac_address(address):
            raise ServiceValidationError(
                f"Invalid MAC address format: {target_address}. "
                "Please provide a valid MAC address in the format "
                "XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX.",
                translation_domain=DOMAIN,
                translation_key="invalid_mac_address",
            )
        _LOGGER.info(
            "Generating support bundle for unconfigured device at %s",
            address,
        )
    elif device_ids:
        if len(device_ids) > 1:
            raise ServiceValidationError(
                "Support bundle generation only supports one configured device at a time. "
                "Select a single device or use target_address for an unconfigured bed.",
                translation_domain=DOMAIN,
                translation_key="multiple_device_targets_not_supported",
            )
        # str() narrows the untyped service-call value for the str-typed parameter
        selected_device_id = str(device_ids[0])
        target = _get_support_bundle_target_from_device(hass, selected_device_id)
        if target is not None:
            address, coordinator, entry = target
            device_name = coordinator.name if coordinator is not None else entry.title
            _LOGGER.info(
                "Generating support bundle for configured device %s at %s",
                device_name,
                address,
            )
        else:
            raise ServiceValidationError(
                f"Could not find Adjustable Bed device with ID: {selected_device_id}. "
                "Please verify the device is configured and try again.",
                translation_domain=DOMAIN,
                translation_key="device_not_found",
            )
    else:
        raise ServiceValidationError(
            "No device_id or target_address was provided. "
            "Please specify either a configured device or a target MAC address.",
            translation_domain=DOMAIN,
            translation_key="missing_target",
        )

    assert address is not None
    try:
        report = await asyncio.wait_for(
            generate_support_bundle(
                hass,
                address=address,
                capture_duration=capture_duration,
                include_logs=include_logs,
                coordinator=coordinator,
                entry=entry,
                device_id=selected_device_id,
            ),
            timeout=capture_duration + 120,
        )
        filepath = await hass.async_add_executor_job(
            save_support_bundle,
            hass,
            report,
            address,
        )

        download_url = register_download(hass, filepath)
        notification_count = len(report.get("notifications", []))
        evidence_warnings = report.get("evidence", {}).get("warnings", [])
        warning_summary = ""
        if evidence_warnings:
            warning_summary = "\n\n**Capture warnings:**\n" + "\n".join(
                f"- {warning}" for warning in evidence_warnings
            )
        async_create(
            hass,
            f"[**Download support bundle**]({download_url})\n\n"
            f"Captured {notification_count} notifications over "
            f"{capture_duration} seconds."
            f"{warning_summary}\n\n"
            "Attach this JSON file when reporting unsupported or broken beds.\n\n"
            f"File path: `{filepath}`",
            title="Adjustable Bed Support Bundle Ready",
            notification_id=f"adjustable_bed_support_bundle_{address.replace(':', '_').lower()}",
        )
        _LOGGER.info("Support bundle saved to %s", filepath)
    except TimeoutError:
        _LOGGER.exception(
            "Support bundle generation timed out after %d seconds for %s",
            capture_duration + 120,
            address,
        )
        async_create(
            hass,
            f"Support bundle generation timed out after {capture_duration + 120} seconds "
            f"for {address}.\n\n"
            "The BLE diagnostics may be hanging. Check Bluetooth connectivity and try again.",
            title="Adjustable Bed Support Bundle Timeout",
            notification_id=f"adjustable_bed_support_bundle_error_{address.replace(':', '_').lower()}",
        )
        raise
    except Exception as err:
        _LOGGER.exception("Failed to generate support bundle for %s", address)
        async_create(
            hass,
            f"Failed to generate support bundle for {address}:\n\n{err}",
            title="Adjustable Bed Support Bundle Error",
            notification_id=f"adjustable_bed_support_bundle_error_{address.replace(':', '_').lower()}",
        )
        raise


async def async_register_services(hass: HomeAssistant) -> None:
    """Register the Adjustable Bed services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_GOTO_PRESET):
        return  # Services already registered

    hass.services.async_register(
        DOMAIN,
        SERVICE_GOTO_PRESET,
        handle_goto_preset,
        schema=vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): cv.ensure_list,
                vol.Required(ATTR_PRESET): vol.All(vol.Coerce(int), vol.Range(min=1)),
                **SIDE_FIELD,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_PRESET,
        handle_save_preset,
        schema=vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): cv.ensure_list,
                vol.Required(ATTR_PRESET): vol.All(vol.Coerce(int), vol.Range(min=1)),
                **SIDE_FIELD,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_ALL,
        handle_stop_all,
        schema=vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): cv.ensure_list,
                **SIDE_FIELD,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_POSITION,
        handle_set_position,
        schema=vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): cv.ensure_list,
                vol.Required(ATTR_MOTOR): vol.In(["back", "legs", "head", "feet"]),
                # No max cap here - per-motor validation handles bed-specific limits
                vol.Required(ATTR_POSITION): vol.All(vol.Coerce(float), vol.Range(min=0)),
                **SIDE_FIELD,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TIMED_MOVE,
        handle_timed_move,
        schema=vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): cv.ensure_list,
                vol.Required(ATTR_MOTOR): vol.In(TIMED_MOVE_MOTOR_OPTIONS),
                vol.Required(ATTR_DIRECTION): vol.In(["up", "down"]),
                vol.Required(ATTR_DURATION_MS): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_TIMED_MOVE_DURATION_MS, max=MAX_TIMED_MOVE_DURATION_MS),
                ),
                **SIDE_FIELD,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_SUPPORT_BUNDLE,
        handle_generate_support_bundle,
        schema=vol.Schema(
            {
                vol.Exclusive(CONF_DEVICE_ID, "target"): cv.ensure_list,
                vol.Exclusive(ATTR_TARGET_ADDRESS, "target"): cv.string,
                vol.Optional(ATTR_CAPTURE_DURATION, default=DEFAULT_CAPTURE_DURATION): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_CAPTURE_DURATION, max=MAX_CAPTURE_DURATION),
                ),
                vol.Optional(ATTR_INCLUDE_LOGS, default=True): cv.boolean,
            }
        ),
    )

    _LOGGER.debug("Registered Adjustable Bed services")
