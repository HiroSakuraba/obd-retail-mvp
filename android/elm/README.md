# android/elm — ELM327 adapter path

Pure-Kotlin ELM327 client for commodity BLE OBD adapters (Veepeak, Carista,
VGate iCar-class). No Android imports here on purpose: the protocol parsing
(`Elm327Protocol.kt`) and the session state machine (`Elm327BleTransport.kt`)
are platform-free and unit-testable; only the `BleGatt` implementation is
Android-specific.

## Where the Android BLE wiring plugs in

Implement `BleGatt` in the app module with `BluetoothGatt`:

- `connect()` — connect to the adapter's `BluetoothDevice`, discover
  services, enable notifications on the TX characteristic.
- `write(data)` — `WRITE_TYPE_NO_RESPONSE` write of `data` to the RX
  characteristic. ELM327 commands are ASCII terminated with `\r`.
- `onNotify(handler)` — route `onCharacteristicChanged` payloads for the
  TX characteristic into `handler`.
- `disconnect()` — close the gatt.

UUID discovery: prefer the Nordic UART triple
(`6E400001-…` service, `…002` RX, `…003` TX). Some adapters (older Veepeak
firmware, no-name clones) expose custom service UUIDs — in that case
enumerate `gatt.services`, pick the service whose characteristics include
one writable and one notifiable characteristic, and log the UUIDs so they
can be added to a known-adapter table. Never hard-fail on an unknown UUID;
fail only if no write+notify pair exists.

## Threading notes

`Elm327Session` serializes all traffic: one command in flight, response read
until the `>` prompt. Call it from a single coroutine scope (e.g. a
`viewModelScope`); concurrent `transact` calls from two coroutines will
interleave bytes on the UART characteristic. The 5 s `responseTimeoutMs`
covers adapter auto-protocol search on first connect.

## Relationship to the existing code

- `scan/OBDRepository.kt` defines the `ApprovedDongleBleClient` contract for
  the future custom dongle. An `Elm327BleClient : ApprovedDongleBleClient`
  adapter (app module, Android) will wrap `Elm327Session` so the scan,
  diagnosis, and basket screens work unchanged against either transport.
- The diagnostic graphs in `diagnostic-graphs/` consume the PID map
  `readPids()` returns (keys `"05"`, `"0C"`, …); PIDs the ECU reports
  `NO DATA` for are absent from the map, and the engine treats a missing
  PID as "test not run", never as zero.
