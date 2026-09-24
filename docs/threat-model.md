# Threat model — Glovebox OBD dongle + app

## Assets

- The vehicle (safety-critical physical system).
- The phone and the user's account/data.
- Backend diagnostic graphs, catalog mappings, and aggregate analytics.
- Retailer trust: one wrong-part scandal or one vehicle-harm incident kills
  the product.

## Attacker goals

1. Use the dongle as a generic CAN injection interface (the nightmare case).
2. Clone the dongle to harvest VIN/DTC data or to sell counterfeits.
3. Tamper with firmware to add write capabilities or exfiltrate data.
4. Spoof the backend to push wrong diagnoses or wrong parts.
5. Intercept BLE traffic to learn a vehicle's VIN, location patterns, or faults.

## Mitigations (designed in, not bolted on)

### No raw CAN write path — by construction
- The firmware allowlist (`firmware/src/obd_allowlist.c`) maps requests to
  exactly five read-only OBD services (01/03/07/09/0A). Unknown ops return
  `ERROR_FORBIDDEN_COMMAND` before any CAN frame is built.
- There is deliberately **no** "send raw CAN frame" BLE command, no DTC-clear
  command, and no actuator-test command. Adding one would be a deliberate
  product decision with its own threat review, not an accident.
- Even a compromised phone app cannot make the dongle transmit anything
  outside its allowlist — the enforcement is on the dongle, not in the app.
  The precise guarantee is: the device may transmit only allowlisted
  diagnostic requests plus the transport-layer frames strictly necessary to
  receive their responses (ISO-TP flow control after a First Frame). There
  is no "send arbitrary CAN frame" primitive anywhere in the firmware.

### BLE pairing and transport
- Numeric comparison / passkey pairing does not fit this hardware: the
  dongle has one LED, no display and no keypad, so there is nothing to
  compare or type on. Pairing is instead enrolled out-of-band: a unique
  cryptographic device identity is printed as a QR code on the housing /
  packaging (plus a short printed pairing code as a fallback), and the app
  binds to that identity at first pairing. Never "just works" pairing for
  a device that touches a vehicle bus.
- Encrypt the link (LE Secure Connections); the JSON protocol carries no
  secrets, but VIN + DTCs are still personal data in transit.
- The app binds to the dongle's cryptographic device identity (plus
  attested firmware version) and refuses to run diagnostics against an
  unrecognized device. The BLE MAC is *not* the device identity — BLE
  addresses may be randomized, so identity must be cryptographic, not a
  MAC allowlist.

### Two transport paths, two security guarantees

| Path | Read-only enforcement |
|---|---|
| Commodity ELM327 adapter | **Application-enforced.** The Kotlin API simply never exposes an arbitrary-command primitive — but another app talking to the same adapter is not bound by that. |
| Custom dongle | **Device-enforced.** The firmware allowlist is the enforcement point; even a compromised phone app cannot make the dongle transmit outside it. |

The roadmap earns credibility by never blurring these: the MVP on
commodity adapters is protected by app discipline, the production device
by hardware/firmware construction.

### Signed firmware
- Firmware images are signed; the bootloader verifies the signature before
  boot and before applying OTA updates. Unsigned or downgraded images
  (below the minimum security version) are rejected. The firmware-*signing*
  private key stays under manufacturer control and is never provisioned
  into dongles — devices carry only the corresponding verification key
  (plus, optionally, a separate per-device private key used solely for
  device authentication / attestation).
- The app checks the dongle's reported firmware version at connect time and
  requires an update when the security policy demands it.

### Backend and app
- The app treats the backend's graphs as data, not code: the engine only
  evaluates the documented `when` grammar — no expression can execute
  anything outside arithmetic and comparisons.
- Repair baskets are generated from the scored graph, never from free-text
  model output. An LLM may *explain* the result; it may not *decide* it.

## What happens if a dongle is cloned

- A cloned device without the signing keys cannot produce a valid firmware
  signature, so it cannot pass as an "approved" dongle to an app that checks
  attestation — it degrades to an unrecognized BLE peripheral and the app
  refuses to run the diagnostic flow.
- A clone that merely replays the read-only protocol can at most read the
  same OBD data the legitimate dongle reads; it cannot write, because the
  write path doesn't exist in the hardware/firmware design being cloned.
- Counterfeit *retail* risk (fake dongles harvesting VINs) is handled by
  in-app pairing verification against the manufacturer's device registry.

## Residual risks (accepted, monitored)

- A physically modified dongle (case opened, debug pads used) is outside the
  threat model for a giveaway device — same as any OBD tool on the market.
- BLE MAC randomization limits but does not eliminate proximity tracking of
  the dongle itself; it advertises only during pairing.
- The phone remains the weakest link: OS-level compromise bypasses app
  checks. Standard mobile hardening (certificate pinning, no sensitive data
  in logs) applies.
