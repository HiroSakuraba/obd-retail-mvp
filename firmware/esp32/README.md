# Glovebox dongle firmware (ESP32)

The read-only OBD-II scan client for the retail dongle. Given a CAN
transport it performs the allowlisted scan -- **VIN (09 02) ->
confirmed/pending/permanent DTCs (03/07/0A) -> live PIDs (01 xx)** -- and
renders the evidence JSON the backend session API consumes.

## Layout

| Path | What |
|---|---|
| `include/can_transport.h` | Abstract 11-bit CAN frame interface (`send`/`recv`/`millis`/`close`) |
| `include/obd_client.h` | Scan API: `obd_read_vin`, `obd_read_dtcs`, `obd_read_pid`, `obd_scan`, `obd_scan_to_json` |
| `src/obd_client.c` | Portable C99 implementation. The same file compiles for ESP32 and host |
| `src/twai_transport.c` | ESP32 TWAI peripheral, 500 kbit/s, RX filter 0x7E8..0x7EF |
| `src/tcp_transport.c` | Host-only test shim: forwards frames over TCP to the simulator |
| `src/main_esp32.c` | ESP-IDF `app_main`: TWAI + NimBLE GATT, one JSON request per BLE write |
| `src/ble_gatt.c` | NimBLE server: Nordic UART service, RX=write, TX=notify |
| `tests/` | Host suite (`make check`): 55 checks against `simulator/can_ecu_sim.py` |
| `platformio.ini` | Real-hardware build (ESP32-C3, ESP-IDF) |

It reuses the existing firmware library (`firmware/src/`): `iso15765.c`
for ISO-TP framing, `obd_allowlist.c` for the read-only gate, and
`ble_protocol.c` for the JSON request/reply envelope.

## Read-only, enforced twice

1. **BLE layer** (`obd_allowlist.c`): unknown ops and non-allowlisted PIDs
   are rejected before the CAN bus is touched.
2. **Transmit path** (`obd_client.c`): `service_tx_allowed()` plus a
   per-PID `pid_allowlisted()` check runs immediately before encoding.
   `obd_read_pid(0xFF)` returns `OBDC_ERR_FORBIDDEN` with **zero frames
   transmitted** (asserted by the test suite via a TX counter).

The only transmitted frames are allowlisted requests to `0x7DF`, plus
ISO-TP Flow Control to the responding ECU's physical id -- the
transport-layer frames strictly necessary to *receive* a multi-frame
response. The host suite includes a **wire audit**: every frame the client
sent during the whole session is checked against that rule.

## No-hardware validation (Stage A2)

```bash
# 1. simulator self-test (9/9)
python3 simulator/can_ecu_sim.py --self-test

# 2. firmware host suite (55/55): spawns the simulator twice
#    (Escape P0171, then Camry P0420 with pending+permanent codes),
#    runs the real obd_client.c over TCP, wire-audits every frame
make -C firmware/esp32/tests check
```

The simulator behaves like a real ECU where it matters: multi-frame VIN
with First Frame / Flow Control / Consecutive Frames, `43`/`47`/`4A`
DTC responses per SAE J2012, and **silence** for unsupported PIDs (the
client must time out and skip, never fail).

## Building for hardware

On a machine with PlatformIO:

```bash
cd firmware/esp32
pio run            # ESP32-C3, ESP-IDF framework
pio run -t upload
pio device monitor
```

Wiring follows `hardware/build-guide.html` (bench stage): **GPIO4 ->
transceiver TX, GPIO5 <- transceiver RX**, 500 kbit/s, transceiver on
the 3.3 V rail, OBD pins 6/14 (CANH/CANL) + 4/5 (GND). (GPIO4/5: the
ESP32-C3 has no GPIO22, and GPIO20/21 are the USB-serial pins, so they
stay free for flashing/monitor.) Override with
`-D CONFIG_OBD_TWAI_TX_PIN=<pin> -D CONFIG_OBD_TWAI_RX_PIN=<pin>`.

On first boot the firmware probes the four CAN variants (11/29-bit x
500/250 kbit/s, 11-bit 500k first) by sending a single-frame `09 02`
(VIN) request on each and locking to the first variant that answers.
The variant is fixed for the session afterwards.

## BLE API

Nordic UART service (`6E400001-B5A3-F393-E0A9-E50E24DCCA9E`), RX characteristic
write takes one JSON request line; the reply is notified on TX.

Replies of 180 bytes or fewer are notified as one raw JSON frame. Larger
replies (e.g. `full_scan`) are split into chunk envelopes:

```
{"v":1,"id":"<req id>","chunk":0,"chunks":3,"b64":"<base64 of bytes 0..119>"}
{"v":1,"id":"<req id>","chunk":1,"chunks":3,"b64":"<base64 of bytes 120..239>"}
...
```

Each envelope carries up to 120 raw bytes (160 base64 chars), so an
envelope is at most ~215 bytes on the wire: the app must negotiate an
ATT MTU of at least 218. The app concatenates the `b64` fields in `chunk`
order and base64-decodes once to recover the full JSON reply. No phone
client implements this yet -- the contract is defined here for the
future `android:app` module.

```
{"v":1,"id":"abc","op":"full_scan"}            -> evidence JSON
{"v":1,"id":"abc","op":"read_vin"}             -> {"vin":"..."}
{"v":1,"id":"abc","op":"read_dtcs_confirmed"}  -> {"dtcs_confirmed":[...]}
{"v":1,"id":"abc","op":"read_dtcs_pending"}
{"v":1,"id":"abc","op":"read_dtcs_permanent"}
{"v":1,"id":"abc","op":"read_pid","pid":"0C"}  -> {"pid":"0C","value":1726}
```

`full_scan` runs `obd_scan()` over the 20-PID default list (every PID the
six diagnostic graphs need, all allowlisted) and returns exactly the
evidence object the backend scores.

## Bench plan

See `docs/bench-test-plan.md`, Stage A2 (simulator, done here) -> Stage B2
(bench CAN rig / real vehicle, TWAI transport, traffic-capture allowlist
audit).
