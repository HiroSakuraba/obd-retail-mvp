#define _POSIX_C_SOURCE 200809L
/* test_obd_client.c - host validation of the ESP32 OBD client.
 *
 * Spawns simulator/can_ecu_sim.py, runs the real obd_client.c against it
 * over the TCP transport, and asserts:
 *   A. VIN reads correctly (multi-frame ISO-TP with flow control)
 *   B. confirmed/pending/permanent DTCs decode per SAE J2012
 *   C. live PID values match the canned vehicle exactly
 *   D. a non-allowlisted PID is refused BEFORE any frame is transmitted
 *   E. an allowlisted-but-unsupported PID times out cleanly (skip, no fail)
 *   F. full obd_scan degrades gracefully and renders evidence JSON
 *   G. wire audit: the client never transmitted anything but allowlisted
 *      services (01/03/07/09/0A) and ISO-TP flow control
 *
 * Build & run: make -C firmware/esp32/tests check
 */

#include <arpa/inet.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <time.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "can_transport.h"
#include "obd_client.h"

/* Provided by tcp_transport.c (host only). */
can_transport_t *tcp_transport_open(const char *host, int port);
can_transport_t *tcp_transport_open_variant(const char *host, int port,
                                            can_variant_t variant);
unsigned long tcp_transport_tx_count(const can_transport_t *t);

static int failures = 0;
static int checks = 0;

#define CHECK(name, cond) do { \
        checks++; \
        if (!(cond)) { failures++; printf("  FAIL: %s\n", name); } \
    } while (0)

#define CHECK_STR(name, got, want) \
    CHECK(name, strcmp((got), (want)) == 0)

#define CHECK_CLOSE(name, got, want) \
    CHECK(name, fabs((got) - (want)) < 1e-6)

/* ------------------------------------------------------------------ sim -- */

static pid_t sim_pid = 0;

static int port_free(int port)
{
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    int ok;
    if (s < 0)
        return 0;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = htons((uint16_t)port);
    ok = bind(s, (struct sockaddr *)&a, sizeof a) == 0;
    close(s);
    return ok;
}

static const char *repo_root(void)
{
    /* tests/ -> esp32/ -> firmware/ -> repo root */
    static char root[4096];
    ssize_t n = readlink("/proc/self/exe", root, sizeof root - 1);
    if (n < 0)
        return NULL;
    root[n] = '\0';
    /* exe is <root>/firmware/esp32/tests/test_obd_client */
    for (int i = 0; i < 4; i++) {
        char *slash = strrchr(root, '/');
        if (!slash)
            return NULL;
        *slash = '\0';
    }
    return root;
}

static int sim_spawn(int port, const char *frame_log, const char *vehicle,
                   int variant)
{
    char portstr[16], simpath[4096], vpath[4096];
    const char *root = repo_root();
    if (!root || strlen(root) + 64 >= sizeof simpath)
        return -1;
    if (!vehicle || strlen(vehicle) > 128)
        return -1;
    strcpy(simpath, root);
    strcat(simpath, "/simulator/can_ecu_sim.py");
    strcpy(vpath, root);
    strcat(vpath, "/simulator/canned-vehicles/");
    strcat(vpath, vehicle);
    snprintf(portstr, sizeof portstr, "%d", port);
    sim_pid = fork();
    if (sim_pid < 0)
        return -1;
    if (sim_pid == 0) {
        /* child: silence stdout, exec the simulator */
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            close(devnull);
        }
        {
            char varstr[8];
            snprintf(varstr, sizeof varstr, "%d", variant);
            execlp("python3", "python3", simpath,
                   "--port", portstr, "--frame-log", frame_log,
                   "--vehicle", vpath, "--variant", varstr, (char *)NULL);
        }
        _exit(127);
    }
    return 0;
}

static void sim_kill(void)
{
    if (sim_pid > 0) {
        kill(sim_pid, SIGTERM);
        waitpid(sim_pid, NULL, 0);
        sim_pid = 0;
    }
}

/* ----------------------------------------------------------------- tests -- */

static void test_vin(can_transport_t *t, const char *want)
{
    char vin[OBD_VIN_LEN + 1];
    int rc = obd_read_vin(t, vin);
    CHECK("vin: ok", rc == OBDC_OK);
    CHECK_STR("vin: value", vin, want);
}

static void test_dtcs(can_transport_t *t,
                      const char *want_conf[], int n_conf,
                      const char *want_pend[], int n_pend,
                      const char *want_perm[], int n_perm)
{
    char codes[OBD_MAX_DTCS][OBD_DTC_STRLEN];
    int n, i;

    n = obd_read_dtcs(t, OBD_SVC_STORED_DTC, codes, OBD_MAX_DTCS);
    CHECK("dtc confirmed: count", n == n_conf);
    for (i = 0; i < n && i < n_conf; i++)
        CHECK_STR("dtc confirmed: code", codes[i], want_conf[i]);

    n = obd_read_dtcs(t, OBD_SVC_PENDING_DTC, codes, OBD_MAX_DTCS);
    CHECK("dtc pending: count", n == n_pend);
    for (i = 0; i < n && i < n_pend; i++)
        CHECK_STR("dtc pending: code", codes[i], want_pend[i]);

    n = obd_read_dtcs(t, OBD_SVC_PERMANENT_DTC, codes, OBD_MAX_DTCS);
    CHECK("dtc permanent: count", n == n_perm);
    for (i = 0; i < n && i < n_perm; i++)
        CHECK_STR("dtc permanent: code", codes[i], want_perm[i]);

    /* bogus service is refused before transmit */
    n = obd_read_dtcs(t, 0x19, codes, OBD_MAX_DTCS);
    CHECK("dtc: service 0x19 forbidden", n == OBDC_ERR_FORBIDDEN);
}

static void test_pids(can_transport_t *t)
{
    double v;
    CHECK("pid 0C: ok", obd_read_pid(t, 0x0C, &v) == OBDC_OK);
    CHECK_CLOSE("pid 0C: 1726 rpm", v, 1726.0);
    CHECK("pid 05: ok", obd_read_pid(t, 0x05, &v) == OBDC_OK);
    CHECK_CLOSE("pid 05: 88 C", v, 88.0);
    CHECK("pid 06: ok", obd_read_pid(t, 0x06, &v) == OBDC_OK);
    CHECK_CLOSE("pid 06: STFT", v, (0x8E - 128) * 100.0 / 128.0);
    CHECK("pid 10: ok", obd_read_pid(t, 0x10, &v) == OBDC_OK);
    CHECK_CLOSE("pid 10: 3.1 g/s", v, 3.1);
    CHECK("pid 33: ok", obd_read_pid(t, 0x33, &v) == OBDC_OK);
    CHECK_CLOSE("pid 33: 101 kPa", v, 101.0);
}

static void test_freeze_frame(can_transport_t *t)
{
    double v;
    /* Mode 02 snapshot stored when the DTC set: the sim's frozen values
       equal its live ones (documented simulator limitation). */
    CHECK("freeze 0C: ok", obd_read_freeze_frame(t, 0x0C, &v) == OBDC_OK);
    CHECK_CLOSE("freeze 0C: 1726 rpm", v, 1726.0);
    CHECK("freeze 05: ok", obd_read_freeze_frame(t, 0x05, &v) == OBDC_OK);
    CHECK_CLOSE("freeze 05: 88 C", v, 88.0);
    /* 0xFF is not allowlisted: refused before any frame is sent. */
    CHECK("freeze FF: forbidden",
          obd_read_freeze_frame(t, 0xFF, &v) == OBDC_ERR_FORBIDDEN);
    /* 0x42 is allowlisted but the canned ECU does not answer it:
       real ECUs stay silent; the client must time out and move on. */
    v = -1.0;
    CHECK("freeze 0x42: timeout (skip)",
          obd_read_freeze_frame(t, 0x42, &v) == OBDC_ERR_TIMEOUT);
    CHECK("freeze 0x42: value untouched", v == -1.0);
}

static void test_forbidden_before_tx(can_transport_t *t)
{
    double v = -1.0;
    unsigned long before = tcp_transport_tx_count(t);
    int rc = obd_read_pid(t, 0xFF, &v);   /* not on the allowlist */
    CHECK("pid 0xFF: forbidden", rc == OBDC_ERR_FORBIDDEN);
    CHECK("pid 0xFF: no frame transmitted",
          tcp_transport_tx_count(t) == before);
    CHECK("pid 0xFF: value untouched", v == -1.0);
}

static void test_unsupported_skips(can_transport_t *t)
{
    double v = -1.0;
    /* 0x42 is allowlisted but the canned ECU does not answer it:
       real ECUs stay silent; the client must time out and move on. */
    int rc = obd_read_pid(t, 0x42, &v);
    CHECK("pid 0x42: timeout (skip)", rc == OBDC_ERR_TIMEOUT);
    CHECK("pid 0x42: value untouched", v == -1.0);
}

static void test_full_scan(can_transport_t *t)
{
    obd_scan_t scan;
    char js[2048];
    int rc, i, present = 0;

    rc = obd_scan(t, OBD_DEFAULT_PIDS, OBD_DEFAULT_PIDS_LEN, &scan);
    CHECK("scan: ok", rc == OBDC_OK);
    CHECK_STR("scan: vin", scan.vin, "1FMCU0GD0JUA12345");
    CHECK("scan: 1 confirmed", scan.n_confirmed == 1);
    CHECK("scan: pending empty", scan.n_pending == 0);
    CHECK("scan: permanent empty", scan.n_permanent == 0);
    CHECK("scan: dtc complete", scan.dtc_incomplete == false);
    CHECK("scan: 20 pids attempted", scan.n_pids == OBD_DEFAULT_PIDS_LEN);
    for (i = 0; i < scan.n_pids; i++)
        if (scan.pids[i].present)
            present++;
    /* canned vehicle answers 15 of the 20 (no 0F/14/15/2F/42) */
    CHECK("scan: 15 pids present", present == 15);

    rc = obd_scan_to_json(&scan, js, sizeof js);
    CHECK("scan json: rendered", rc > 0);
    CHECK("scan json: vin", strstr(js, "\"vin\":\"1FMCU0GD0JUA12345\"") != NULL);
    CHECK("scan json: dtc", strstr(js, "\"dtcs_confirmed\":[\"P0171\"]") != NULL);
    CHECK("scan json: rpm", strstr(js, "\"0C\":1726") != NULL);
    CHECK("scan json: absent pid omitted", strstr(js, "\"42\"") == NULL);
}

/* Normalize a hex id: strip leading zeros ("000007DF" -> "7DF"). */
static void norm_id(const char *hex, char *out, size_t cap)
{
    while (*hex == '0')
        hex++;
    if (!*hex)
        hex = "0";
    snprintf(out, cap, "%s", hex);
}

/* Wire audit: parse the simulator's frame log; every client frame must be
   either a single-frame request to the functional id with an allowlisted
   service, or a flow-control frame to the physical id. func_id/fc_id are
   the expected ids without leading zeros ("7DF"/"7E0", "18DB33F1"/...). */
static void test_wire_audit(const char *frame_log, int min_frames,
                            const char *func_id, const char *fc_id)
{
    FILE *f = fopen(frame_log, "r");
    char line[256];
    int nframes = 0, ok = 1;

    CHECK("wire audit: log exists", f != NULL);
    if (!f)
        return;
    while (fgets(line, sizeof line, f)) {
        const char *id, *pci, *svc;
        char nid[16];
        const char *hex;
        size_t i;
        nframes++;
        id = strstr(line, "\"id\": \"0x");
        pci = strstr(line, "\"pci\": \"");
        svc = strstr(line, "\"service\": \"0x");
        if (!id || !pci) { ok = 0; break; }
        hex = id + 9;
        for (i = 0; i < 8 && isxdigit((unsigned char)hex[i]); i++)
            ;
        if (i == 0 || i > 8) { ok = 0; break; }
        {
            char raw[16];
            memcpy(raw, hex, i);
            raw[i] = '\0';
            norm_id(raw, nid, sizeof nid);
        }
        if (strcmp(nid, func_id) == 0) {
            if (strncmp(pci + 8, "SF", 2) != 0) { ok = 0; break; }
            if (!svc) { ok = 0; break; }
            if (!(strncmp(svc + 14, "01", 2) == 0 ||
                  strncmp(svc + 14, "02", 2) == 0 ||
                  strncmp(svc + 14, "03", 2) == 0 ||
                  strncmp(svc + 14, "07", 2) == 0 ||
                  strncmp(svc + 14, "09", 2) == 0 ||
                  strncmp(svc + 14, "0A", 2) == 0)) { ok = 0; break; }
        } else if (strcmp(nid, fc_id) == 0) {
            if (strncmp(pci + 8, "FC", 2) != 0) { ok = 0; break; }
        } else {
            ok = 0; break;
        }
    }
    fclose(f);
    CHECK("wire audit: frames seen", nframes >= min_frames);
    CHECK("wire audit: only allowlisted services + flow control", ok);
}

/* ------------------------------------------------------------------ main -- */

/* Spawn the simulator on a free port and connect. Returns the transport
   (caller closes) or NULL. */
static can_transport_t *sim_up(const char *vehicle, const char *frame_log,
                               int *port_out, can_variant_t variant)
{
    can_transport_t *t = NULL;
    int port, tries = 0;

    for (port = 35001; port < 35060; port++)
        if (port_free(port))
            break;
    if (port >= 35060)
        return NULL;
    if (sim_spawn(port, frame_log, vehicle,
                  variant == CAN_VAR_29B_500K ? 29 : 11) != 0)
        return NULL;
    while (tries++ < 50) {
        t = tcp_transport_open_variant("127.0.0.1", port, variant);
        if (t)
            break;
        {
            struct timespec ts = { 0, 100000000L };
            nanosleep(&ts, NULL);
        }
    }
    if (!t)
        sim_kill();
    else if (port_out)
        *port_out = port;
    return t;
}

static char *new_frame_log(void)
{
    static char frame_log[] = "/tmp/obd_wire_audit_XXXXXX";
    int logfd = mkstemp(frame_log);
    if (logfd >= 0)
        close(logfd);
    return frame_log;
}

int main(void)
{
    can_transport_t *t;
    char *frame_log;
    const char *esc_conf[] = { "P0171" };
    const char *cam_conf[] = { "P0420" };
    const char *cam_pend[] = { "P0133" };
    const char *cam_perm[] = { "P0420" };

    /* Phase 1: Escape P0171 -- full suite. */
    frame_log = new_frame_log();
    t = sim_up("escape-2018-p0171.json", frame_log, NULL, CAN_VAR_11B_500K);
    if (!t) {
        printf("sim did not come up\n");
        return 1;
    }
    test_vin(t, "1FMCU0GD0JUA12345");
    test_dtcs(t, esc_conf, 1, NULL, 0, NULL, 0);
    test_pids(t);
    test_freeze_frame(t);
    test_forbidden_before_tx(t);
    test_unsupported_skips(t);
    test_full_scan(t);
    t->close(t);
    sim_kill();
    test_wire_audit(frame_log, 20, "7DF", "7E0");
    unlink(frame_log);

    /* Phase 2: Camry P0420 -- non-empty pending + permanent DTC paths,
       multi-frame VIN again, and one live PID for good measure. */
    frame_log = new_frame_log();
    t = sim_up("camry-2020-p0420.json", frame_log, NULL, CAN_VAR_11B_500K);
    if (!t) {
        printf("sim 2 did not come up\n");
        return 1;
    }
    {
        double v;
        test_vin(t, "4T1G11AKXLU123456");
        test_dtcs(t, cam_conf, 1, cam_pend, 1, cam_perm, 1);
        CHECK("camry pid 0C: ok", obd_read_pid(t, 0x0C, &v) == OBDC_OK);
        CHECK_CLOSE("camry pid 0C: 1480 rpm", v, 1480.0);
        test_forbidden_before_tx(t);
    }
    t->close(t);
    sim_kill();
    test_wire_audit(frame_log, 5, "7DF", "7E0");
    unlink(frame_log);

    /* Phase 3: 29-bit variant -- same client code, extended identifiers.
       Exercises the variant-aware request/response id mapping end to end. */
    frame_log = new_frame_log();
    t = sim_up("escape-2018-p0171.json", frame_log, NULL, CAN_VAR_29B_500K);
    if (!t) {
        printf("sim 3 (29-bit) did not come up\n");
        return 1;
    }
    {
        double v;
        test_vin(t, "1FMCU0GD0JUA12345");
        test_dtcs(t, esc_conf, 1, NULL, 0, NULL, 0);
        CHECK("29-bit pid 0C: ok", obd_read_pid(t, 0x0C, &v) == OBDC_OK);
        CHECK_CLOSE("29-bit pid 0C: 1726 rpm", v, 1726.0);
        test_freeze_frame(t);
    }
    t->close(t);
    sim_kill();
    test_wire_audit(frame_log, 8, "18DB33F1", "18DA10F1");
    unlink(frame_log);

    printf("obd_client host tests: %d/%d passed\n",
           checks - failures, checks);
    return failures ? 1 : 0;
}
