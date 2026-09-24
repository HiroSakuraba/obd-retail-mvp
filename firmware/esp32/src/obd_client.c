/* obd_client.c - read-only OBD-II scan client.
 *
 * Portable C99: no OS calls, no dynamic allocation. Time comes from the
 * transport's millis(); I/O is can_transport_t frames. The identical file
 * compiles for the ESP32 (twai_transport.c) and for the host test harness
 * (tcp_transport.c against simulator/can_ecu_sim.py).
 *
 * Transmit discipline (enforced here, not just documented):
 *  - requests go to the functional id of the negotiated CAN variant
 *    (0x7DF for 11-bit, 0x18DB33F1 for 29-bit) and carry only allowlisted
 *    service bytes (01/02/03/07/09/0A) with allowlisted PIDs;
 *  - the only other transmitted frames are ISO-TP Flow Control (0x30)
 *    addressed at the responding ECU's physical request id, which is
 *    transport-layer plumbing strictly necessary to receive a multi-frame
 *    response (see firmware/src/iso15765.c).
 */

#include "obd_client.h"

#include <stdio.h>
#include <string.h>

#include "iso15765.h"
#include "obd_allowlist.h"

/* ---- variant id mapping -------------------------------------------- */

typedef struct {
    uint32_t func_id;
    bool     func_extd;
    uint32_t resp_base;
    uint32_t resp_last;
} obd_ids_t;

static obd_ids_t obd_ids_for(can_variant_t v)
{
    obd_ids_t ids;
    if (v == CAN_VAR_29B_500K || v == CAN_VAR_29B_250K) {
        ids.func_id = CAN_ID_FUNC_REQUEST_29B;
        ids.func_extd = true;
        ids.resp_base = CAN_ID_RESP_BASE_29B;
        ids.resp_last = CAN_ID_RESP_LAST_29B;
    } else {
        ids.func_id = CAN_ID_FUNC_REQUEST_11B;
        ids.func_extd = false;
        ids.resp_base = CAN_ID_RESP_BASE_11B;
        ids.resp_last = CAN_ID_RESP_LAST_11B;
    }
    return ids;
}

/* Physical request id paired with a responding ECU, for ISO-TP flow
   control: 0x7E8 -> 0x7E0 (11-bit); 0x18DAF1xx -> 0x18DAxxF1 (29-bit). */
static uint32_t obd_phys_for(obd_ids_t ids, uint32_t responder, bool *extd)
{
    if (ids.func_extd) {
        *extd = true;
        return 0x18DA0000u | ((responder & 0xFFu) << 8) | 0xF1u;
    }
    *extd = false;
    return CAN_ID_PHYS_REQUEST_11B + (responder - CAN_ID_RESP_BASE_11B);
}

/* ---- default scan PID list ---------------------------------------- */

const uint8_t OBD_DEFAULT_PIDS[] = {
    0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0B, 0x0C, 0x0D, 0x0E,
    0x0F, 0x10, 0x11, 0x14, 0x15, 0x1F, 0x21, 0x2F, 0x33, 0x42,
};
const int OBD_DEFAULT_PIDS_LEN =
    (int)(sizeof OBD_DEFAULT_PIDS / sizeof OBD_DEFAULT_PIDS[0]);

/* ---- timeouts ------------------------------------------------------ */

#define TP_FIRST_FRAME_MS  1000u  /* ECU response latency budget */
#define TP_NEXT_FRAME_MS    250u  /* consecutive-frame gap budget */
#define TP_MAX_PAYLOAD      256u

/* ---- ISO-TP transact ----------------------------------------------- */

/* Send an ISO-TP-encoded request to the variant's functional id and
 * collect the reassembled response payload from the first ECU that
 * answers on the variant's response range. Returns payload length,
 * OBDC_ERR_TIMEOUT, or OBDC_ERR_PROTOCOL. */
static int tp_transact(can_transport_t *t,
                       const uint8_t *req, size_t req_len,
                       uint8_t *resp, size_t resp_cap)
{
    uint8_t frames[ISOTP_MAX_FRAMES][ISOTP_FRAME_LEN];
    int nframes, i;
    can_frame_t f;
    uint32_t deadline;
    uint32_t responder = 0;          /* 0 = none yet */
    uint8_t payload[TP_MAX_PAYLOAD];
    size_t total = 0, got = 0;
    uint8_t expect_seq = 1;
    int rc;
    obd_ids_t ids;

    if (!t || !req || req_len == 0 || req_len > 0xFFF || !resp ||
        resp_cap == 0)
        return OBDC_ERR_PROTOCOL;

    ids = obd_ids_for(t->variant);

    nframes = isotp_encode(req, req_len, frames, ISOTP_MAX_FRAMES);
    if (nframes < 0)
        return OBDC_ERR_PROTOCOL;

    for (i = 0; i < nframes; i++) {
        can_frame_t tx;
        tx.id = ids.func_id;
        tx.extd = ids.func_extd;
        tx.dlc = 8;
        memcpy(tx.data, frames[i], 8);
        if (t->send(t, &tx) != 0)
            return OBDC_ERR_TIMEOUT;
    }

    deadline = t->millis(t) + TP_FIRST_FRAME_MS;
    for (;;) {
        uint32_t now = t->millis(t);
        uint32_t wait = (deadline > now) ? (deadline - now) : 0;
        rc = t->recv(t, &f, wait);
        if (rc == 1)
            return OBDC_ERR_TIMEOUT;      /* deadline hit */
        if (rc != 0)
            return OBDC_ERR_TIMEOUT;      /* transport error: fail closed */
        if (f.dlc == 0)
            continue;
        if (f.extd != ids.func_extd ||
            f.id < ids.resp_base || f.id > ids.resp_last)
            continue;                    /* not an OBD response id */
        if (responder != 0 && f.id != responder)
            continue;                    /* first responder wins */

        {
            uint8_t pci_type = (uint8_t)(f.data[0] >> 4);
            if (pci_type == 0x0) {        /* single frame */
                size_t len = f.data[0] & 0x0F;
                if (len > 7 || len + 1 > f.dlc)
                    return OBDC_ERR_PROTOCOL;
                if (len > resp_cap)
                    return OBDC_ERR_PROTOCOL;
                memcpy(resp, f.data + 1, len);
                return (int)len;
            }
            if (pci_type == 0x1) {        /* first frame */
                uint8_t fc[ISOTP_FRAME_LEN];
                can_frame_t fctx;
                int announced;
                if (responder != 0)
                    return OBDC_ERR_PROTOCOL;  /* second FF: give up */
                responder = f.id;
                announced = isotp_on_first_frame(f.data, fc);
                if (announced < 0 || (size_t)announced > TP_MAX_PAYLOAD)
                    return OBDC_ERR_PROTOCOL;
                total = (size_t)announced;
                if (total > resp_cap)
                    return OBDC_ERR_PROTOCOL;
                /* Flow Control goes to the physical request id paired
                   with the responding ECU (0x7E8 -> 0x7E0, ...). */
                fctx.id = obd_phys_for(ids, responder, &fctx.extd);
                fctx.dlc = 8;
                memcpy(fctx.data, fc, 8);
                if (t->send(t, &fctx) != 0)
                    return OBDC_ERR_TIMEOUT;
                got = (total > 6) ? 6 : total;
                memcpy(payload, f.data + 2, got);
                expect_seq = 1;
                deadline = t->millis(t) + TP_NEXT_FRAME_MS;
                continue;
            }
            if (pci_type == 0x2) {        /* consecutive frame */
                if (responder == 0 || f.id != responder)
                    continue;
                if ((f.data[0] & 0x0F) != (expect_seq & 0x0F))
                    return OBDC_ERR_PROTOCOL;
                {
                    size_t chunk = total - got;
                    if (chunk > 7)
                        chunk = 7;
                    if (chunk + 1 > f.dlc)
                        return OBDC_ERR_PROTOCOL;
                    memcpy(payload + got, f.data + 1, chunk);
                    got += chunk;
                }
                expect_seq++;
                if (got >= total) {
                    memcpy(resp, payload, total);
                    return (int)total;
                }
                deadline = t->millis(t) + TP_NEXT_FRAME_MS;
                continue;
            }
            /* PCI 0x3 (flow control) from an ECU is not a response. */
        }
    }
}

/* ---- service allowlist gate ---------------------------------------- */

/* Services this client may ever put on the bus. Mirrors the firmware
   allowlist; checked on the transmit path so a caller bug cannot turn
   the dongle into a generic transmitter. */
static bool service_tx_allowed(uint8_t service)
{
    return service == OBD_SVC_CURRENT_DATA ||
           service == OBD_SVC_FREEZE_FRAME_DATA ||
           service == OBD_SVC_STORED_DTC ||
           service == OBD_SVC_PENDING_DTC ||
           service == OBD_SVC_VEHICLE_INFO ||
           service == OBD_SVC_PERMANENT_DTC;
}

/* ---- VIN ------------------------------------------------------------ */

static bool vin_char_ok(char c)
{
    /* ISO 3779: digits + A-Z except I, O, Q. */
    return (c >= '0' && c <= '9') ||
           (c >= 'A' && c <= 'H') || (c >= 'J' && c <= 'N') ||
           (c >= 'P' && c <= 'R') || (c >= 'S' && c <= 'Z');
}

int obd_read_vin(can_transport_t *t, char vin_out[OBD_VIN_LEN + 1])
{
    const uint8_t req[2] = { OBD_SVC_VEHICLE_INFO, 0x02 };
    uint8_t resp[TP_MAX_PAYLOAD];
    int len, i;

    if (!service_tx_allowed(req[0]))
        return OBDC_ERR_FORBIDDEN;
    len = tp_transact(t, req, sizeof req, resp, sizeof resp);
    if (len < 0)
        return len;
    /* Positive response: 49 02 <msg-count> <17 ASCII>. */
    if (len < 20 || resp[0] != 0x49 || resp[1] != 0x02)
        return OBDC_ERR_PROTOCOL;
    for (i = 0; i < OBD_VIN_LEN; i++) {
        char c = (char)resp[3 + i];
        if (!vin_char_ok(c))
            return OBDC_ERR_PROTOCOL;
        vin_out[i] = c;
    }
    vin_out[OBD_VIN_LEN] = '\0';
    return OBDC_OK;
}

/* ---- DTCs ----------------------------------------------------------- */

static void dtc_decode(uint8_t b1, uint8_t b2, char out[OBD_DTC_STRLEN])
{
    static const char sys[4] = { 'P', 'C', 'B', 'U' };
    /* SAE J2012: bits 7-6 = system, bits 5-4 + nibbles = code digits. */
    snprintf(out, OBD_DTC_STRLEN, "%c%X%X%X%X",
             sys[(b1 >> 6) & 0x03], (b1 >> 4) & 0x03,
             b1 & 0x0F, (b2 >> 4) & 0x0F, b2 & 0x0F);
}

int obd_read_dtcs(can_transport_t *t, uint8_t service,
                  char codes_out[][OBD_DTC_STRLEN], int cap)
{
    uint8_t resp[TP_MAX_PAYLOAD];
    int len, n = 0, i;
    uint8_t want_sid;

    if (!service_tx_allowed(service) || cap < 0)
        return OBDC_ERR_FORBIDDEN;
    if (service != OBD_SVC_STORED_DTC &&
        service != OBD_SVC_PENDING_DTC &&
        service != OBD_SVC_PERMANENT_DTC)
        return OBDC_ERR_FORBIDDEN;

    len = tp_transact(t, &service, 1, resp, sizeof resp);
    if (len < 0)
        return len;
    want_sid = (uint8_t)(service + 0x40);  /* 03->43, 07->47, 0A->4A */
    if (len < 1 || resp[0] != want_sid)
        return OBDC_ERR_PROTOCOL;

    for (i = 1; i + 1 < len && n < cap; i += 2) {
        if (resp[i] == 0x00 && resp[i + 1] == 0x00)
            break;                        /* terminator / padding */
        if (n >= OBD_MAX_DTCS)
            break;
        dtc_decode(resp[i], resp[i + 1], codes_out[n]);
        n++;
    }
    return n;
}

/* ---- live PIDs ------------------------------------------------------ */

static int pid_decode(uint8_t pid, const uint8_t *d, size_t n, double *v)
{
#define NEED(k) do { if (n < (k)) return OBDC_ERR_PROTOCOL; } while (0)
    switch (pid) {
    case 0x04: NEED(1); *v = d[0] * 100.0 / 255.0; return OBDC_OK;
    case 0x05: NEED(1); *v = (double)((int)d[0] - 40); return OBDC_OK;
    case 0x06: case 0x07: case 0x08: case 0x09:
        NEED(1); *v = ((int)d[0] - 128) * 100.0 / 128.0; return OBDC_OK;
    case 0x0B: NEED(1); *v = (double)d[0]; return OBDC_OK;
    case 0x0C: NEED(2); *v = (d[0] * 256u + d[1]) / 4.0; return OBDC_OK;
    case 0x0D: NEED(1); *v = (double)d[0]; return OBDC_OK;
    case 0x0E: NEED(1); *v = d[0] / 2.0 - 64.0; return OBDC_OK;
    case 0x0F: NEED(1); *v = (double)((int)d[0] - 40); return OBDC_OK;
    case 0x10: NEED(2); *v = (d[0] * 256u + d[1]) / 100.0; return OBDC_OK;
    case 0x11: NEED(1); *v = d[0] * 100.0 / 255.0; return OBDC_OK;
    case 0x14: case 0x15:
        NEED(2); *v = d[0] / 200.0; return OBDC_OK;  /* sensor voltage */
    case 0x1F: NEED(2); *v = (double)(d[0] * 256u + d[1]); return OBDC_OK;
    case 0x21: NEED(2); *v = (double)(d[0] * 256u + d[1]); return OBDC_OK;
    case 0x2F: NEED(1); *v = d[0] * 100.0 / 255.0; return OBDC_OK;
    case 0x33: NEED(1); *v = (double)d[0]; return OBDC_OK;
    case 0x42: NEED(2); *v = (d[0] * 256u + d[1]) / 1000.0; return OBDC_OK;
    default: return OBDC_ERR_FORBIDDEN;
    }
#undef NEED
}

int obd_read_pid(can_transport_t *t, uint8_t pid, double *value_out)
{
    char hex[3];
    const uint8_t req[2] = { OBD_SVC_CURRENT_DATA, pid };
    uint8_t resp[TP_MAX_PAYLOAD];
    int len;

    if (!service_tx_allowed(req[0]))
        return OBDC_ERR_FORBIDDEN;
    /* Allowlist gate BEFORE any frame is transmitted. */
    snprintf(hex, sizeof hex, "%02X", pid);
    if (!pid_allowlisted(hex))
        return OBDC_ERR_FORBIDDEN;

    len = tp_transact(t, req, sizeof req, resp, sizeof resp);
    if (len < 0)
        return len;                       /* timeout: ECU stayed silent */
    if (len < 2 || resp[0] != 0x41 || resp[1] != pid)
        return OBDC_ERR_PROTOCOL;
    return pid_decode(pid, resp + 2, (size_t)(len - 2), value_out);
}

/* ---- freeze frame (Mode 02) ---------------------------------------- */

/* Read one frozen PID (service 02, frame 0): the snapshot the ECU stored
   when the DTC was set. Same decode table as live PIDs; the PID allowlist
   gate applies before any frame is transmitted. */
int obd_read_freeze_frame(can_transport_t *t, uint8_t pid, double *value_out)
{
    char hex[3];
    const uint8_t req[3] = { OBD_SVC_FREEZE_FRAME_DATA, pid, 0x00 };
    uint8_t resp[TP_MAX_PAYLOAD];
    int len;

    if (!service_tx_allowed(req[0]))
        return OBDC_ERR_FORBIDDEN;
    snprintf(hex, sizeof hex, "%02X", pid);
    if (!pid_allowlisted(hex))
        return OBDC_ERR_FORBIDDEN;

    len = tp_transact(t, req, sizeof req, resp, sizeof resp);
    if (len < 0)
        return len;                       /* timeout: ECU stayed silent */
    /* Positive response: 42 <pid> <frame=00> <data...>. */
    if (len < 3 || resp[0] != 0x42 || resp[1] != pid || resp[2] != 0x00)
        return OBDC_ERR_PROTOCOL;
    return pid_decode(pid, resp + 3, (size_t)(len - 3), value_out);
}

/* ---- full scan ------------------------------------------------------ */

int obd_scan(can_transport_t *t, const uint8_t *pid_list, int n_pids,
             obd_scan_t *out)
{
    int rc, i;

    if (!t || !out || (n_pids > 0 && !pid_list) || n_pids > OBD_MAX_PIDS)
        return OBDC_ERR_PROTOCOL;
    memset(out, 0, sizeof *out);

    rc = obd_read_vin(t, out->vin);
    if (rc != OBDC_OK)
        return rc;                        /* no VIN, no scan */

    out->dtc_incomplete = false;
    rc = obd_read_dtcs(t, OBD_SVC_STORED_DTC, out->confirmed, OBD_MAX_DTCS);
    if (rc < 0) { out->dtc_incomplete = true; } else { out->n_confirmed = rc; }
    rc = obd_read_dtcs(t, OBD_SVC_PENDING_DTC, out->pending, OBD_MAX_DTCS);
    if (rc < 0) { out->dtc_incomplete = true; } else { out->n_pending = rc; }
    rc = obd_read_dtcs(t, OBD_SVC_PERMANENT_DTC, out->permanent, OBD_MAX_DTCS);
    if (rc < 0) { out->dtc_incomplete = true; } else { out->n_permanent = rc; }

    out->n_pids = 0;
    for (i = 0; i < n_pids; i++) {
        obd_pid_value_t *slot = &out->pids[out->n_pids];
        double v = 0.0;
        rc = obd_read_pid(t, pid_list[i], &v);
        if (rc == OBDC_ERR_FORBIDDEN)
            continue;                     /* not allowlisted: skip silently */
        slot->pid = pid_list[i];
        slot->present = (rc == OBDC_OK);
        slot->value = v;
        out->n_pids++;
    }
    return OBDC_OK;
}

/* ---- evidence JSON -------------------------------------------------- */

int obd_scan_to_json(const obd_scan_t *scan, char *buf, size_t cap)
{
    size_t used = 0;
    int i, w;

#define EMIT(...) do { \
        w = snprintf(buf + used, cap - used, __VA_ARGS__); \
        if (w < 0 || (size_t)w >= cap - used) return -1; \
        used += (size_t)w; \
    } while (0)

    if (!scan || !buf || cap == 0)
        return -1;
    EMIT("{\"vin\":\"%s\",\"dtcs_confirmed\":[", scan->vin);
    for (i = 0; i < scan->n_confirmed; i++)
        EMIT("%s\"%s\"", i ? "," : "", scan->confirmed[i]);
    EMIT("],\"dtcs_pending\":[");
    for (i = 0; i < scan->n_pending; i++)
        EMIT("%s\"%s\"", i ? "," : "", scan->pending[i]);
    EMIT("],\"dtcs_permanent\":[");
    for (i = 0; i < scan->n_permanent; i++)
        EMIT("%s\"%s\"", i ? "," : "", scan->permanent[i]);
    EMIT("],\"dtc_incomplete\":%s,\"pids\":{",
         scan->dtc_incomplete ? "true" : "false");
    {
        int first = 1;
        for (i = 0; i < scan->n_pids; i++) {
            if (!scan->pids[i].present)
                continue;
            EMIT("%s\"%02X\":%.4g", first ? "" : ",",
                 scan->pids[i].pid, scan->pids[i].value);
            first = 0;
        }
    }
    EMIT("}}");
    return (int)used;
#undef EMIT
}
