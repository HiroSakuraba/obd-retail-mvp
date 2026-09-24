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

# Host stub-compile of the real ESP-IDF application sources (CI coverage
# for "the ESP32 app never compiled"): main_esp32.c, twai_transport.c and
# ble_gatt.c are compiled with -DESP_PLATFORM against stub ESP-IDF/NimBLE
# headers in stubs/. Compile-only (-c): catches missing includes,
# declarations and type errors without needing real ESP-IDF or hardware.
APP_SRCS = ../src/main_esp32.c \
           ../src/twai_transport.c \
           ../src/ble_gatt.c \
           ../src/obd_client.c \
           ../../src/iso15765.c \
           ../../src/obd_allowlist.c \
           ../../src/ble_protocol.c

STUB_INCLUDES = -Istubs -I../include -I../../include

.PHONY: check clean check-app

check: test_obd_client
	./test_obd_client

check-app:
	mkdir -p build-host
	for f in $(APP_SRCS); do \
	  $(CC) $(CFLAGS) -DESP_PLATFORM $(STUB_INCLUDES) -c $$f \
	      -o build-host/$$(basename $$f .c).o || exit 1; \
	done
	@echo "ESP-IDF app sources stub-compile OK"

clean:
	rm -f test_obd_client
	rm -rf build-host
