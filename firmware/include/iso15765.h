/* iso15765.h - ISO-TP (ISO 15765-2) helpers for the read-only dongle.
 *
 * The dongle transmits exactly two kinds of CAN frames:
 *   1. allowlisted diagnostic requests (single-frame, via isotp_encode), and
 *   2. ISO-TP Flow Control frames strictly necessary to RECEIVE multi-frame
 *      responses (via isotp_send_flow_control / isotp_on_first_frame).
 * There is no raw-CAN-write API.
 */
#ifndef ISO15765_H
#define ISO15765_H

#include <stdint.h>
#include <stddef.h>

#define ISOTP_FRAME_LEN 8
#define ISOTP_MAX_FRAMES 64

int isotp_encode(const uint8_t *payload, size_t len,
                 uint8_t frames[][ISOTP_FRAME_LEN], size_t max_frames);

int isotp_decode(const uint8_t frames[][ISOTP_FRAME_LEN], size_t nframes,
                 uint8_t *out, size_t out_cap);

/* Build a Flow Control frame (PCI 0x30). flow_status: 0=ContinueToSend,
 * 1=Wait, 2=Overflow/abort. Returns 0 on success, -1 on bad arguments. */
int isotp_send_flow_control(uint8_t flow_status, uint8_t block_size,
                            uint8_t st_min, uint8_t frame_out[ISOTP_FRAME_LEN]);

/* Live-bus receive path for an incoming First Frame: builds the
 * ContinueToSend flow-control frame into fc_out and returns the announced
 * payload length. The caller must transmit fc_out before Consecutive
 * Frames will arrive. Returns -1 if not a valid First Frame. */
int isotp_on_first_frame(const uint8_t frame[ISOTP_FRAME_LEN],
                         uint8_t fc_out[ISOTP_FRAME_LEN]);

#endif /* ISO15765_H */
