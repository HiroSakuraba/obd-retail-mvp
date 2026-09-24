# Host test build for the ESP32 OBD client.
#
# Compiles the portable firmware sources (obd_client.c + the existing
# firmware C library) with the TCP test transport and runs the suite
# against simulator/can_ecu_sim.py -- no ESP32 hardware needed.
#
#   make check   # build + run the full suite

CC      ?= gcc
CFLAGS  ?= -std=c99 -Wall -Wextra -Werror -g -O1
INCLUDES = -I../include -I../../include

FW_SRCS = ../src/obd_client.c \
          ../src/tcp_transport.c \
          ../../src/iso15765.c \
          ../../src/obd_allowlist.c \
          ../../src/ble_protocol.c

test_obd_client: test_obd_client.c $(FW_SRCS)
	$(CC) $(CFLAGS) $(INCLUDES) -o $@ test_obd_client.c $(FW_SRCS) -lm

.PHONY: check clean

check: test_obd_client
	./test_obd_client

clean:
	rm -f test_obd_client
