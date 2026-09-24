"""Algorithm verification for android/.../elm/Elm327Protocol.kt.

kotlinc is not available on this machine, so each parser below mirrors the
Kotlin implementation line-for-line in Python and is verified against fixed
test vectors. If the Kotlin and Python ever disagree, the vectors in this
file are the source of truth — update the Kotlin to match.
"""

import re

import pytest


# ------------------------------------------------- mirror of Elm327Protocol --

def sanitize(raw):
    out = []
    for line in raw.splitlines():
        l = line.strip().rstrip(">").strip()
        if not l:
            continue
        up = l.upper()
        if up in ("NO DATA", "?", "OK") or up.startswith("SEARCHING") \
                or up.startswith("ELM327"):
            continue
        l = re.sub(r"^\d+\s*:", "", l).strip()
        toks = [t for t in re.split(r"\s+", l) if t]
        if len(toks) >= 2 and re.fullmatch(r"[0-9A-Fa-f]{3}", toks[0]) \
                and re.fullmatch(r"[12][0-9A-Fa-f]", toks[1]):
            toks = toks[2:]
        toks = [t for t in toks if re.fullmatch(r"[0-9A-Fa-f]{1,2}", t)]
        if toks:
            out.append(" ".join(toks).upper())
    return out


def parse_vin(lines):
    byts = []
    for t in (" ".join(lines)).split():
        try:
            byts.append(int(t, 16))
        except ValueError:
            raise ValueError("non-hex VIN token %r" % t)
    try:
        i = byts.index(0x49)
    except ValueError:
        i = -1
    if not (i >= 0 and i + 2 < len(byts) and byts[i + 1] == 0x02):
        raise ValueError("no '49 02' header in VIN response")
    vin_b = byts[i + 3:i + 20]
    if len(vin_b) != 17:
        raise ValueError("VIN payload is %d bytes, expected 17" % len(vin_b))
    vin = "".join(chr(b) for b in vin_b)
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise ValueError("VIN %r fails ISO 3779 format check" % vin)
    return vin


def decode_dtc(b1, b2):
    sys = "PCBU"[b1 >> 6]
    return "%s%X%X%X%X" % (sys, (b1 >> 4) & 3, b1 & 15, b2 >> 4, b2 & 15)


def parse_dtcs(response, response_sid=0x43):
    toks = " ".join(sanitize(response)).split()
    byts = [int(t, 16) for t in toks]
    if response_sid not in byts:
        raise ValueError("no '%02X' service header in DTC response" % response_sid)
    i = byts.index(response_sid)
    out, j = [], i + 1
    while j + 1 < len(byts):
        b1, b2 = byts[j], byts[j + 1]
        if b1 == 0 and b2 == 0:
            break
        out.append(decode_dtc(b1, b2))
        j += 2
    return out


def decode_pid(pid, data_hex):
    toks = [t for t in re.split(r"\s+", data_hex.strip()) if t]
    b = []
    for t in toks:
        try:
            b.append(int(t, 16))
        except ValueError:
            raise ValueError("non-hex PID payload token %r" % t)

    def need(n):
        if len(b) < n:
            raise ValueError("PID 0x%02X needs %d bytes, got %d"
                             % (pid, n, len(b)))

    if pid == 0x04:
        need(1); return b[0] * 100.0 / 255
    if pid == 0x05:
        need(1); return float(b[0] - 40)
    if pid in (0x06, 0x07, 0x08, 0x09):
        need(1); return (b[0] - 128) * 100.0 / 128
    if pid == 0x0B:
        need(1); return float(b[0])
    if pid == 0x0C:
        need(2); return (b[0] * 256 + b[1]) / 4.0
    if pid == 0x0D:
        need(1); return float(b[0])
    if pid == 0x0E:
        need(1); return b[0] / 2.0 - 64
    if pid == 0x10:
        need(2); return (b[0] * 256 + b[1]) / 100.0
    if pid == 0x11:
        need(1); return b[0] * 100.0 / 255
    if pid == 0x1F:
        need(2); return float(b[0] * 256 + b[1])
    if pid == 0x21:
        need(2); return float(b[0] * 256 + b[1])
    if pid == 0x33:
        need(1); return float(b[0])
    raise ValueError("unsupported PID 0x%02X" % pid)


# ------------------------------------------------------------------ vectors --

VIN = "1FMCU0GD0JUA12345"
# VIN bytes: 31 46 4D 43 55 30 47 44 30 4A 55 41 31 32 33 34 35

def test_vin_numbered_lines():
    lines = sanitize("014\r\n0: 49 02 01 31 46 4D 43\r\n"
                     "1: 55 30 47 44 30 4A 55\r\n2: 41 31 32 33 34 35\r\n>")
    assert parse_vin(lines) == VIN


def test_vin_pci_frames():
    lines = sanitize("7E8 10 14 49 02 01 31 46 4D\r\n"
                     "7E8 21 43 55 30 47 44 30 4A\r\n"
                     "7E8 22 55 41 31 32 33 34 35\r\n>")
    assert parse_vin(lines) == VIN


def test_vin_plain_concatenated():
    lines = sanitize("49 02 01 31 46 4D 43 55 30 47 44 30 4A 55 41 31 32 33 34 35\r\n>")
    assert parse_vin(lines) == VIN


def test_vin_garbage_raises():
    with pytest.raises(ValueError):
        parse_vin(sanitize("NO DATA\r\n>"))
    with pytest.raises(ValueError):
        parse_vin(sanitize("43 01 71\r\n>"))  # wrong service


def test_sanitize_drops_echo_and_prompts():
    assert sanitize("0902\r\n43 01 71\r\n>") == ["43 01 71"]
    assert sanitize("SEARCHING...\r\n41 0C 1A F8\r\n>") == ["41 0C 1A F8"]
    assert sanitize("NO DATA\r\n>") == []
    assert sanitize("?\r\n>") == []


# DTC vectors derived from the SAE J2012 bit layout:
#   byte1 bits 7-6: 00=P 01=C 10=B 11=U; bits 5-4: second digit 0-3.
#   P0171 = 01 71 | P0133 = 01 33 | U0100 = C1 00 | C1234 = 52 34 | B2AAA = AA AA
def test_dtc_two_codes():
    assert parse_dtcs("43 01 33 01 71\r\n>") == ["P0133", "P0171"]


def test_dtc_none():
    assert parse_dtcs("43 00\r\n>") == []


def test_dtc_system_letters():
    assert parse_dtcs("43 C1 00\r\n>") == ["U0100"]
    assert parse_dtcs("43 52 34\r\n>") == ["C1234"]
    assert parse_dtcs("43 AA AA\r\n>") == ["B2AAA"]


def test_dtc_stops_at_terminator():
    assert parse_dtcs("43 01 71 00 00\r\n>") == ["P0171"]


def test_dtc_multiframe_numbered():
    assert parse_dtcs("0: 43 01 71 01 33 AA\r\n1: AA\r\n>") == \
        ["P0171", "P0133", "B2AAA"]


def test_dtc_with_headers():
    assert parse_dtcs("7E8 04 43 01 71 01 33\r\n>") == ["P0171", "P0133"]


# Service 07 / 0A use positive-response SIDs 0x47 / 0x4A, not 0x43.
# Regression test for the parser that rejected compliant 47/4A responses.
def test_dtc_pending_sid_47():
    assert parse_dtcs("47 01 71\r\n>", response_sid=0x47) == ["P0171"]
    assert parse_dtcs("47 00\r\n>", response_sid=0x47) == []


def test_dtc_permanent_sid_4a():
    assert parse_dtcs("4A 04 20\r\n>", response_sid=0x4A) == ["P0420"]
    assert parse_dtcs("4A 00\r\n>", response_sid=0x4A) == []


def test_dtc_wrong_sid_rejected():
    # A 47 response must not parse as service 03 (and vice versa).
    with pytest.raises(ValueError):
        parse_dtcs("47 01 71\r\n>")
    with pytest.raises(ValueError):
        parse_dtcs("43 01 71\r\n>", response_sid=0x47)


@pytest.mark.parametrize("pid,payload,expected", [
    (0x04, "3B", 59 * 100.0 / 255),       # engine load %
    (0x05, "80", 88.0),                   # coolant C
    (0x06, "8E", (142 - 128) * 100.0 / 128),  # STFT %
    (0x07, "9D", (157 - 128) * 100.0 / 128),  # LTFT %
    (0x08, "82", (130 - 128) * 100.0 / 128),
    (0x09, "83", (131 - 128) * 100.0 / 128),
    (0x0B, "3E", 62.0),                   # MAP kPa
    (0x0C, "1A F8", 1726.0),              # RPM
    (0x0D, "40", 64.0),                   # speed km/h
    (0x0E, "80", 0.0),                    # timing deg
    (0x10, "01 36", 3.10),                # MAF g/s
    (0x11, "2E", 46 * 100.0 / 255),       # throttle %
    (0x1F, "01 2C", 300.0),               # runtime s
    (0x21, "00 00", 0.0),                 # distance with MIL km
    (0x33, "65", 101.0),                  # baro kPa
])
def test_pid_decodes(pid, payload, expected):
    assert decode_pid(pid, payload) == pytest.approx(expected)


def test_pid_unsupported_raises():
    with pytest.raises(ValueError):
        decode_pid(0x2F, "00")


def test_pid_short_payload_raises():
    with pytest.raises(ValueError):
        decode_pid(0x0C, "1A")
