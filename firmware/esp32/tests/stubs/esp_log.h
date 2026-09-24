#ifndef STUB_ESP_LOG_H
#define STUB_ESP_LOG_H
/* Host stub for ESP-IDF esp_log.h: log macros print to stderr so
 * stub-compiled code links and runs on the host if ever executed. */

#include <stdio.h>

#define ESP_LOGE(tag, fmt, ...) \
    fprintf(stderr, "[E][%s] " fmt "\n", tag, ##__VA_ARGS__)
#define ESP_LOGW(tag, fmt, ...) \
    fprintf(stderr, "[W][%s] " fmt "\n", tag, ##__VA_ARGS__)
#define ESP_LOGI(tag, fmt, ...) \
    fprintf(stderr, "[I][%s] " fmt "\n", tag, ##__VA_ARGS__)
#define ESP_LOGD(tag, fmt, ...) \
    fprintf(stderr, "[D][%s] " fmt "\n", tag, ##__VA_ARGS__)

#endif /* STUB_ESP_LOG_H */
