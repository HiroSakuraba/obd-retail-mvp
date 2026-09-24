/* can_transport.h - abstract CAN frame transport for the OBD client.
 *
 * The OBD client (obd_client.c) sees CAN frames through this interface.
 * Two implementations exist:
 *   - twai_transport.c : ESP32 TWAI peripheral (real hardware). Opens with
 *                        automatic protocol-variant detection across the
 *                        four CAN OBD variants (11/29-bit x 500/250 kbit/s).
 *   - tcp_transport.c  : host test shim that forwards frames over TCP to
 *                        simulator/can_ecu_sim.py (bench validation).
 *
 * The interface is deliberately frame-oriented and read-mostly: the
 * client may transmit only allowlisted diagnostic requests (checked in
 * obd_client.c before transmit) and ISO-TP flow-control frames.
 */
#ifndef CAN_TRANSPORT_H
#define CAN_TRANSPORT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* CAN OBD protocol variants. The transport negotiates one of these at
   open() time by probing; the client derives its request/response CAN
   ids from it (see obd_client.c). */
typedef enum {
    CAN_VAR_11B_500K = 0,   /* classic: 11-bit ids, 500 kbit/s */
    CAN_VAR_29B_500K,       /* 29-bit ids, 500 kbit/s */
    CAN_VAR_11B_250K,       /* 11-bit ids, 250 kbit/s (older vehicles) */
    CAN_VAR_29B_250K        /* 29-bit ids, 250 kbit/s */
} can_variant_t;

/* OBD-II CAN identifiers, per variant. */
#define CAN_ID_FUNC_REQUEST_11B  0x7DFu       /* functional request */
#define CAN_ID_PHYS_REQUEST_11B  0x7E0u       /* physical request, ECU #1 */
#define CAN_ID_RESP_BASE_11B     0x7E8u       /* responses 0x7E8..0x7EF */
#define CAN_ID_RESP_LAST_11B     0x7EFu

#define CAN_ID_FUNC_REQUEST_29B  0x18DB33F1u  /* functional request */
#define CAN_ID_RESP_BASE_29B     0x18DAF100u  /* responses 0x18DAF100.. */
#define CAN_ID_RESP_LAST_29B     0x18DAF1FFu  /* ..0x18DAF1FF (ECU addr) */

/* Backwards-compatible aliases for the 11-bit (most common) variant. */
#define CAN_ID_FUNC_REQUEST  CAN_ID_FUNC_REQUEST_11B
#define CAN_ID_PHYS_REQUEST  CAN_ID_PHYS_REQUEST_11B
#define CAN_ID_RESP_BASE     CAN_ID_RESP_BASE_11B
#define CAN_ID_RESP_LAST     CAN_ID_RESP_LAST_11B

typedef struct {
    uint32_t id;       /* 11-bit or 29-bit CAN identifier (see extd) */
    bool     extd;     /* true = 29-bit identifier */
    uint8_t  dlc;      /* 0..8 */
    uint8_t  data[8];
} can_frame_t;

/* Human-readable variant name for logging. */
static inline const char *can_variant_name(can_variant_t v)
{
    switch (v) {
    case CAN_VAR_11B_500K: return "11-bit 500 kbit/s";
    case CAN_VAR_29B_500K: return "29-bit 500 kbit/s";
    case CAN_VAR_11B_250K: return "11-bit 250 kbit/s";
    case CAN_VAR_29B_250K: return "29-bit 250 kbit/s";
    default:              return "unknown";
    }
}

typedef struct can_transport can_transport_t;

struct can_transport {
    void *ctx;
    can_variant_t variant;  /* negotiated at open(); fixed afterwards */
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
