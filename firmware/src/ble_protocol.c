/* ble_protocol.c - JSON request/response framing for the dongle.
 *
 * Minimal, allocation-free JSON handling sized for a BLE MCU: the parser
 * extracts only the fields the protocol needs ("v", "id", "op", "pid").
 * Production firmware should prefer CBOR/MessagePack; JSON is used here
 * because it is easy to debug on the bench.
 */

#include "ble_protocol.h"

#include <stdio.h>
#include <string.h>

/* Copy the string value of "key" from a flat JSON object into buf.
 * Returns 0 on success, -1 if the key is missing or not a string. */
static int json_get_string(const char *json, size_t len,
                           const char *key, char *buf, size_t buflen)
{
    char needle[48];
    const char *p, *q, *end;
    size_t klen;

    if (snprintf(needle, sizeof needle, "\"%s\"", key) < 0)
        return -1;
    klen = strlen(needle);
    end = json + len;
    p = json;
    while (p + klen < end) {
        if (memcmp(p, needle, klen) == 0) {
            q = p + klen;
            while (q < end && (*q == ' ' || *q == '\t' || *q == ':'))
                q++;
            if (q >= end || *q != '"')
                return -1;
            q++;
            p = q;
            while (q < end && *q != '"')
                q++;
            if (q >= end)
                return -1;
            if ((size_t)(q - p) >= buflen)
                return -1;
            memcpy(buf, p, (size_t)(q - p));
            buf[q - p] = '\0';
            return 0;
        }
        p++;
    }
    return -1;
}

/* Copy the numeric value of "key" into *out. Returns 0 on success. */
static int json_get_uint(const char *json, size_t len,
                         const char *key, uint32_t *out)
{
    char needle[48];
    const char *p, *q, *end;
    unsigned long v = 0;

    if (snprintf(needle, sizeof needle, "\"%s\"", key) < 0)
        return -1;
    end = json + len;
    p = json;
    while (p + strlen(needle) < end) {
        if (memcmp(p, needle, strlen(needle)) == 0) {
            q = p + strlen(needle);
            while (q < end && (*q == ' ' || *q == '\t' || *q == ':'))
                q++;
            if (q >= end || *q < '0' || *q > '9')
                return -1;
            while (q < end && *q >= '0' && *q <= '9') {
                v = v * 10u + (unsigned)(*q - '0');
                q++;
            }
            *out = (uint32_t)v;
            return 0;
        }
        p++;
    }
    return -1;
}

int proto_parse_request(const char *json, size_t len, ble_request_t *out)
{
    uint32_t v;

    if (!json || !out || len == 0)
        return -1;
    memset(out, 0, sizeof *out);

    if (json_get_uint(json, len, "v", &v) != 0)
        return -1;
    if (v != BLE_PROTO_VERSION)
        return -2;
    out->version = v;

    if (json_get_string(json, len, "id", out->id, sizeof out->id) != 0)
        return -1;
    if (json_get_string(json, len, "op", out->op, sizeof out->op) != 0)
        return -1;
    /* "pid" is optional; absent means "". */
    json_get_string(json, len, "pid", out->pid, sizeof out->pid);
    return 0;
}

int proto_build_response(const ble_request_t *req, const char *data_json,
                         char *buf, size_t buflen)
{
    int n;

    if (!req || !data_json || !buf || buflen == 0)
        return -1;
    n = snprintf(buf, buflen,
                 "{\"v\":%u,\"id\":\"%s\",\"ok\":true,\"data\":%s}",
                 BLE_PROTO_VERSION, req->id, data_json);
    if (n < 0 || (size_t)n >= buflen)
        return -1;
    return 0;
}

int proto_build_error(const ble_request_t *req, const char *error_code,
                      char *buf, size_t buflen)
{
    int n;
    const char *id;

    if (!error_code || !buf || buflen == 0)
        return -1;
    id = (req && req->id[0]) ? req->id : "unknown";
    n = snprintf(buf, buflen,
                 "{\"v\":%u,\"id\":\"%s\",\"ok\":false,\"error\":\"%s\"}",
                 BLE_PROTO_VERSION, id, error_code);
    if (n < 0 || (size_t)n >= buflen)
        return -1;
    return 0;
}

/* ------------------------------------------------------------------ */
/* base64 (RFC 4648, no line breaks) for BLE chunking envelopes.      */

static const char b64_table[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

size_t proto_b64_len(size_t inlen)
{
    /* 4 chars per 3 bytes, rounded up, plus NUL. */
    return ((inlen + 2) / 3) * 4 + 1;
}

int proto_b64_encode(const uint8_t *in, size_t inlen,
                     char *out, size_t outlen)
{
    size_t need, o = 0, i;

    if (!in || !out || outlen == 0)
        return -1;
    need = proto_b64_len(inlen);
    if (outlen < need)
        return -1;
    for (i = 0; i < inlen; i += 3) {
        uint32_t n = (uint32_t)in[i] << 16;
        size_t rem = inlen - i;
        if (rem > 1) n |= (uint32_t)in[i + 1] << 8;
        if (rem > 2) n |= in[i + 2];
        out[o++] = b64_table[(n >> 18) & 0x3F];
        out[o++] = b64_table[(n >> 12) & 0x3F];
        out[o++] = (rem > 1) ? b64_table[(n >> 6) & 0x3F] : '=';
        out[o++] = (rem > 2) ? b64_table[n & 0x3F] : '=';
    }
    out[o] = '\0';
    return 0;
}
