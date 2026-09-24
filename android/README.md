# android/ — Gradle project

## Layout

```
android/
├── settings.gradle.kts / build.gradle.kts   # project config
├── gradlew, gradlew.bat, gradle/wrapper/     # Gradle 8.10.2 wrapper
├── core/                                    # pure-Kotlin JVM module (this is the real code)
│   └── src/main/kotlin/com/glovebox/obd/
│       ├── ble/        ApprovedDongleBleClient — contract for the custom dongle
│       ├── diagnosis/  DiagnosticEngine — graph scoring, "when"-rule evaluator
│       ├── elm/        Elm327Protocol + Elm327Session — commodity-adapter path
│       └── scan/       OBDRepository, VehicleSession, DtcEvidence
├── core/src/test/kotlin/                    # unit tests (kotlin.test + JUnit 5)
└── elm/README.md                            # BLE GATT wiring notes for the app module
```

## Building and testing

```sh
cd android
./gradlew :core:test
```

Requires JDK 17+ (`jvmToolchain(17)`). Kotlin 2.1.0, JUnit 5. No Android
SDK needed — `:core` is platform-free by design, which is what makes the
protocol parsers, the rule evaluator, and the scan orchestration
unit-testable on the JVM.

## Where the Android app plugs in (future work)

There is deliberately **no `:app` module yet**: a real one needs the
Android SDK (Manifest, Compose UI, `BluetoothGatt` implementation,
permissions, lifecycle, background BLE), and faking it would be worse
than not having it. When it exists:

- Implement `elm.BleGatt` with `BluetoothGatt` (wiring notes in
  `elm/README.md`), then wrap `Elm327Session` as an
  `Elm327BleClient : ApprovedDongleBleClient` so scan/diagnosis/basket
  work unchanged against either transport.
- Implement `scan.DiagnosticApi` over HTTP against the FastAPI backend:
  `GET /v1/sessions/{id}/required-signals` → `{"signals": [...]}`.
  The interface is already shaped around that JSON contract; `:core`
  derives the plain Mode-01 PIDs from signal recipes and the `:app`
  module will own the actual over-time measurement protocols (sampling
  O₂ traces, idle-vs-2500-RPM trims, …).

## Security note (two paths)

- **Commodity ELM327**: read-only is *application-enforced* — this code
  never exposes an arbitrary-command primitive, but another app on the
  same adapter is not bound by that.
- **Custom dongle**: read-only is *device-enforced* by the firmware
  allowlist; a hostile phone app cannot escape it.
