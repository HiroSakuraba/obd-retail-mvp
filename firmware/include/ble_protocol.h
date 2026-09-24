#ifndef BLE_PROTOCOL_H
#define BLE_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Protocol version. A dongle only answers requests whose "v" matches. */
#define BLE_PROTO_VERSION 1u

/* Capability flags advertised by the dongle (read-only by design). */
#define BLE_CAP_READ_VIN   (1u << 0)
#define BLE_CAP_READ_DTCS  (1u << 1)
#define BLE_CAP_READ_PID   (1u << 2)
#define BLE_CAP_FREEZE     (1u << 3)

#define BLE_ID_MAX    40
#define BLE_OP_MAX    32
#define BLE_PID_MAX    8

typedef struct {
    uint32_t version;          /* request "v" */
    char id[BLE_ID_MAX];       /* request "id", echoed in every reply */
    char op[BLE_OP_MAX];       /* e.g. "read_vin", "read_pid" */
    char pid[BLE_PID_MAX];     /* hex PID string for "read_pid", else "" */
} ble_request_t;

/* Reply envelope written by proto_build_response / proto_build_error. */
#define BLE_RESP_MAX 512

/* Parse one JSON request line. Returns 0 on success, -1 on malformed JSON,
   -2 on version mismatch. */
int proto_parse_request(const char *json, size_t len, ble_request_t *out);

/* Build {"v":1,"id":...,"ok":true,"data":{...}} into buf. data_json must
   already be a JSON object fragment, e.g. "{\"pid\":\"0C\",\"value\":1726}". */
int proto_build_response(const ble_request_t *req, const char *data_json,
                         char *buf, size_t buflen);

/* Build {"v":1,"id":...,"ok":false,"error":"..."} into buf. */
int proto_build_error(const ble_request_t *req, const char *error_code,
                      char *buf, size_t buflen);

/* Base64 (RFC 4648, no line breaks) for chunked envelopes. */
/* Encoded length of inlen bytes, including the NUL terminator. */
size_t proto_b64_len(size_t inlen);
/* Encode in[0..inlen) into out (outlen bytes, incl. NUL).
   Returns 0 on success, -1 on NULL args or short buffer. */
int proto_b64_encode(const uint8_t *in, size_t inlen,
                     char *out, size_t outlen);

#ifdef __cplusplus
}
#endif

#endif /* BLE_PROTOCOL_H */
