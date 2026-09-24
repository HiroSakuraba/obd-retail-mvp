#define _POSIX_C_SOURCE 200809L
/* tcp_transport.c - host test transport for the OBD client.
 *
 * Forwards CAN frames over TCP to simulator/can_ecu_sim.py using its
 * 11-byte framing: struct ">H B 8s" = CAN id, DLC, data padded to 8.
 * Host-only (POSIX sockets); never compiled into the ESP32 firmware.
 */

#include "can_transport.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    int sock;
    unsigned long tx_frames;   /* frames transmitted (wire-audit tests) */
} tcp_ctx_t;

static uint32_t tcp_millis(can_transport_t *t)
{
    struct timespec ts;
    (void)t;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static int tcp_send(can_transport_t *t, const can_frame_t *f)
{
    tcp_ctx_t *c = (tcp_ctx_t *)t->ctx;
    uint8_t buf[11];
    size_t sent = 0;

    if (!c || !f || f->dlc > 8)
        return -1;
    buf[0] = (uint8_t)((f->id >> 8) & 0xFF);
    buf[1] = (uint8_t)(f->id & 0xFF);
    buf[2] = f->dlc;
    memcpy(buf + 3, f->data, 8);
    while (sent < sizeof buf) {
        ssize_t n = send(c->sock, buf + sent, sizeof buf - sent, 0);
        if (n <= 0)
            return -1;
        sent += (size_t)n;
    }
    c->tx_frames++;
    return 0;
}

static int tcp_recv(can_transport_t *t, can_frame_t *f, uint32_t timeout_ms)
{
    tcp_ctx_t *c = (tcp_ctx_t *)t->ctx;
    uint8_t buf[11];
    size_t got = 0;
    fd_set rfds;
    struct timeval tv;

    if (!c || !f)
        return -1;
    FD_ZERO(&rfds);
    FD_SET(c->sock, &rfds);
    tv.tv_sec = (long)(timeout_ms / 1000);
    tv.tv_usec = (long)((timeout_ms % 1000) * 1000);
    if (select(c->sock + 1, &rfds, NULL, NULL, &tv) <= 0)
        return 1;                       /* timeout (or select error: same) */
    while (got < sizeof buf) {
        ssize_t n = recv(c->sock, buf + got, sizeof buf - got, 0);
        if (n <= 0)
            return -1;
        got += (size_t)n;
    }
    f->id = (uint32_t)((buf[0] << 8) | buf[1]);
    f->dlc = buf[2] > 8 ? 8 : buf[2];
    memcpy(f->data, buf + 3, 8);
    return 0;
}

static void tcp_close(can_transport_t *t)
{
    tcp_ctx_t *c;
    if (!t)
        return;
    c = (tcp_ctx_t *)t->ctx;
    if (c) {
        close(c->sock);
        free(c);
    }
    free(t);
}

/* Open a transport to a running can_ecu_sim.py. Returns NULL on failure. */
can_transport_t *tcp_transport_open(const char *host, int port)
{
    tcp_ctx_t *c;
    can_transport_t *t;
    struct sockaddr_in addr;
    int sock = socket(AF_INET, SOCK_STREAM, 0);

    if (sock < 0)
        return NULL;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host ? host : "127.0.0.1", &addr.sin_addr) != 1) {
        close(sock);
        return NULL;
    }
    if (connect(sock, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(sock);
        return NULL;
    }
    c = (tcp_ctx_t *)calloc(1, sizeof *c);
    t = (can_transport_t *)calloc(1, sizeof *t);
    if (!c || !t) {
        free(c);
        free(t);
        close(sock);
        return NULL;
    }
    c->sock = sock;
    t->ctx = c;
    t->send = tcp_send;
    t->recv = tcp_recv;
    t->millis = tcp_millis;
    t->close = tcp_close;
    return t;
}

/* Frames transmitted so far (for read-only wire-audit assertions). */
unsigned long tcp_transport_tx_count(const can_transport_t *t)
{
    const tcp_ctx_t *c = t ? (const tcp_ctx_t *)t->ctx : NULL;
    return c ? c->tx_frames : 0;
}
