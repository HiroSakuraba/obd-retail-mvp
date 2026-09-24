#ifndef STUB_BLE_UUID_H
#define STUB_BLE_UUID_H
/* Host stub for NimBLE host/ble_uuid.h. */

#include <stdint.h>

typedef struct {
    uint8_t type;
} ble_uuid_t;

typedef struct {
    ble_uuid_t u;
    uint8_t value[16];
} ble_uuid128_t;

#define BLE_UUID128_INIT(...) { { 0 }, { __VA_ARGS__ } }

#endif /* STUB_BLE_UUID_H */
