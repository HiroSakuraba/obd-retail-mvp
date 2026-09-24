# Hardware BOM — build parts (transcribed)

> **Provenance:** this parts list is transcribed from a forwarded build guide
> ("Glovebox OBD BLE Dongle — Hardware Build & Bring-Up Guide", received
> 23 Sept 2026), not independently sourced. Part choices, quantities, and
> warnings below are the guide author's; verify against current datasheets
> before ordering. For the cost model and certification caveats, see
> [`docs/hardware-bom.md`](../docs/hardware-bom.md) (deliberately separate).

## Build 1 — bench prototype

Deliberately simple first build: ESP32 DevKit + Adafruit CAN Pal + OBD
breakout, powered from a USB power bank (isolated from vehicle battery power
until communication works).

| Part | Recommended item | Qty | Purpose / notes |
|---|---|---|---|
| MCU board | ESP32 DevKit-class board | 1 | Bluetooth LE + native TWAI/CAN controller; easy USB programming. Guide does not name a specific DevKit model — any DevKit-class board with BLE and TWAI-capable GPIOs works. |
| CAN physical layer | Adafruit CAN Pal (TJA1051/T3) | 1 | Converts 3.3 V logic to CAN-H/CAN-L. Exposes CANH/CANL/GND plus RX/TX. |
| OBD connector | 16-pin SAE J1962 male breakout plug or breakout cable | 1 | Access pins 4/5/6/14/16 without molding a connector. |
| Power | USB power bank | 1 | Safer first vehicle test: electronics stay isolated from OBD pin 16 until communication works. |
| Interconnect | 22–26 AWG jumpers / Dupont leads | 1 set | Bench wiring. |
| Test | Digital multimeter | 1 | Continuity, voltage, and short checks before connection. |
| Optional | Bench CAN/OBD simulator | 1 | Best way to verify frames before touching a real car. |

**⚠️ Termination warning (from the guide):** the Adafruit CAN Pal includes a
selectable 120 Ω termination resistor — **leave it OFF in a vehicle**.
The automotive bus is already terminated at both ends; the dongle must be a
high-impedance diagnostic node. (Same rule appears in `docs/hardware-bom.md`
for the custom board.)

## Build 2 — custom Rev A low-profile board

First custom PCB. The guide's recommended trio:

- **U1 — ESP32-C3-MINI-1** (preferably extended-temperature variant): BLE MCU
  module. BLE, firmware, TWAI controller, flash, crystal, RF matching, and
  antenna already integrated — avoids designing antenna matching for Rev A.
- **U2 — TI TCAN3404-Q1**: automotive CAN transceiver. Runs from the same
  3.3 V rail as the ESP32-C3 (no separate 5 V regulator), AEC-Q100,
  standby/shutdown.
- **U3 — TI LM5164-Q1**: off-battery buck, 6–100 V input → 3.3 V rail, low
  quiescent current, automotive-rated. Low no-load current matters because
  the device may sit on vehicle battery power indefinitely.

Full Rev A parts table (reference designators from the guide):

| Ref | Part | Suggested choice | Role |
|---|---|---|---|
| U1 | BLE MCU module | ESP32-C3-MINI-1, preferably extended-temperature variant | BLE, firmware, TWAI controller, flash, crystal, RF matching and antenna |
| U2 | Automotive CAN transceiver | TI TCAN3404-Q1 | 3.3 V single-supply CAN/CAN-FD physical layer; AEC-Q100; standby/shutdown |
| U3 | Off-battery buck | TI LM5164-Q1 | 6–100 V input → 3.3 V rail; low quiescent current; automotive-rated |
| D1 | Input TVS | 33 V class bidirectional automotive TVS | Clamps positive/negative electrical transients. Guide says: **final part after transient validation** — not a firm part number yet. |
| D2 | Reverse-polarity element | 60 V+ Schottky for Rev A, or ideal-diode MOSFET stage for production | Prevents reverse battery damage |
| F1 | Input protection | ~250 mA resettable PTC / fuse | Limits fault current from OBD pin 16 |
| D3 | CAN ESD array | Nexperia PESD2CANFD24V-T class device | Protects CAN-H/CAN-L from ESD and transient events |
| L1 | CAN common-mode choke | TDK ACT45B series | Optional until EMC tuning — populate only if signal-integrity/EMC testing says it's useful |
| LED1 | Status LED | Low-current blue/green LED + resistor | Pairing / scan status |
| J1 | Vehicle interface | Right-angle 16-pin J1962 male connector | Direct plug into vehicle |
| TPx | Test pads | 3V3, GND, UART/JTAG, CANH, CANL, EN, BOOT | Programming and factory test |

**Automotive power warnings (from the guide):**

- Vehicle "12 V" is not a clean 12 V rail — include PTC, reverse-polarity
  protection, TVS near the connector, wide-input buck, and input bulk +
  ceramic capacitance per the regulator reference design.
- **Never connect raw 12–14.4 V directly to the ESP32.**
- Do **not** add a 120 Ω terminator across CAN-H/CAN-L on the custom board.
- The LM5164-Q1 choice does not automatically make the board ISO 7637 /
  ISO 16750 compliant — protection values, PCB layout, and transient testing
  still need validation.

## OBD-II pins used (both builds)

| J1962 pin | Signal | Use in this MVP |
|---|---|---|
| 4 | Chassis ground | Ground reference |
| 5 | Signal ground | Ground reference |
| 6 | CAN-H | ISO 15765-4 high line |
| 14 | CAN-L | ISO 15765-4 low line |
| 16 | Battery + | **Leave disconnected during the first communication test.** Used later for standalone power (via the protected buck front end, never raw). |

## What the guide deliberately leaves vague

- Exact ESP32 DevKit model (any DevKit-class board works for the bench).
- Firm TVS part number for D1 ("final part after transient validation").
- Power-bank capacity.
- Enclosure/connector specifics for Rev A beyond "right-angle J1962 male".

See the interactive step-by-step version: [`build-guide.html`](build-guide.html).
