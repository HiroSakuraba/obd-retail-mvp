#ifndef STUB_TWAI_H
#define STUB_TWAI_H
/* Host stub for ESP-IDF driver/twai.h: struct layouts mirror the real
 * driver closely enough that field accesses in twai_transport.c
 * type-check; every function is a declaration only. */

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

#define TWAI_MSG_FLAG_EXTD 0x01u
#define TWAI_MSG_FLAG_RTR  0x02u

typedef struct {
    uint32_t flags;            /* TWAI_MSG_FLAG_* */
    uint32_t identifier;       /* 11- or 29-bit CAN id */
    uint8_t  data_length_code; /* 0..8 */
    uint8_t  data[8];
} twai_message_t;

#define TWAI_MODE_NORMAL 0

typedef struct {
    int mode;
    int tx_pin;
    int rx_pin;
} twai_general_config_t;

#define TWAI_GENERAL_CONFIG_DEFAULT(tx, rx, mode_) \
    ((twai_general_config_t){ .mode = (mode_), .tx_pin = (tx), .rx_pin = (rx) })

typedef struct {
    uint32_t brp;
    uint8_t  tseg_1;
    uint8_t  tseg_2;
    uint8_t  sjw;
    bool     triple_sampling;
} twai_timing_config_t;

#define TWAI_TIMING_CONFIG_500KBITS() ((twai_timing_config_t){ 0 })
#define TWAI_TIMING_CONFIG_250KBITS() ((twai_timing_config_t){ 0 })

typedef struct {
    uint32_t acceptance_code;
    uint32_t acceptance_mask;
    bool     single_filter;
} twai_filter_config_t;

#define TWAI_FILTER_CONFIG_ACCEPT_ALL() ((twai_filter_config_t){ 0 })

esp_err_t twai_driver_install(const twai_general_config_t *g_config,
                             const twai_timing_config_t *t_config,
                             const twai_filter_config_t *f_config);
esp_err_t twai_driver_uninstall(void);
esp_err_t twai_start(void);
esp_err_t twai_stop(void);
esp_err_t twai_transmit(const twai_message_t *message, TickType_t ticks_to_wait);
esp_err_t twai_receive(twai_message_t *message, TickType_t ticks_to_wait);

#endif /* STUB_TWAI_H */
