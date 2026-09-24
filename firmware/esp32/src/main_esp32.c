/* main_esp32.c - Glovebox dongle firmware (ESP-IDF application).
 *
 * Boots the TWAI CAN peripheral, brings up a NimBLE GATT server exposing
 * the Nordic UART service, and answers one JSON request per BLE write:
 *
 *   {"v":1,"id":"...","op":"read_vin"}            -> VIN
 *   {"v":1,"id":"...","op":"read_dtcs_confirmed"} -> confirmed DTCs
 *   {"v":1,"id":"...","op":"read_dtcs_pending"}   -> pending DTCs
 *   {"v":1,"id":"...","op":"read_dtcs_permanent"} -> permanent DTCs
 *   {"v":1,"id":"...","op":"read_pid","pid":"0C"} -> live PID
 *   {"v":1,"id":"...","op":"full_scan"}           -> evidence JSON
 *
 * Requests are parsed by ble_protocol.c and gated by the read-only
 * allowlist (obd_allowlist.c); the CAN layer (obd_client.c) re-checks the
 * gate on the transmit path. "full_scan" runs the whole evidence capture
 * the backend session API consumes.
 *
 * Host builds get a stub main() that says how to build for real hardware.
 */

#ifdef ESP_PLATFORM

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "ble_protocol.h"
#include "can_transport.h"
#include "obd_allowlist.h"
#include "obd_client.h"

/* Provided by twai_transport.c */
can_transport_t *twai_transport_open(void);

static const char *TAG = "glovebox";
static can_transport_t *g_can;

/* Nordic UART service (same UUIDs the Android/WebBluetooth clients use). */
#define UART_SVC_UUID        0x6E400001UL  /* 128-bit base handled below */
#define UART_TX_CHAR_UUID    0x6E400003UL
#define UART_RX_CHAR_UUID    0x6E400002UL

static void run_op(const ble_request_t *req, char *out, size_t cap)
{
    obd_op_t op = obd_op_from_request(req);
    char data[BLE_RESP_MAX];
    int rc;

    /* "full_scan" is a BLE-level convenience op, not a CAN op: run the
       whole evidence capture through the allowlisted primitives. */
    if (strcmp(req->op, "full_scan") == 0) {
        obd_scan_t scan;
        char js[2048];
        rc = obd_scan(g_can, OBD_DEFAULT_PIDS, OBD_DEFAULT_PIDS_LEN, &scan);
        if (rc != OBDC_OK) {
            proto_build_error(req, obd_error_code(OBD_ERR_BAD_REQUEST),
                              out, cap);
            return;
        }
        if (obd_scan_to_json(&scan, js, sizeof js) < 0) {
            proto_build_error(req, "ERROR_INTERNAL", out, cap);
            return;
        }
        proto_build_response(req, js, out, cap);
        return;
    }

    {
        obd_result_t dr = obd_dispatch(req, &op);
        if (dr != OBDC_OK) {
            proto_build_error(req, obd_error_code(dr), out, cap);
            return;
        }
    }

    switch (op) {
    case OP_READ_VIN: {
        char vin[OBD_VIN_LEN + 1];
        rc = obd_read_vin(g_can, vin);
        if (rc != OBDC_OK) { proto_build_error(req, "ERROR_NO_DATA", out, cap); return; }
        snprintf(data, sizeof data, "{\"vin\":\"%s\"}", vin);
        break;
    }
    case OP_READ_CURRENT_DTCS:
    case OP_READ_PENDING_DTCS:
    case OP_READ_PERMANENT_DTCS: {
        static const char *names[] = {
            "dtcs_confirmed", "dtcs_pending", "dtcs_permanent"
        };
        char codes[OBD_MAX_DTCS][OBD_DTC_STRLEN];
        int n, i, idx;
        uint8_t svc = obd_service_for(op);
        /* order: OP_READ_CURRENT_DTCS=1, PENDING=2, PERMANENT=3 */
        idx = (op == OP_READ_CURRENT_DTCS) ? 0 :
              (op == OP_READ_PENDING_DTCS) ? 1 : 2;
        n = obd_read_dtcs(g_can, svc, codes, OBD_MAX_DTCS);
        if (n < 0) { proto_build_error(req, "ERROR_NO_DATA", out, cap); return; }
        {
            size_t u = 0;
            u += (size_t)snprintf(data + u, sizeof data - u,
                                  "{\"%s\":[", names[idx]);
            for (i = 0; i < n && u < sizeof data; i++)
                u += (size_t)snprintf(data + u, sizeof data - u,
                                      "%s\"%s\"", i ? "," : "", codes[i]);
            snprintf(data + u, sizeof data - u, "]}");
        }
        break;
    }
    case OP_READ_PID: {
        double v = 0.0;
        unsigned pid = 0;
        sscanf(req->pid, "%2x", &pid);
        rc = obd_read_pid(g_can, (uint8_t)pid, &v);
        if (rc == OBDC_ERR_TIMEOUT) {
            proto_build_error(req, "ERROR_NO_DATA", out, cap);
            return;
        }
        if (rc != OBDC_OK) {
            proto_build_error(req, obd_error_code(OBD_ERR_BAD_REQUEST),
                              out, cap);
            return;
        }
        snprintf(data, sizeof data, "{\"pid\":\"%02X\",\"value\":%.4g}",
                 pid, v);
        break;
    }
    default:
        proto_build_error(req, obd_error_code(OBD_ERR_FORBIDDEN_COMMAND),
                          out, cap);
        return;
    }
    proto_build_response(req, data, out, cap);
}

/* NimBLE GATT write callback: one JSON request line in, one JSON reply
   notified out. (Full NimBLE service setup is in ble_gatt.c; this file
   owns the request -> OBD dispatch.) */
void glovebox_on_ble_write(const uint8_t *in, size_t len)
{
    static char reply[BLE_RESP_MAX + 64];
    ble_request_t req;
    int pr;

    if (len >= BLE_RESP_MAX)
        len = BLE_RESP_MAX - 1;
    pr = proto_parse_request((const char *)in, len, &req);
    if (pr == -2) {
        /* version mismatch: answer without an id echo */
        snprintf(reply, sizeof reply,
                 "{\"v\":%u,\"ok\":false,\"error\":\"ERROR_VERSION\"}",
                 BLE_PROTO_VERSION);
    } else if (pr != 0) {
        snprintf(reply, sizeof reply,
                 "{\"v\":%u,\"ok\":false,\"error\":\"ERROR_BAD_REQUEST\"}",
                 BLE_PROTO_VERSION);
    } else {
        run_op(&req, reply, sizeof reply);
    }
    ESP_LOGI(TAG, "reply: %s", reply);
    glovebox_ble_notify((const uint8_t *)reply, strlen(reply));
}

/* Provided by ble_gatt.c (NimBLE service wiring). */
void glovebox_ble_notify(const uint8_t *data, size_t len);
void glovebox_ble_start(void);

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    g_can = twai_transport_open();
    if (!g_can) {
        ESP_LOGE(TAG, "TWAI init failed -- check TX/RX wiring");
        return;
    }
    ESP_LOGI(TAG, "TWAI up at 500 kbit/s, read-only OBD client ready");

    glovebox_ble_start();   /* blocks in the NimBLE event loop */
}

#else /* host stub */

#include <stdio.h>

int main(void)
{
    puts("Glovebox dongle firmware: build for ESP32 with ESP-IDF or "
         "PlatformIO; see firmware/esp32/README.md.");
    puts("Host test: make -C firmware/esp32/tests (needs "
         "simulator/can_ecu_sim.py running).");
    return 0;
}

#endif
