/* iso15765.c - ISO-TP (ISO 15765-2) single/First/Consecutive frame helpers.
 *
 * The dongle only *requests* read-only diagnostic services and only
 * *receives* multi-frame responses, but receiving a multi-frame response
 * requires the receiver to TRANSMIT: when the ECU sends a First Frame
 * (e.g. the 17-byte VIN reply to service 09/02), the receiver must answer
 * with a Flow Control frame or the ECU will never send the Consecutive
 * Frames. That is transport-layer plumbing, not a diagnostic write.
 *
 * Security definition: the device may transmit only allowlisted diagnostic
 * requests plus the transport-layer frames strictly necessary to receive
 * their responses (ISO-TP flow control). There is no raw-CAN-write path.
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
 * Returns payload length, or -1 on any protocol violation.
 *
 * NOTE: this is an offline decoder for already-captured frames. On the
 * live CAN bus, the receive path must transmit a Flow Control frame
 * (isotp_send_flow_control) immediately after each First Frame; see
 * isotp_on_first_frame(). */
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

/* Build an ISO-TP Flow Control frame into frame_out[8].
 *
 * Sent by the *receiver* after a First Frame, telling the sender it may
 * continue with Consecutive Frames. PCI byte 0x30 | flow_status:
 *   flow_status 0 = ContinueToSend, 1 = Wait, 2 = Overflow/abort.
 * block_size = max Consecutive Frames per block (0 = send all, no limit).
 * st_min = minimum separation time: 0x00-0x7F = 0-127 ms,
 *          0xF1-0xF9 = 100-900 us. Other values are reserved.
 * Returns 0 on success, -1 on bad arguments. */
int isotp_send_flow_control(uint8_t flow_status, uint8_t block_size,
                            uint8_t st_min, uint8_t frame_out[ISOTP_FRAME_LEN])
{
    size_t i;

    if (!frame_out || flow_status > 2)
        return -1;
    if (st_min > 0x7F && (st_min < 0xF1 || st_min > 0xF9))
        return -1; /* reserved STmin values */

    frame_out[0] = (uint8_t)(0x30 | flow_status);
    frame_out[1] = block_size;
    frame_out[2] = st_min;
    for (i = 3; i < ISOTP_FRAME_LEN; i++)
        frame_out[i] = 0x00;
    return 0;
}

/* Live-bus receive path: call this from the CAN ISR/task when a frame
 * arrives and the first PCI nibble is 0x1 (First Frame).
 *
 * Builds the Flow Control (ContinueToSend, block size 0, STmin 0) frame
 * into fc_out and returns the total payload length the sender announced.
 * The caller MUST transmit fc_out on the bus before expecting the
 * Consecutive Frames; without it the ECU stays silent and the transfer
 * stalls. Returns -1 if frame is not a valid First Frame. */
int isotp_on_first_frame(const uint8_t frame[ISOTP_FRAME_LEN],
                         uint8_t fc_out[ISOTP_FRAME_LEN])
{
    size_t total;

    if (!frame || ((frame[0] >> 4) != 0x1))
        return -1;
    total = ((size_t)(frame[0] & 0x0F) << 8) | frame[1];
    if (total <= 7 || total > 0xFFF)
        return -1;
    if (isotp_send_flow_control(0 /* ContinueToSend */, 0, 0, fc_out) != 0)
        return -1;
    return (int)total;
}
