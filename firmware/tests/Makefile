CC = gcc
CFLAGS = -std=c99 -Wall -Wextra -Werror -I../include
SRC = test_allowlist.c ../src/obd_allowlist.c ../src/ble_protocol.c

.PHONY: all test clean

all: test_allowlist

test_allowlist: $(SRC)
	$(CC) $(CFLAGS) -o $@ $(SRC)

test: test_allowlist
	./test_allowlist

clean:
	rm -f test_allowlist
