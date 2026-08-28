# Leggett & Platt Okin (`leggett_okin`)

Control boxes sold as Leggett & Platt Prodigy Comfort Elite and similar, marked
`LP BED CONTROL` in BLE advertisements. Confirmed hardware: DewertOkin CU170.

Two vendor apps drive this hardware: **LP Control** (`com.leggett.android.universal`)
and the older, delisted **Prodigy CE** (`com.leggett.prodigy4`). Where they
disagree, this document follows the clean-room analysis of Prodigy CE 1.2.0 and
says so.

## Transport

| | |
|---|---|
| Service UUID | `62741523-52f9-8864-b1ab-3b3a8d65950b` |
| Write characteristic | `62741525-52f9-8864-b1ab-3b3a8d65950b` (accepts unconfirmed writes) |
| Notify characteristic | `62741625-52f9-8864-b1ab-3b3a8d65950b` |
| Pairing | Required. Neither app calls `createBond`, but the write characteristic needs an encrypted link, so Android bonds reactively on the resulting ATT error 5. |
| Position feedback | None. See [Notifications](#notifications). |

### Two frame formats

The app picks its framing once per connection, purely on whether characteristic
`00001721-0000-1000-8000-00805f9b34fb` exists under the service:

| Characteristic present | Frame | Length |
|---|---|---|
| yes (revision 1) | `04 02 <keycode big-endian 32>` | 6 bytes |
| no (revision 0) | `E5 FE 16 <keycode big-endian 32> <checksum>` | 8 bytes |

The revision-0 checksum is `~sum(bytes[0..6])` truncated to 8 bits, giving the
invariant that all eight bytes sum to `0xFF`.

**The integration currently only emits the 6-byte revision-1 frame.** A
revision-0 control box would silently ignore every command. This is a known gap,
not a proven-absent case.

## Keycodes

Everything is a 32-bit keycode. The app keeps a bitmask of currently-held
buttons, so multiple simultaneous actions are one frame with several bits set.

> **Do not trust the `FBP_KEYCODE_*` constant names in decompiled output.**
> Several are demonstrably wrong for this hardware: `0x00800000` is declared
> `LIGHT_INTENSITY_DOWN` but the shipped massage screen binds it to head massage
> **down**. The layout binding and the write boundary are the authority. Both
> independent analyses of Prodigy CE reached this conclusion separately.

### Motors

| Action | Keycode |
|---|---|
| Head up / down | `0x00000001` / `0x00000002` |
| Feet up / down | `0x00000004` / `0x00000008` |
| Tilt (pillow) up / down | `0x00000010` / `0x00000020` |
| Lumbar up / down | `0x00000040` / `0x00000080` |
| Release / stop | `0x00000000` |

### Presets

| Action | Keycode | Kind |
|---|---|---|
| Memory 1 / Zero-G | `0x00001000` | recall |
| Memory 2 | `0x00002000` | recall |
| Memory 3 / Anti-snore | `0x00004000` | recall |
| Memory 4 | `0x00008000` | recall |
| Flat | `0x08000000` | **held button**, not a recall |
| Memory store (arm) | `0x00010000` | **not a recall** |

Zero-G and anti-snore are genuine aliases: the vendor apps ship those positions
pre-assigned to slots 1 and 3, and send the same keycode.

`0x00010000` arms the box to overwrite a slot. It must never appear in the
recall ladder. Earlier releases of this integration used a ladder shifted one
step up (`0x2000`…`0x10000`), so every memory button recalled its neighbour and
"Memory 4" streamed the store-arm keycode for 30 seconds - which could
reprogram a slot with whatever position the bed happened to be in.

### Massage and lights

| Action | Keycode |
|---|---|
| Head massage up / down | `0x00000800` / `0x00800000` |
| Foot massage up / down | `0x00000400` / `0x01000000` |
| Massage on/off (toggle) | `0x00000100` |
| Massage wave mode step | `0x10000000` |
| Under-bed light toggle | `0x00020000` |

Massage power is a **toggle** with no discrete off, so the integration exposes
no massage-off button.

There is no massage timer. `0x00000200` appears in the app as a constant
(`FBP_KEYCODE_M5_IN`, a fifth actuator channel) but is never bound to a control
and never written; it must not be reconstructed as a command.

## Timing and release semantics

This protocol treats held buttons and one-shot recalls very differently, and
getting the distinction wrong is the main way to break it.

**Held keycodes** (motors, flat, light, massage) stream every ~100 ms while the
button is down. On release the app emits **exactly four** keycode-`0` frames and
then goes silent. There is no distinct stop opcode; the release frame is an
ordinary frame carrying zero.

CU170 hardware testing measured a 217-218 ms motion watchdog. The integration
therefore uses unconfirmed writes and measures the 100 ms interval from the
start of each write. Awaiting a confirmed write and then sleeping 100 ms adds
the BLE round trip to every gap, which repeatedly crosses the watchdog over a
WiFi Bluetooth proxy and makes the motor stop and restart.

**One-shot recalls** (the memory slots) are a burst of **exactly 10 frames at
~100 ms**, with **no terminator at all**. The control box drives the move to
completion by itself. Appending a release frame here risks cancelling the motion
the recall just started, so the integration deliberately does not.

LP Control 2.9.0 uses a 200 ms cadence for held commands where Prodigy CE uses
100 ms. The integration defaults to 100 ms: a shorter refresh cannot fall
outside a keep-alive window that a longer one satisfies, and users on 200 ms
reported stuttering movement.

## Memory programming

There is no program opcode. Storing a position is two ordinary held keycodes in
sequence:

1. hold `0x00010000` for ~5 s, then release
2. hold the slot keycode for ~2 s, then release

The shipped user guide corroborates this: "Touch Save… the massage motors will
buzz once. Within 5 seconds, touch the Favorite Position being edited."

## Notifications

The notify characteristic acknowledges accepted writes and also emits status
updates for physical-remote actions. The vendor app parses it into two live
indicators - sleep timer (`0x8000`) and alarm (`0x4000`) - and no parsed value
ever influences a later command.

There is **no position, angle, percentage, motor-state or error feedback of any
kind** in either app. Under-bed light state is *not* among the bits either app
reads. CU170 hardware testing confirms that a light-state bit is present, but
the exact byte and mask still need a paired before/after notification capture.
Until then the integration keeps the light as a blind toggle rather than
guessing the bit.

## Provenance

Command values, framing, timing and release semantics come from a clean-room
analysis of `com.leggett.prodigy4` 1.2.0 (versionCode 18, artifact SHA-256
`f32978d8…`), traced from layout binding to the GATT write boundary. Two
independent analyst runs agreed on every value recorded here.

Unverified against hardware, and worth a capture if you have the equipment:
which frame revision real units use, whether preset recall truly ends without a
terminator, and whether the `0x08010000` chord resets memory to factory
defaults as the vendor guide states.
