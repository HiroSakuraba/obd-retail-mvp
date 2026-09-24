/* obd_allowlist.c - read-only OBD command allowlist.
 *
 * This is the entire attack surface of the dongle's CAN side: a request is
 * either mapped to one of six read-only OBD services or refused. There is
 * no code path that transmits an arbitrary CAN frame; adding one would be a
 * deliberate product decision, not an accident of this file.
 *
 * Security definition: the device may transmit only allowlisted diagnostic
 * requests plus the transport-layer frames strictly necessary to receive
 * their responses (ISO-TP flow control). It never transmits anything else.
 */

#include "obd_allowlist.h"

#include <ctype.h>
#include <string.h>

/* Mode-01 PIDs the MVP diagnostic graphs actually need, plus a small set of
 * standard drivability signals. Everything else is refused. Stored as
 * two-character uppercase hex strings. */
static const char *const PID_ALLOWLIST[] = {
    "04", /* calculated engine load */
    "05", /* coolant temperature */
    "06", /* short-term fuel trim bank 1 */
    "07", /* long-term fuel trim bank 1 */
    "08", /* short-term fuel trim bank 2 */
    "09", /* long-term fuel trim bank 2 */
    "0B", /* intake manifold pressure */
    "0C", /* engine RPM */
    "0D", /* vehicle speed */
    "0E", /* timing advance */
    "0F", /* intake air temperature */
    "10", /* MAF air flow rate */
    "11", /* throttle position */
    "14", /* O2 sensor 1 voltage (bank 1, sensor 1) — needed by P0133/P0420 */
    "15", /* O2 sensor 2 voltage (bank 1, sensor 2) — needed by P0420 */
    "1F", /* run time since engine start */
    "21", /* distance traveled with MIL on */
    "2F", /* fuel tank level input */
    "33", /* barometric pressure */
    "42", /* control module voltage */
    "43", /* absolute load value */
    "44", /* commanded equivalence ratio */
    "45", /* relative throttle position */
    "46", /* ambient air temperature */
    "47", /* absolute throttle position B */
    "4C", /* commanded throttle actuator */
};
#define PID_ALLOWLIST_LEN (sizeof PID_ALLOWLIST / sizeof PID_ALLOWLIST[0])

bool pid_allowlisted(const char *pid_hex)
{
    char norm[3];
    size_t i;

    if (!pid_hex || strlen(pid_hex) != 2)
        return false;
    norm[0] = (char)toupper((unsigned char)pid_hex[0]);
    norm[1] = (char)toupper((unsigned char)pid_hex[1]);
    norm[2] = '\0';
    for (i = 0; i < PID_ALLOWLIST_LEN; i++) {
        if (strcmp(norm, PID_ALLOWLIST[i]) == 0)
            return true;
    }
    return false;
}

obd_op_t obd_op_from_request(const ble_request_t *req)
{
    if (!req)
        return OP_INVALID;
    if (strcmp(req->op, "read_vin") == 0)
        return OP_READ_VIN;
    if (strcmp(req->op, "read_dtcs_confirmed") == 0)
        return OP_READ_CURRENT_DTCS;
    if (strcmp(req->op, "read_dtcs_pending") == 0)
        return OP_READ_PENDING_DTCS;
    if (strcmp(req->op, "read_dtcs_permanent") == 0)
        return OP_READ_PERMANENT_DTCS;
    if (strcmp(req->op, "read_pid") == 0)
        return OP_READ_PID;
    if (strcmp(req->op, "read_freeze_frame") == 0)
        return OP_READ_FREEZE_FRAME;
    return OP_INVALID;
}

uint8_t obd_service_for(obd_op_t op)
{
    switch (op) {
    case OP_READ_VIN:           return 0x09; /* + PID 0x02 */
    case OP_READ_CURRENT_DTCS:  return 0x03;
    case OP_READ_PENDING_DTCS:  return 0x07;
    case OP_READ_PERMANENT_DTCS:return 0x0A;
    case OP_READ_PID:           return 0x01; /* + PID */
    case OP_READ_FREEZE_FRAME:  return 0x02; /* + PID (frozen at DTC set) */
    default:                    return 0x00;
    }
}

obd_result_t obd_dispatch(const ble_request_t *req, obd_op_t *op_out)
{
    obd_op_t op;

    if (!req)
        return OBD_ERR_BAD_REQUEST;
    if (req->version != BLE_PROTO_VERSION)
        return OBD_ERR_VERSION;

    op = obd_op_from_request(req);
    if (op == OP_INVALID)
        return OBD_ERR_FORBIDDEN_COMMAND;
    /* Both live PID reads and freeze-frame reads are gated on the same
       Mode-01 PID allowlist: a frozen PID is no more sensitive than a
       live one, and the set the graphs need is identical. */
    if ((op == OP_READ_PID || op == OP_READ_FREEZE_FRAME)
        && !pid_allowlisted(req->pid))
        return OBD_ERR_PID_NOT_ALLOWED;

    if (op_out)
        *op_out = op;
    return OBD_OK;
}

const char *obd_error_code(obd_result_t r)
{
    switch (r) {
    case OBD_OK:                    return "OK";
    case OBD_ERR_FORBIDDEN_COMMAND: return "ERROR_FORBIDDEN_COMMAND";
    case OBD_ERR_PID_NOT_ALLOWED:   return "ERROR_PID_NOT_ALLOWED";
    case OBD_ERR_VERSION:           return "ERROR_VERSION";
    case OBD_ERR_BAD_REQUEST:       return "ERROR_BAD_REQUEST";
    default:                        return "ERROR_UNKNOWN";
    }
}
