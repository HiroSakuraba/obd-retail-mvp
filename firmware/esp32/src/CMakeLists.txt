# Main component: the sources in this dir plus the shared firmware
# library in firmware/src (portable C: ISO-TP, allowlist, BLE protocol).
idf_component_register(
    SRCS
        "main_esp32.c"
        "twai_transport.c"
        "ble_gatt.c"
        "obd_client.c"
        "../../src/iso15765.c"
        "../../src/obd_allowlist.c"
        "../../src/ble_protocol.c"
    INCLUDE_DIRS
        "../include"
        "../../include"
    REQUIRES
        driver
        esp_timer
        nvs_flash
        bt
)
