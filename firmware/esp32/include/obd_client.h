/* obd_client.h - read-only OBD-II scan client for the retail dongle.
 *
 * This is the firmware that turns the bench build (ESP32 + CAN
 * transceiver on the OBD-II port) into the product behavior: given a CAN
 * transport, it performs the allowlisted read-only scan --
 *   VIN (09 02) -> confirmed/pending/permanent DTCs (03/07/0A) ->
 *   live PIDs (01 xx)
 * -- and renders the result as the evidence JSON the backend session API
 * expects.
 *
 * Read-only enforcement lives here, on the transmit path, in addition to
 * the BLE-request allowlist (obd_allowlist.h): a service byte that is not
 * in the allowlist is never encoded onto the bus, and a PID that is not
 * on the PID allowlist is refused before any frame is transmitted.
 *
 * The code is portable C99: the same translation unit compiles for the
 * ESP32 (with twai_transport.c) and for the host test harness (with
 * tcp_transport.c talking to simulator/can_ecu_sim.py).
 */
#ifndef OBD_CLIENT_H
#define OBD_CLIENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "can_transport.h"

#ifdef __cplusplus
extern "C" {
#endif

#define OBD_VIN_LEN      17
#define OBD_MAX_DTCS     32
#define OBD_DTC_STRLEN   6   /* "P0171" + NUL */
#define OBD_MAX_PIDS     48

/* Result codes. Negative values never leave a partial side effect on the
   bus: OBD_FORBIDDEN is returned before any frame is transmitted. */
#define OBDC_OK            0
#define OBDC_ERR_TIMEOUT  -1  /* no (usable) response within the deadline */
#define OBDC_ERR_FORBIDDEN -2 /* service/PID not on the read-only allowlist */
#define OBDC_ERR_PROTOCOL -3  /* response failed validation */

/* Services the client may transmit (subset of the firmware allowlist that
   the scan uses). */
#define OBD_SVC_CURRENT_DATA   0x01u
#define OBD_SVC_FREEZE_FRAME_DATA 0x02u
#define OBD_SVC_STORED_DTC     0x03u
#define OBD_SVC_PENDING_DTC    0x07u
#define OBD_SVC_VEHICLE_INFO   0x09u
#define OBD_SVC_PERMANENT_DTC  0x0Au

/* Default live-PID scan list: every PID the six diagnostic graphs need
   (fuel trims, RPM, coolant, O2) plus the core drivability set. All are
   on the firmware PID allowlist (obd_allowlist.h). */
extern const uint8_t OBD_DEFAULT_PIDS[];
extern const int OBD_DEFAULT_PIDS_LEN;

/* One decoded live PID. present=false means the ECU stayed silent
   (unsupported) and the scan moved on -- never a failure. */
typedef struct {
    uint8_t pid;
    double  value;
    bool    present;
} obd_pid_value_t;

typedef struct {
    char vin[OBD_VIN_LEN + 1];  /* "" if unread */
    char confirmed[OBD_MAX_DTCS][OBD_DTC_STRLEN];
    char pending[OBD_MAX_DTCS][OBD_DTC_STRLEN];
    char permanent[OBD_MAX_DTCS][OBD_DTC_STRLEN];
    int  n_confirmed, n_pending, n_permanent;
    bool dtc_incomplete;  /* a DTC service timed out; lists may be partial */
    obd_pid_value_t pids[OBD_MAX_PIDS];
    int  n_pids;
} obd_scan_t;

/* Read the 17-character VIN (service 09 PID 02, multi-frame ISO-TP).
   vin_out must hold OBD_VIN_LEN+1 bytes. */
int obd_read_vin(can_transport_t *t, char vin_out[OBD_VIN_LEN + 1]);

/* Read a DTC list. service is OBD_SVC_STORED_DTC / OBD_SVC_PENDING_DTC /
   OBD_SVC_PERMANENT_DTC. codes_out holds up to cap strings of
   OBD_DTC_STRLEN. Returns the number of codes, or a negative OBD_ERR_*. */
int obd_read_dtcs(can_transport_t *t, uint8_t service,
                  char codes_out[][OBD_DTC_STRLEN], int cap);

/* Read one live PID (service 01). Returns OBDC_OK with *value_out set,
   OBDC_ERR_TIMEOUT if the ECU stayed silent (unsupported PID -- skip it),
   OBDC_ERR_FORBIDDEN if the PID is not allowlisted (no frame is sent),
   OBDC_ERR_PROTOCOL if the response fails validation. */
int obd_read_pid(can_transport_t *t, uint8_t pid, double *value_out);

/* Read one frozen PID (service 02, frame 0): the snapshot stored when the
   DTC was set. Same contract as obd_read_pid (OBDC_ERR_TIMEOUT if the ECU
   stayed silent, OBDC_ERR_FORBIDDEN before any frame is transmitted). */
int obd_read_freeze_frame(can_transport_t *t, uint8_t pid, double *value_out);

/* Full scan: VIN, then the three DTC lists, then each PID in pid_list.
   VIN failure aborts the scan (OBD_ERR_*); everything else degrades
   gracefully (dtc_incomplete / present=false). */
int obd_scan(can_transport_t *t, const uint8_t *pid_list, int n_pids,
             obd_scan_t *out);

/* Render a scan as the evidence JSON the backend session API consumes:
   {"vin":"...","dtcs_confirmed":[...],"dtcs_pending":[...],
    "dtcs_permanent":[...],"dtc_incomplete":false,
    "pids":{"0C":1726.0,...}}.
   Returns the bytes written (excluding NUL) or -1 if buf is too small. */
int obd_scan_to_json(const obd_scan_t *scan, char *buf, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* OBD_CLIENT_H */
