# Linak

**Status:** ✅ Existing protocol user tested; current app corpus statically verified

**Credit:** Reverse engineering by [kristofferR](https://github.com/kristofferR/ha-adjustable-bed), jascdk and [Richard Hopton](https://github.com/richardhopton/smartbed-mqtt)

## Known Models
- Linak DPG1M (OEM controller used in many beds)
- Bedre Nætter
- Jensen
- Auping
- Carpe Diem
- Wonderland
- Svane
- Many OEM adjustable beds with Linak motors

## Analysis provenance

All three current corpus packages have frozen COMPLETE reports that pass the
reusable [Phase 4 completion gates](https://github.com/kristofferR/ha-adjustable-bed/issues/443).
The APKs, decompilation output, and reports remain machine-local as required by
[issue #436](https://github.com/kristofferR/ha-adjustable-bed/issues/436).

| App | Package and version | Artifact-set SHA-256 | Frozen report evidence |
|-----|---------------------|---------------------|------------------------|
| Bed Control | `com.linak.linakbed.ble.memory` 6.0.9 (`264`) | `4ac807c4a76c34edffa06f6d659c07ecd94382e656851fa6f784fd47076854d3` | `analysis.json` `3801cc95b7ddbffa8a828809451143ad74ddedb2ffdc93d383f2c0a474528017`; manifest `c4d5e4ca095f4a7a43aa7ce7f17f82badf16ff1b79485bf80c17511d76cbfb94` |
| Bed Connect | `com.linak.bedconnect.iot` 5.2.4 (`253`) | `21f189557b5a23c1dfc60623feacbf586571da9c125234cf48aabe89fbe2d885` | `analysis.json` `377168b2f0654353d0c5fd1827624f3de3c5a421d385b4755ec23d2fcd30b49a`; manifest `ef22dd0bf16252b6f75e08da39b3f33ce2dd92e9f8e16914a66a1c83edfba235` |
| Performance Series | `com.linak.leggettandplatt` 1.0 (`124`) | `0693675a0182fc5a7a9e430d8ead9e75fefc0e2538692712172842f12095bcfe` | `analysis.json` `405f97be46aa84cf53c17acfe86ccb1665618b2683e2c3804c7b4fc60fe89f92`; manifest `662c3b4158ef31024dcbcbd62616353ef0886cddcf8d0a3585902494fd998b63` |

## Features
| Feature | Supported |
|---------|-----------|
| Motor Control | ✅ (up to 4 app-mapped axes) |
| Position Feedback | ✅ (model dependent) |
| Memory Presets | ✅ (4 current-app slots; 5/6 retained for compatibility) |
| Massage | ✅ (model dependent) |
| Under-bed Lights | ✅ |
| Battery Level | ❌ (no reachable current-app path or integration entity) |
| Bed Connect WiFi module | ❌ (separate provisioning protocol, not motor control) |

## Protocol Details

**Service UUID:** `99fa0001-338a-1024-8a49-009c0215f78a`
**Write Characteristic:** `99fa0002-338a-1024-8a49-009c0215f78a`
**Format:** 2 bytes `[command, 0x00]`

### Motor Commands

| Command | Bytes | Description |
|---------|-------|-------------|
| Stop / release | `0xFF 0x00` | Stop all motors |
| Flat / all down | `0x00 0x00` | Lower all motors; this is not STOP |
| All up | `0x01 0x00` | Raise all motors |
| Head Up | `0x03 0x00` | Raise head |
| Head Down | `0x02 0x00` | Lower head |
| Back Up | `0x0B 0x00` | Raise back |
| Back Down | `0x0A 0x00` | Lower back |
| Legs Up | `0x09 0x00` | Raise legs |
| Legs Down | `0x08 0x00` | Lower legs |
| Feet Up | `0x05 0x00` | Raise feet |
| Feet Down | `0x04 0x00` | Lower feet |

### Preset Commands

| Command | Bytes |
|---------|-------|
| Memory 1 | `0x0E 0x00` |
| Memory 2 | `0x0F 0x00` |
| Memory 3 | `0x0C 0x00` |
| Memory 4 | `0x44 0x00` |
| Save Memory 1 | `0x38 0x00` |
| Save Memory 2 | `0x39 0x00` |
| Save Memory 3 | `0x3A 0x00` |
| Save Memory 4 | `0x45 0x00` |
| Memory 5 | `0x83 0x00` (library-only in current apps) |
| Memory 6 | `0x84 0x00` (library-only in current apps) |
| Save Memory 5 | `0x85 0x00` (library-only in current apps) |
| Save Memory 6 | `0x86 0x00` (library-only in current apps) |

### Light Commands

| Command | Bytes |
|---------|-------|
| Lights On | `0x92 0x00` (library-only in current apps) |
| Lights Off | `0x93 0x00` (library-only in current apps) |
| Lights Toggle | `0x94 0x00` (current-app UI path) |

### Massage Commands

| Command | Bytes |
|---------|-------|
| All Off | `0x80 0x00` |
| All Toggle | `0x91 0x00` |
| All Intensity Up | `0xA8 0x00` |
| All Intensity Down | `0xA9 0x00` |
| Head Toggle | `0xA6 0x00` |
| Head Up | `0x8D 0x00` |
| Head Down | `0x8E 0x00` |
| Foot Toggle | `0xA7 0x00` |
| Foot Up | `0x8F 0x00` |
| Foot Down | `0x90 0x00` |
| Mode Step | `0x81 0x00` |

### Position Feedback

Model-dependent position data is available through read/notify characteristics:

- Back: `99fa0028-...` (max 820 → 68°)
- Leg: `99fa0027-...` (max 548 → 45°)
- Head: `99fa0026-...` (3+ motors)
- Feet: `99fa0025-...` (4 motors)

The current Bed Control and Bed Connect apps parse four-byte little-endian
reference frames from characteristics `0x0024` through `0x0028`. The low 16
bits hold extension in hundredths, bits 16 through 19 are status flags, and
the upper 12 bits hold signed speed. The integration consumes the extension
needed for angle feedback. A `0x0061` battery UUID exists only as an unused
library constant in the current apps, so it is not evidence for battery
support.

## Command Timing

Motor and memory-recall commands are held actions in the current apps:

- Bed Control repeats at 100 ms and sends `0xFF 0x00` on release.
- Bed Connect writes synchronously, schedules another zero-delay write, then
  repeats every 300 ms. It sends `0xFF 0x00` on release.
- The older Performance Series app repeats every 300 ms and attempts a
  one-byte `0xFF` release. Its non-priority operation slot can reject that
  release without a retry.

The integration follows the modern apps: its configurable movement burst
(15 writes at 100 ms by default) is followed by the two-byte `0xFF 0x00`
release. The reverse jerk in
[issue #45](https://github.com/kristofferR/ha-adjustable-bed/issues/45) came
from the old `0x00 0x00` pseudo-STOP, which is actually all-down. Ending a
command refresh may also stop a controller watchdog, but it does not replace
the protocol-defined release frame.

Bed Connect also contains a separate P2 WiFi-module provisioning and firmware
stack. It is not the direct bed-motor protocol and is not implemented here.

### Position seeking

Linak keeps the normal 0.75° seek tolerance for ordinary targets. Live testing
found one lower physical endpoint where the back actuator stopped at a reported
1.0° to 1.1° when 0° was requested. A back seek to exactly 0° therefore
completes after two consecutive stalled checks at or below 1.1°, with at most
one retry after the first confirmed stall. Mid-range stalls continue to retry
normally. Other axes retain the normal tolerance at both endpoints.

Upper endpoints retain the normal tolerance. No upper-endpoint exception has
been enabled without supporting evidence; beta feedback should report any
repeatable physical maximum that remains outside the normal completion band.
