#ifndef STUB_ESP_ERR_H
#define STUB_ESP_ERR_H
/* Host stub for ESP-IDF esp_err.h: declarations only, for CI
 * stub-compilation of the ESP32 app sources. No behavior. */

typedef int esp_err_t;

#define ESP_OK                              0
#define ESP_FAIL                            1
#define ESP_ERR_NO_MEM                      2
#define ESP_ERR_INVALID_ARG                 3
#define ESP_ERR_TIMEOUT                     4
#define ESP_ERR_NVS_NO_FREE_PAGES           5
#define ESP_ERR_NVS_NEW_VERSION_FOUND       6

#endif /* STUB_ESP_ERR_H */
