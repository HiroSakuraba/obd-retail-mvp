#ifndef OBD_ALLOWLIST_H
#define OBD_ALLOWLIST_H

#include <stdbool.h>
#include <stdint.h>

#include "ble_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Read-only operations the dongle will ever perform. There is deliberately
   no raw-CAN-write operation and no API to add one: a retail giveaway
   dongle must not become a generic vehicle command-injection interface. */
typedef enum {
    OP_READ_VIN,
    OP_READ_CURRENT_DTCS,
    OP_READ_PENDING_DTCS,
    OP_READ_PERMANENT_DTCS,
    OP_READ_PID,
    OP_INVALID
} obd_op_t;

typedef enum {
    OBD_OK = 0,
    OBD_ERR_FORBIDDEN_COMMAND,  /* op unknown or not allowlisted */
    OBD_ERR_PID_NOT_ALLOWED,    /* op is read_pid but pid not allowlisted */
    OBD_ERR_VERSION,            /* protocol version mismatch */
    OBD_ERR_BAD_REQUEST         /* malformed request */
} obd_result_t;

/* Map a parsed request to an allowlisted op (OP_INVALID if not allowed). */
obd_op_t obd_op_from_request(const ble_request_t *req);

/* OBD service byte to send on the CAN bus for an op (0x01/0x03/0x07/0x09/0x0A).
   Returns 0 for OP_INVALID. */
uint8_t obd_service_for(obd_op_t op);

/* True if the two-hex-digit PID string is on the read allowlist. */
bool pid_allowlisted(const char *pid_hex);

/* Full dispatch: parse already done by caller. Validates version, op and pid,
   and returns the disposition. The caller turns non-OK results into
   ERROR_* BLE replies and never touches the CAN bus for them. */
obd_result_t obd_dispatch(const ble_request_t *req, obd_op_t *op_out);

const char *obd_error_code(obd_result_t r);

#ifdef __cplusplus
}
#endif

#endif /* OBD_ALLOWLIST_H */
