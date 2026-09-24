/* twai_transport.c - ESP32 TWAI (CAN) transport for the OBD client.
 *
 * Compiled into the firmware only under ESP-IDF (ESP_PLATFORM defined).
 * On the host it provides a stub so accidental compilation fails loudly
 * instead of silently linking nothing.
 *
 * Wiring (bench stage, see hardware/build-guide.html):
 *   GPIO4 -> transceiver TX (TWAI TX), GPIO5 <- transceiver RX (TWAI RX).
 * Plain exposed GPIOs on the DevKitM-1, deliberately avoiding the
 * USB-serial pins (GPIO20 RX / GPIO21 TX). Both are Kconfig-overridable
 * (CONFIG_OBD_TWAI_TX_PIN / CONFIG_OBD_TWAI_RX_PIN).
 *
 * Protocol-variant detection: real vehicles speak one of four CAN OBD
 * variants (11/29-bit identifiers x 500/250 kbit/s). twai_transport_open()
 * probes each variant in turn with a single-frame "09 02" (VIN) request
 * and locks onto the first one that yields a plausible OBD response.
 * The negotiated variant is published on t->variant so the client
 * derives its request/response ids from it (see obd_client.c).
 */

#include "can_transport.h"

#if defined(ESP_PLATFORM)

#include <stdlib.h>
#include <string.h>

#include "driver/twai.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#ifndef CONFIG_OBD_TWAI_TX_PIN
#define CONFIG_OBD_TWAI_TX_PIN 4
#endif
#ifndef CONFIG_OBD_TWAI_RX_PIN
#define CONFIG_OBD_TWAI_RX_PIN 5
#endif

/* Most common variant first: detection usually succeeds immediately. */
typedef struct {
    can_variant_t variant;
    twai_timing_config_t tcfg;
    bool extd;
    uint32_t func_id;
    uint32_t resp_base;
    uint32_t resp_last;
} probe_variant_t;

static const probe_variant_t PROBE_VARIANTS[] = {
    { CAN_VAR_11B_500K, TWAI_TIMING_CONFIG_500KBITS(), false,
      CAN_ID_FUNC_REQUEST_11B, CAN_ID_RESP_BASE_11B, CAN_ID_RESP_LAST_11B },
    { CAN_VAR_29B_500K, TWAI_TIMING_CONFIG_500KBITS(), true,
      CAN_ID_FUNC_REQUEST_29B, CAN_ID_RESP_BASE_29B, CAN_ID_RESP_LAST_29B },
    { CAN_VAR_11B_250K, TWAI_TIMING_CONFIG_250KBITS(), false,
      CAN_ID_FUNC_REQUEST_11B, CAN_ID_RESP_BASE_11B, CAN_ID_RESP_LAST_11B },
    { CAN_VAR_29B_250K, TWAI_TIMING_CONFIG_250KBITS(), true,
      CAN_ID_FUNC_REQUEST_29B, CAN_ID_RESP_BASE_29B, CAN_ID_RESP_LAST_29B },
};

#define PROBE_TIMEOUT_MS 250u   /* per variant */

typedef struct {
    unsigned long tx_frames;
} twai_ctx_t;

static uint32_t twai_millis(can_transport_t *t)
{
    (void)t;
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static int twai_send(can_transport_t *t, const can_frame_t *f)
{
    twai_ctx_t *c = (twai_ctx_t *)t->ctx;
    twai_message_t msg;

    if (!c || !f || f->dlc > 8)
        return -1;
    memset(&msg, 0, sizeof msg);
    msg.identifier = f->id;
    if (f->extd)
        msg.flags |= TWAI_MSG_FLAG_EXTD;
    msg.data_length_code = f->dlc;
    memcpy(msg.data, f->data, f->dlc);
    /* 100 ms to queue; a wedged bus must not wedge the scan task. */
    if (twai_transmit(&msg, pdMS_TO_TICKS(100)) != ESP_OK)
        return -1;
    c->tx_frames++;
    return 0;
}

static int twai_recv(can_transport_t *t, can_frame_t *f, uint32_t timeout_ms)
{
    twai_message_t msg;
    (void)t;

    if (!f)
        return -1;
    if (twai_receive(&msg, pdMS_TO_TICKS(timeout_ms)) == ESP_ERR_TIMEOUT)
        return 1;
    if (msg.flags & TWAI_MSG_FLAG_RTR)
        return 1;                       /* ignore remote frames */
    if (msg.data_length_code > 8)
        return -1;
    /* Accept-all filter: the OBD client matches response ids itself. */
    f->id = msg.identifier;
    f->extd = (msg.flags & TWAI_MSG_FLAG_EXTD) != 0;
    f->dlc = msg.data_length_code;
    memcpy(f->data, msg.data, f->dlc);
    return 0;
}

static void twai_close(can_transport_t *t)
{
    if (!t)
        return;
    twai_stop();
    twai_driver_uninstall();
    free(t->ctx);
    free(t);
}

/* Probe one variant: install the driver at its bitrate, send a
   single-frame "09 02" on its functional id, and wait for a plausible
   OBD response id. Returns 0 on detection, -1 otherwise. The driver is
   uninstalled on the way out either way. */
static int probe_variant(const probe_variant_t *pv)
{
    twai_general_config_t gcfg =
        TWAI_GENERAL_CONFIG_DEFAULT(CONFIG_OBD_TWAI_TX_PIN,
                                    CONFIG_OBD_TWAI_RX_PIN,
                                    TWAI_MODE_NORMAL);
    twai_filter_config_t fcfg = TWAI_FILTER_CONFIG_ACCEPT_ALL();
    twai_message_t tx, rx;
    uint32_t deadline;

    if (twai_driver_install(&gcfg, &pv->tcfg, &fcfg) != ESP_OK)
        return -1;
    if (twai_start() != ESP_OK) {
        twai_driver_uninstall();
        return -1;
    }

    memset(&tx, 0, sizeof tx);
    tx.identifier = pv->func_id;
    if (pv->extd)
        tx.flags |= TWAI_MSG_FLAG_EXTD;
    tx.data_length_code = 8;
    tx.data[0] = 0x02;      /* PCI: single frame, 2 payload bytes */
    tx.data[1] = 0x09;      /* service 09 */
    tx.data[2] = 0x02;      /* PID 02: VIN */

    if (twai_transmit(&tx, pdMS_TO_TICKS(100)) != ESP_OK) {
        twai_stop();
        twai_driver_uninstall();
        return -1;
    }

    deadline = (uint32_t)(esp_timer_get_time() / 1000) + PROBE_TIMEOUT_MS;
    for (;;) {
        uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        uint32_t wait = (deadline > now) ? (deadline - now) : 0;
        if (twai_receive(&rx, pdMS_TO_TICKS(wait)) == ESP_ERR_TIMEOUT)
            break;
        if (rx.data_length_code == 0)
            continue;
        if (((rx.flags & TWAI_MSG_FLAG_EXTD) != 0) != pv->extd)
            continue;
        if (rx.identifier < pv->resp_base || rx.identifier > pv->resp_last)
            continue;
        /* Plausible OBD response: single/first/consecutive frame PCI. */
        if ((rx.data[0] >> 4) <= 0x2) {
            twai_stop();
            twai_driver_uninstall();
            return 0;
        }
    }
    twai_stop();
    twai_driver_uninstall();
    return -1;
}

/* Install the TWAI driver, auto-detect the CAN OBD variant, and open the
   transport. Returns NULL on failure (no variant answered). */
can_transport_t *twai_transport_open(void)
{
    twai_general_config_t gcfg =
        TWAI_GENERAL_CONFIG_DEFAULT(CONFIG_OBD_TWAI_TX_PIN,
                                    CONFIG_OBD_TWAI_RX_PIN,
                                    TWAI_MODE_NORMAL);
    twai_filter_config_t fcfg = TWAI_FILTER_CONFIG_ACCEPT_ALL();
    const probe_variant_t *winner = NULL;
    twai_ctx_t *c;
    can_transport_t *t;
    size_t i;

    for (i = 0; i < sizeof PROBE_VARIANTS / sizeof PROBE_VARIANTS[0]; i++) {
        if (probe_variant(&PROBE_VARIANTS[i]) == 0) {
            winner = &PROBE_VARIANTS[i];
            break;
        }
    }
    if (!winner)
        return NULL;

    if (twai_driver_install(&gcfg, &winner->tcfg, &fcfg) != ESP_OK)
        return NULL;
    if (twai_start() != ESP_OK) {
        twai_driver_uninstall();
        return NULL;
    }
    c = (twai_ctx_t *)calloc(1, sizeof *c);
    t = (can_transport_t *)calloc(1, sizeof *t);
    if (!c || !t) {
        free(c);
        free(t);
        twai_stop();
        twai_driver_uninstall();
        return NULL;
    }
    t->ctx = c;
    t->variant = winner->variant;
    t->send = twai_send;
    t->recv = twai_recv;
    t->millis = twai_millis;
    t->close = twai_close;
    return t;
}

#else /* host stub */

#warning "twai_transport.c compiled without ESP_PLATFORM: stub only"

#include <stddef.h>

can_transport_t *twai_transport_open(void)
{
    return NULL;
}

#endif
