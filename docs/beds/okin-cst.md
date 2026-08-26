# Okin CST (CSTProtocol)

**Status:** Static analysis complete; hardware validation pending
**Ref:** Phase 4 clean-room analysis of `com.okin.bedding.rizemf900` 1.1.2

## Known Brands

- Mattress Firm 900-O / MFirm 900-O
- Rize MF900
- Nectar Motion / some `OKIN-*` Nectar bases

## Detection

| Signal | Value |
|--------|-------|
| Service UUID | `62741523-52f9-8864-b1ab-3b3a8d65950b` (standard OKIN) |
| Name patterns | Varies (shared UUID requires disambiguation; some report as `OKIN-XXXXXX`) |
| Connected GATT hint | CSS `90311623-...` plus Nordic DFU `00001530-...`, unless a stronger device identity selects another shared-UUID protocol |
| BLE Pairing | Required |

Manual selection may be needed since the service UUID is shared with other Okin protocols.
Choose this profile for Nectar Motion style `OKIN-*` bases when diagnostics show
both the CSS service and Nordic DFU service.
This also applies to Mattress Firm 900-O / MFirm 900-O bases advertising as
`OKIN-XXXXXX` with the same connected GATT signature.

Do not select CST for `LP BED...` receivers. LP Control 2.9.0 identifies those
as its Okin profile and sends 6-byte commands, even when the receiver also
exposes CSS and Nordic DFU services. See [Leggett & Platt](leggett-platt.md).

## Pairing

The captured MFirm receiver gates its reads and notification characteristics behind
an OS-level Bluetooth **bond**. Pairing is "Just Works": **no PIN** and **no
dedicated Bluetooth pairing button**. Before bonding, those characteristics return
GATT `error=5` "Insufficient authentication". The command characteristic
(`62741525-...`) itself was readable unbonded, but the integration still establishes
the bond so the connection matches the official app's full GATT session.

**To enter pairing mode, power-cycle the control box:** unplug it for ~30 seconds,
then plug it back in. The status light blinks blue, then turns green after ~20 s —
that window is when the base accepts a new bond. Some models instead use the
under-bed lamp/light button (hold until it blinks blue). The physical "Pair"/"Learn"
button found on some OKIN control boxes only syncs the **RF remote**, not Bluetooth.

The integration requests `pair=True` automatically and verifies the bond after
connecting; if the link connected but did not bond it clears its cached bond state,
re-pairs on the next attempt, and surfaces a **"Bluetooth pairing required"** repair
with a guided **Fix** button. ESPHome Bluetooth proxies can pair only on ESPHome
≥ 2024.3.0; a local adapter near the bed is the most reliable for the first bond.

## Protocol

Uses a 14-byte command format with two separate 32-bit fields:

```text
[0x0C, 0x02, primary[4], secondary[4], 0x00, 0x00, 0x00, 0x00]
```

- **Primary field** (bytes 2-5): Head, foot, and lumbar motor control plus
  presets, memory-save chords, light toggle, massage stop, and intensity
- **Secondary field** (bytes 6-9): Discrete light on/off and massage wave modes
- **Write characteristic:** `62741525-...`; the app leaves the characteristic's
  runtime/default write type unchanged, while the captured hardware advertises
  the `write` property
- **Notify characteristic:** `62741625-...`; the app only watches byte 10 for a
  generic change and does not decode motor positions

Field placement is app-specific. Do not assume all presets, lights, or massage
commands use the secondary field.

### Motor Commands (primary field)

| Action | Value |
|--------|-------|
| Stop | `0x00000000` |
| Head Up | `0x00000001` |
| Head Down | `0x00000002` |
| Foot Up | `0x00000004` |
| Foot Down | `0x00000008` |
| Lumbar Up | `0x00000010` |
| Lumbar Down | `0x00000020` |

Multiple motor bits can be OR'd together for simultaneous movement.

### Remote Actions (primary field)

| Action | Value |
|--------|-------|
| Flat | `0x08000000` |
| Zero-G | `0x00001000` |
| Lounge | `0x00002000` |
| Incline / TV | `0x00004000` |
| Anti-snore | `0x00008000` |
| Save Zero-G | `0x08001000` |
| Save Lounge | `0x08002000` |
| Save Incline | `0x08004000` |
| Light Toggle | `0x00020000` |
| Massage Off | `0x02000000` |
| Massage All + | `0x00000C00` |
| Massage All - | `0x01800000` |

### Remote Actions (secondary field)

| Action | Value |
|--------|-------|
| Light On | `0x00000040` |
| Light Off | `0x00000080` |
| Massage Wave 1 | `0x00080000` |
| Massage Wave 2 | `0x00100000` |
| Massage Wave 3 | `0x00200000` |

### Timing

The Android app sends the active command immediately and every 100 ms while a
button is held. On release it sends the all-zero STOP frame twice, at +100 ms and
+200 ms. For one-shot voice actions such as presets, lights, and massage, the app
streams for 500 ms before the same delayed STOP cleanup. Home Assistant uses that
fixed one-shot cadence for button entities and keeps motor movement duration
configurable.

### Memory Slots

The MFirm app treats Zero-G, Incline, and Lounge as user-programmable preset
memories. Home Assistant exposes those as numbered memory slots:

| HA Memory Slot | MFirm App Memory |
|----------------|------------------|
| Memory 1 | Zero-G |
| Memory 2 | Incline |
| Memory 3 | Lounge |

## Features

- 3 motors: head, foot, lumbar
- Flat, Zero-G, anti-snore, lounge, and incline presets
- 3 programmable preset memory slots: Zero-G, Incline, and Lounge
- Discrete light on/off plus toggle
- Massage with global intensity control, stop, and three wave modes
- Under-bed light toggle and discrete on/off; no RGB command was found in the app
- No decoded motor-position feedback

## Relationship to Other Okin Protocols

Many command values match Okimat/Okin UUID values. CST differs in packet framing
and in which 32-bit field carries each remote action.

## App

- **Android:** Mattress Firm 900 - O / MFirm 900-O (`com.okin.bedding.rizemf900`)
- **Android:** `com.okin.bedding.nectarmotion` (historically associated with this
  profile; not part of this clean-room run)

## Source

The command table and timing above come from a COMPLETE Phase 4 clean-room
analysis of the frozen MFirm 900-O 1.1.2 XAPK, archive SHA-256
`a4f5ae67b2b9b870e6413d08597364041ac6947c7ba5445eb1979498895ff46f`.
The analysis covered all 24 reachable control frames and independently checked
the Java decompilation against smali bytecode. Physical behavior still needs
validation on target hardware.
