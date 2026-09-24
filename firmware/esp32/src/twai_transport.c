/* twai_transport.c - ESP32 TWAI (CAN) transport for the OBD client.
 *
 * Compiled into the firmware only under ESP-IDF (ESP_PLATFORM defined).
 * On the host it provides a stub so accidental compilation fails loudly
 * instead of silently linking nothing.
 *
 * Wiring (bench stage, see hardware/build-guide.html):
 *   GPIO21 -> transceiver TX (TWAI TX), GPIO22 <- transceiver RX (TWAI RX).
 * Both are Kconfig-overridable (CONFIG_OBD_TWAI_TX_PIN /
 * CONFIG_OBD_TWAI_RX_PIN); the Rev A PCB remaps these for the ESP32-C3.
 */

#include "can_transport.h"

#if defined(ESP_PLATFORM)

#include <string.h>

#include "driver/twai.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#ifndef CONFIG_OBD_TWAI_TX_PIN
#define CONFIG_OBD_TWAI_TX_PIN 21
#endif
#ifndef CONFIG_OBD_TWAI_RX_PIN
#define CONFIG_OBD_TWAI_RX_PIN 22
#endif

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

    if (!f)
        return -1;
    if (twai_receive(&msg, pdMS_TO_TICKS(timeout_ms)) == ESP_ERR_TIMEOUT)
        return 1;
    if (msg.flags & (TWAI_MSG_FLAG_RTR | TWAI_MSG_FLAG_EXTD))
        return 1;                       /* ignore remote/extended frames */
    if (msg.data_length_code > 8)
        return -1;
    f->id = msg.identifier;
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

/* Install the TWAI driver at 500 kbit/s (ISO 15765 / SAE J2284) and open
   the transport. Returns NULL on failure. */
can_transport_t *twai_transport_open(void)
{
    twai_general_config_t gcfg =
        TWAI_GENERAL_CONFIG_DEFAULT(CONFIG_OBD_TWAI_TX_PIN,
                                    CONFIG_OBD_TWAI_RX_PIN,
                                    TWAI_MODE_NORMAL);
    twai_timing_config_t tcfg = TWAI_TIMING_CONFIG_500KBITS();
    twai_filter_config_t fcfg = TWAI_FILTER_CONFIG_ACCEPT_ALL();
    twai_ctx_t *c;
    can_transport_t *t;

    /* Only listen to OBD response ids 0x7E8..0x7EF; everything else is
       bus noise. Low 3 id bits are don't-care (mask bits 21..23). */
    fcfg.acceptance_code = (CAN_ID_RESP_BASE << 21);
    fcfg.acceptance_mask = ~(0x7F8u << 21);
    fcfg.single_filter = true;

    if (twai_driver_install(&gcfg, &tcfg, &fcfg) != ESP_OK)
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
