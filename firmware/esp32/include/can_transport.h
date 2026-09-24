/* can_transport.h - abstract CAN frame transport for the OBD client.
 *
 * The OBD client (obd_client.c) only ever sees 11-bit CAN frames through
 * this interface. Two implementations exist:
 *   - twai_transport.c : ESP32 TWAI peripheral (real hardware, 500 kbit/s)
 *   - tcp_transport.c  : host test shim that forwards frames over TCP to
 *                        simulator/can_ecu_sim.py (bench validation)
 *
 * The interface is deliberately frame-oriented and read-mostly: the
 * client may transmit only allowlisted diagnostic requests (checked in
 * obd_client.c before transmit) and ISO-TP flow-control frames.
 */
#ifndef CAN_TRANSPORT_H
#define CAN_TRANSPORT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* OBD-II 11-bit CAN identifiers used by this firmware. */
#define CAN_ID_FUNC_REQUEST  0x7DFu  /* functional broadcast request */
#define CAN_ID_PHYS_REQUEST  0x7E0u  /* physical request, ECU #1      */
#define CAN_ID_RESP_BASE     0x7E8u  /* responses 0x7E8..0x7EF        */
#define CAN_ID_RESP_LAST     0x7EFu

typedef struct {
    uint32_t id;       /* 11-bit CAN identifier */
    uint8_t  dlc;      /* 0..8 */
    uint8_t  data[8];
} can_frame_t;

typedef struct can_transport can_transport_t;

struct can_transport {
    void *ctx;
    /* Transmit one frame. Returns 0 on success, negative on error. */
    int (*send)(can_transport_t *t, const can_frame_t *f);
    /* Receive one frame, waiting up to timeout_ms. Returns 0 on success,
       1 on timeout, negative on error. */
    int (*recv)(can_transport_t *t, can_frame_t *f, uint32_t timeout_ms);
    /* Milliseconds since an arbitrary epoch (for deadlines). */
    uint32_t (*millis)(can_transport_t *t);
    /* Release transport resources. */
    void (*close)(can_transport_t *t);
};

#ifdef __cplusplus
}
#endif

#endif /* CAN_TRANSPORT_H */
