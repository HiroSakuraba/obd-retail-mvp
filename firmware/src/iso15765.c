/* iso15765.c - ISO-TP (ISO 15765-2) single/First/Consecutive frame helpers.
 *
 * The dongle only *receives* multi-frame responses and *sends* short
 * single-frame requests, but both directions are implemented so the bench
 * harness can test against a simulator. Flow-control frames are parsed on
 * receive; the dongle never needs to send them for read-only services.
 */

#include <stdint.h>
#include <stddef.h>

#define ISOTP_FRAME_LEN 8
#define ISOTP_MAX_FRAMES 64

/* Encode payload into 8-byte CAN frames.
 * Returns the number of frames written, or -1 if max_frames is too small. */
int isotp_encode(const uint8_t *payload, size_t len,
                 uint8_t frames[][ISOTP_FRAME_LEN], size_t max_frames)
{
    size_t nframes = 0, off = 0, chunk;
    uint8_t seq = 1;

    if (!payload || !frames || max_frames == 0)
        return -1;

    if (len <= 7) {
        /* Single frame: PCI byte 0x0N, N = length. */
        if (max_frames < 1)
            return -1;
        frames[0][0] = (uint8_t)(len & 0x0F);
        for (off = 0; off < len; off++)
            frames[0][1 + off] = payload[off];
        for (; off < 7; off++)
            frames[0][1 + off] = 0x00;
        return 1;
    }

    /* First frame: 0x10 | (len >> 8), len & 0xFF, then 6 payload bytes. */
    if (max_frames < 1 || len > 0xFFF)
        return -1;
    frames[0][0] = (uint8_t)(0x10 | ((len >> 8) & 0x0F));
    frames[0][1] = (uint8_t)(len & 0xFF);
    for (off = 0; off < 6 && off < len; off++)
        frames[0][2 + off] = payload[off];
    nframes = 1;

    /* Consecutive frames: 0x20 | seq, then 7 payload bytes each. */
    while (off < len) {
        size_t i;
        if (nframes >= max_frames || nframes >= ISOTP_MAX_FRAMES)
            return -1;
        chunk = len - off;
        if (chunk > 7)
            chunk = 7;
        frames[nframes][0] = (uint8_t)(0x20 | (seq & 0x0F));
        for (i = 0; i < chunk; i++)
            frames[nframes][1 + i] = payload[off + i];
        for (; i < 7; i++)
            frames[nframes][1 + i] = 0x00;
        off += chunk;
        nframes++;
        seq++;
        if (seq > 0x0F)
            seq = 0;
    }
    return (int)nframes;
}

/* Decode a received frame sequence back into a payload.
 * Returns payload length, or -1 on any protocol violation. */
int isotp_decode(const uint8_t frames[][ISOTP_FRAME_LEN], size_t nframes,
                 uint8_t *out, size_t out_cap)
{
    size_t total, off = 0, i, chunk;
    uint8_t expect_seq = 1;
    uint8_t ptype;

    if (!frames || !out || nframes == 0 || nframes > ISOTP_MAX_FRAMES)
        return -1;

    ptype = (uint8_t)(frames[0][0] >> 4);
    if (ptype == 0x0) {
        /* Single frame. */
        total = frames[0][0] & 0x0F;
        if (total > 7 || total > out_cap)
            return -1;
        for (i = 0; i < total; i++)
            out[i] = frames[0][1 + i];
        return (int)total;
    }
    if (ptype != 0x1)
        return -1;

    /* First frame: total length in 12 bits. */
    total = ((size_t)(frames[0][0] & 0x0F) << 8) | frames[0][1];
    if (total <= 7 || total > out_cap)
        return -1;
    chunk = total < 6 ? total : 6;
    for (i = 0; i < chunk; i++)
        out[off++] = frames[0][2 + i];

    for (i = 1; i < nframes && off < total; i++) {
        size_t j, take;
        if ((frames[i][0] >> 4) != 0x2)
            return -1; /* expected consecutive frame */
        if ((frames[i][0] & 0x0F) != (expect_seq & 0x0F))
            return -1; /* sequence break */
        take = total - off;
        if (take > 7)
            take = 7;
        for (j = 0; j < take; j++)
            out[off++] = frames[i][1 + j];
        expect_seq++;
        if (expect_seq > 0x0F)
            expect_seq = 0;
    }
    if (off != total)
        return -1; /* truncated */
    return (int)total;
}
