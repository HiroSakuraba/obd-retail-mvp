#ifndef STUB_FREERTOS_H
#define STUB_FREERTOS_H
/* Host stub for FreeRTOS.h: only what the firmware sources use. */

#include <stdint.h>

typedef uint32_t TickType_t;

#define portTICK_PERIOD_MS 1u
#define pdMS_TO_TICKS(ms) ((TickType_t)((ms) / portTICK_PERIOD_MS))

#endif /* STUB_FREERTOS_H */
