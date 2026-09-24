#ifndef STUB_BLE_HS_H
#define STUB_BLE_HS_H
/* Host stub for NimBLE host/ble_hs.h: only the surface ble_gatt.c uses. */

#include <stdint.h>

#include "host/ble_uuid.h"

#define BLE_HS_CONN_HANDLE_NONE 0xFFFFu
#define BLE_HS_ADV_F_DISC_GEN   0x02u
#define BLE_HS_ADV_F_BREDR_UNSUP 0x04u
#define BLE_HS_FOREVER          0
#define BLE_OWN_ADDR_PUBLIC     0

#define BLE_GAP_CONN_MODE_UND   0
#define BLE_GAP_DISC_MODE_GEN   0

#define BLE_GAP_EVENT_CONNECT    0
#define BLE_GAP_EVENT_DISCONNECT 1

#define BLE_GATT_ACCESS_OP_WRITE_CHR 1
#define BLE_GATT_SVC_TYPE_PRIMARY    1
#define BLE_GATT_CHR_F_WRITE  0x08u
#define BLE_GATT_CHR_F_NOTIFY 0x10u

struct os_mbuf {
    uint8_t *om_data;
    uint16_t om_len;
};

struct ble_hs_adv_fields {
    uint8_t flags;
    const uint8_t *name;
    uint8_t name_len;
    uint8_t name_is_complete;
    const ble_uuid128_t *uuids128;
    uint8_t num_uuids128;
    uint8_t uuids128_is_complete;
};

struct ble_gap_adv_params {
    uint8_t conn_mode;
    uint8_t disc_mode;
};

struct ble_gap_event {
    uint8_t type;
    union {
        struct {
            int status;
            uint16_t conn_handle;
        } connect;
        struct {
            int reason;
        } disconnect;
    };
};

typedef int ble_gap_event_fn(struct ble_gap_event *event, void *arg);

struct ble_gatt_access_ctxt {
    uint8_t op;
    struct os_mbuf *om;
};

struct ble_gatt_chr_def {
    const void *uuid;
    int (*access_cb)(uint16_t conn_handle, uint16_t attr_handle,
                     struct ble_gatt_access_ctxt *ctxt, void *arg);
    uint16_t *val_handle;
    uint16_t flags;
};

struct ble_gatt_svc_def {
    uint8_t type;
    const void *uuid;
    const struct ble_gatt_chr_def *characteristics;
};

struct ble_hs_cfg {
    void (*sync_cb)(void);
};

extern struct ble_hs_cfg ble_hs_cfg;

int ble_gap_adv_set_fields(const struct ble_hs_adv_fields *fields);
int ble_gap_adv_start(uint8_t own_addr_type, const void *addr,
                      int32_t duration_ms,
                      const struct ble_gap_adv_params *adv_params,
                      ble_gap_event_fn *cb, void *cb_arg);
struct os_mbuf *ble_hs_mbuf_from_flat(const void *data, uint16_t len);
int ble_gatts_notify_custom(uint16_t conn_handle, uint16_t attr_handle,
                            struct os_mbuf *om);
int ble_hs_id_infer_auto(int privacy, void *out_addr);
int ble_gatts_count_cfg(const struct ble_gatt_svc_def *defs);
int ble_gatts_add_svcs(const struct ble_gatt_svc_def *defs);

#endif /* STUB_BLE_HS_H */
