/* ble_gatt.c - NimBLE GATT server exposing the Nordic UART service.
 *
 * ESP-IDF only. One service, two characteristics:
 *   RX (6E400002-...): WRITE  -> one JSON request line -> glovebox_on_ble_write
 *   TX (6E400003-...): NOTIFY -> one JSON reply line
 *
 * Pairing model (see docs/threat-model.md): the dongle has no display and
 * no numeric-comparison input, so pairing uses the QR/out-of-band code
 * printed on the dongle label (NimBLE OOB / passkey entry via the app
 * typing the printed code). This file wires the service; the OOB secret
 * provisioning is a manufacturing step, not firmware logic.
 */

#ifdef ESP_PLATFORM

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "ble_protocol.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "glovebox-ble";

/* Nordic UART service, 128-bit UUIDs. */
static const ble_uuid128_t UART_SVC_UUID =
    BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0,
                     0x93, 0xF3, 0xA3, 0xB5, 0x01, 0x00, 0x40, 0x6E);
static const ble_uuid128_t UART_RX_UUID =
    BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0,
                     0x93, 0xF3, 0xA3, 0xB5, 0x02, 0x00, 0x40, 0x6E);
static const ble_uuid128_t UART_TX_UUID =
    BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0,
                     0x93, 0xF3, 0xA3, 0xB5, 0x03, 0x00, 0x40, 0x6E);

static uint16_t g_tx_handle;
static uint16_t g_conn_handle = BLE_HS_CONN_HANDLE_NONE;

/* Implemented in main_esp32.c: JSON request -> OBD dispatch. */
void glovebox_on_ble_write(const uint8_t *in, size_t len);

/* Defined below advertise(); forward-declared so advertise() can pass it
   to ble_gap_adv_start. Without this, connection events never fire and
   g_conn_handle stays NONE forever. */
static int gap_event(struct ble_gap_event *event, void *arg);

static int gatt_write_cb(uint16_t conn_handle, uint16_t attr_handle,
                         struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle; (void)attr_handle; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        glovebox_on_ble_write(ctxt->om->om_data, ctxt->om->om_len);
    }
    return 0;
}

/* Chunking (contract for the phone app, see firmware/esp32/README.md):
 * replies of <= 180 bytes are notified as one raw JSON frame, exactly as
 * before; larger replies are split into envelopes
 *   {"v":1,"id":"<id>","chunk":<seq>,"chunks":<n>,"b64":"<base64>"}
 * each carrying up to 120 raw bytes (160 base64 chars), so an envelope is
 * at most ~215 bytes on the wire and the app must negotiate an ATT MTU
 * of at least 218. The app concatenates the "b64" fields in order and
 * base64-decodes once to recover the full JSON reply.
 */
#define CHUNK_RAW_MAX 120

static void notify_one(const uint8_t *data, size_t len)
{
    struct os_mbuf *om;
    int rc;

    om = ble_hs_mbuf_from_flat(data, (uint16_t)len);
    if (!om)
        return;
    rc = ble_gatts_notify_custom(g_conn_handle, g_tx_handle, om);
    if (rc != 0)
        ESP_LOGW(TAG, "notify failed: %d", rc);
}

void glovebox_ble_notify(const char *req_id, const uint8_t *data, size_t len)
{
    size_t n, i;

    if (!req_id || !req_id[0])
        req_id = "unknown";
    if (g_conn_handle == BLE_HS_CONN_HANDLE_NONE || !data || len == 0)
        return;
    if (len <= 180) {
        notify_one(data, len);
        return;
    }
    n = (len + CHUNK_RAW_MAX - 1) / CHUNK_RAW_MAX;
    for (i = 0; i < n; i++) {
        size_t off = i * CHUNK_RAW_MAX;
        size_t clen = (len - off > CHUNK_RAW_MAX) ? CHUNK_RAW_MAX : len - off;
        /* 120 raw bytes -> 160 base64 chars; envelope ~215 bytes max. */
        char b64[164];
        char env[256];
        int w;

        if (proto_b64_encode(data + off, clen, b64, sizeof b64) != 0)
            return;
        w = snprintf(env, sizeof env,
                     "{\"v\":%u,\"id\":\"%s\",\"chunk\":%u,"
                     "\"chunks\":%u,\"b64\":\"%s\"}",
                     BLE_PROTO_VERSION, req_id,
                     (unsigned)i, (unsigned)n, b64);
        if (w < 0 || (size_t)w >= sizeof env)
            return;
        notify_one((const uint8_t *)env, (size_t)w);
    }
}

static const struct ble_gatt_svc_def gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &UART_SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &UART_RX_UUID.u,
                .access_cb = gatt_write_cb,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &UART_TX_UUID.u,
                .access_cb = gatt_write_cb,
                .val_handle = &g_tx_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            { 0 },
        },
    },
    { 0 },
};

static void advertise(void)
{
    struct ble_gap_adv_params adv_params;
    struct ble_hs_adv_fields fields;
    int rc;

    memset(&fields, 0, sizeof fields);
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)"Glovebox-OBD";
    fields.name_len = strlen("Glovebox-OBD");
    fields.name_is_complete = 1;
    /* Advertise the UART service so scanners find us without bonding. */
    fields.uuids128 = (ble_uuid128_t[]){ UART_SVC_UUID };
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;

    rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) { ESP_LOGE(TAG, "adv_set_fields %d", rc); return; }
    memset(&adv_params, 0, sizeof adv_params);
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                           &adv_params, gap_event, NULL);
    if (rc != 0)
        ESP_LOGE(TAG, "adv_start %d", rc);
    else
        ESP_LOGI(TAG, "advertising as Glovebox-OBD");
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            g_conn_handle = event->connect.conn_handle;
            ESP_LOGI(TAG, "BLE connected");
        } else {
            advertise();  /* resume advertising on failed connect */
        }
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        g_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        ESP_LOGI(TAG, "BLE disconnected");
        advertise();
        break;
    default:
        break;
    }
    return 0;
}

static void ble_host_task(void *param)
{
    (void)param;
    nimble_port_run();   /* does not return */
    nimble_port_freertos_deinit();
}

static void on_sync(void)
{
    int rc = ble_hs_id_infer_auto(0, NULL);
    if (rc != 0) { ESP_LOGE(TAG, "id_infer_auto %d", rc); return; }
    advertise();
}

void glovebox_ble_start(void)
{
    nimble_port_init();
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_gatts_count_cfg(gatt_svcs);
    ble_gatts_add_svcs(gatt_svcs);
    ble_svc_gap_device_name_set("Glovebox-OBD");
    ble_hs_cfg.sync_cb = on_sync;
    nimble_port_freertos_init(ble_host_task);
}

#else /* host stub */

void glovebox_ble_notify(const char *req_id, const uint8_t *data, size_t len)
{
    (void)req_id; (void)data; (void)len;
}

void glovebox_ble_start(void)
{
}

#endif
