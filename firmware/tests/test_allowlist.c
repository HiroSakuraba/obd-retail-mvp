/* test_allowlist.c - host-runnable tests for the read-only allowlist.
 *
 * Verifies the core safety property: anything not on the allowlist is
 * refused before it can reach the CAN bus, and version mismatches never
 * get dispatched.
 */

#include <stdio.h>
#include <string.h>

#include "ble_protocol.h"
#include "obd_allowlist.h"

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond, name) do { \
    g_checks++; \
    if (cond) { printf("PASS %s\n", name); } \
    else { printf("FAIL %s\n", name); g_failures++; } \
} while (0)

/* Build a request struct the way proto_parse_request would. */
static void make_req(ble_request_t *r, unsigned v, const char *id,
                     const char *op, const char *pid)
{
    memset(r, 0, sizeof *r);
    r->version = v;
    snprintf(r->id, sizeof r->id, "%s", id);
    snprintf(r->op, sizeof r->op, "%s", op);
    if (pid)
        snprintf(r->pid, sizeof r->pid, "%s", pid);
}

static void test_allowlisted_pids_pass(void)
{
    ble_request_t r;
    obd_op_t op;

    make_req(&r, 1, "t1", "read_pid", "0C");
    CHECK(obd_dispatch(&r, &op) == OBD_OK && op == OP_READ_PID,
          "read_pid 0C (RPM) allowed");
    CHECK(obd_service_for(op) == 0x01, "read_pid maps to service 01");

    make_req(&r, 1, "t2", "read_pid", "05");
    CHECK(obd_dispatch(&r, &op) == OBD_OK, "read_pid 05 (coolant) allowed");

    make_req(&r, 1, "t3", "read_pid", "06");
    CHECK(obd_dispatch(&r, &op) == OBD_OK, "read_pid 06 (STFT B1) allowed");

    make_req(&r, 1, "t4", "read_pid", "0c");
    CHECK(obd_dispatch(&r, &op) == OBD_OK, "lowercase pid normalized");
}

static void test_non_allowlisted_pid_rejected(void)
{
    ble_request_t r;
    obd_op_t op = OP_READ_PID;

    make_req(&r, 1, "t5", "read_pid", "FF");
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_PID_NOT_ALLOWED,
          "read_pid FF rejected with ERROR_PID_NOT_ALLOWED");
    CHECK(strcmp(obd_error_code(OBD_ERR_PID_NOT_ALLOWED),
                 "ERROR_PID_NOT_ALLOWED") == 0, "error code string matches");

    make_req(&r, 1, "t6", "read_pid", "");
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_PID_NOT_ALLOWED,
          "read_pid with empty pid rejected");
}

static void test_forbidden_ops_rejected(void)
{
    ble_request_t r;
    obd_op_t op;

    make_req(&r, 1, "t7", "raw_can_write", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_FORBIDDEN_COMMAND,
          "raw_can_write rejected with ERROR_FORBIDDEN_COMMAND");

    make_req(&r, 1, "t8", "clear_dtcs", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_FORBIDDEN_COMMAND,
          "clear_dtcs rejected (no write path)");

    make_req(&r, 1, "t9", "ecu_reprogram", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_FORBIDDEN_COMMAND,
          "ecu_reprogram rejected");

    make_req(&r, 1, "t10", "", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_FORBIDDEN_COMMAND,
          "empty op rejected");
}

static void test_dtc_and_vin_ops(void)
{
    ble_request_t r;
    obd_op_t op;

    make_req(&r, 1, "t11", "read_vin", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_OK && obd_service_for(op) == 0x09,
          "read_vin -> service 09");

    make_req(&r, 1, "t12", "read_dtcs_confirmed", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_OK && obd_service_for(op) == 0x03,
          "read_dtcs_confirmed -> service 03");

    make_req(&r, 1, "t13", "read_dtcs_pending", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_OK && obd_service_for(op) == 0x07,
          "read_dtcs_pending -> service 07");

    make_req(&r, 1, "t14", "read_dtcs_permanent", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_OK && obd_service_for(op) == 0x0A,
          "read_dtcs_permanent -> service 0A");

    make_req(&r, 1, "t14b", "read_freeze_frame", "05");
    CHECK(obd_dispatch(&r, &op) == OBD_OK && op == OP_READ_FREEZE_FRAME &&
          obd_service_for(op) == 0x02,
          "read_freeze_frame 05 -> service 02");

    make_req(&r, 1, "t14c", "read_freeze_frame", "FF");
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_PID_NOT_ALLOWED,
          "read_freeze_frame with non-allowlisted pid rejected");

    make_req(&r, 1, "t14d", "read_pid", "14");
    CHECK(obd_dispatch(&r, &op) == OBD_OK,
          "read_pid 14 (upstream O2 voltage) allowed");
    make_req(&r, 1, "t14e", "read_pid", "15");
    CHECK(obd_dispatch(&r, &op) == OBD_OK,
          "read_pid 15 (downstream O2 voltage) allowed");
}

static void test_version_mismatch(void)
{
    ble_request_t r;
    obd_op_t op;

    make_req(&r, 2, "t15", "read_vin", NULL);
    CHECK(obd_dispatch(&r, &op) == OBD_ERR_VERSION,
          "protocol v2 request rejected with ERROR_VERSION");

    /* Parser-level check too. */
    {
        const char *json = "{\"v\":2,\"id\":\"x\",\"op\":\"read_vin\"}";
        ble_request_t pr;
        CHECK(proto_parse_request(json, strlen(json), &pr) == -2,
              "parser reports version mismatch");
    }
    {
        const char *json =
            "{\"v\":1,\"id\":\"req-1042\",\"op\":\"read_pid\",\"pid\":\"0C\"}";
        ble_request_t pr;
        CHECK(proto_parse_request(json, strlen(json), &pr) == 0 &&
              strcmp(pr.id, "req-1042") == 0 &&
              strcmp(pr.op, "read_pid") == 0 &&
              strcmp(pr.pid, "0C") == 0,
              "parser extracts id/op/pid");
    }
}

static void test_response_framing(void)
{
    ble_request_t r;
    char buf[BLE_RESP_MAX];

    make_req(&r, 1, "req-7", "read_pid", "0C");
    CHECK(proto_build_response(&r, "{\"pid\":\"0C\",\"value\":1726}",
                               buf, sizeof buf) == 0 &&
          strstr(buf, "\"ok\":true") != NULL &&
          strstr(buf, "\"id\":\"req-7\"") != NULL,
          "ok response framed with echoed id");
    CHECK(proto_build_error(&r, "ERROR_FORBIDDEN_COMMAND",
                            buf, sizeof buf) == 0 &&
          strstr(buf, "\"ok\":false") != NULL &&
          strstr(buf, "ERROR_FORBIDDEN_COMMAND") != NULL,
          "error response framed");
}

int main(void)
{
    test_allowlisted_pids_pass();
    test_non_allowlisted_pid_rejected();
    test_forbidden_ops_rejected();
    test_dtc_and_vin_ops();
    test_version_mismatch();
    test_response_framing();
    printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
