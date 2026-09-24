/* power.c - sleep/wake power management.
 *
 * The dongle spends most of its life asleep on the OBD port's battery pin.
 * These functions own the policy (when to sleep, what wakes us); the
 * platform_* hooks below are where a board port wires in the actual
 * register writes for its MCU. Nothing here transmits on CAN.
 */

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    POWER_ACTIVE,
    POWER_SLEEP
} power_state_t;

static volatile power_state_t g_power_state = POWER_SLEEP;
static volatile uint32_t g_sleep_count = 0;

/* --- platform hooks: implement per MCU -------------------------------- */
static void platform_enter_deep_sleep(void)
{
    /* Board port: configure wake sources (BLE connect event, button),
       shut down the CAN transceiver standby pin, then WFI/WFE. */
}

static void platform_on_wake(void)
{
    /* Board port: restore clocks, re-enable the CAN transceiver. */
}
/* ---------------------------------------------------------------------- */

void power_init(void)
{
    g_power_state = POWER_SLEEP;
    g_sleep_count = 0;
}

/* Called when BLE disconnects or the idle timer expires. */
void power_enter_sleep(void)
{
    g_power_state = POWER_SLEEP;
    g_sleep_count++;
    platform_enter_deep_sleep();
}

/* Called from the BLE-connected interrupt/event. Returns true if we were
 * asleep and are now active. */
bool power_wake_on_ble(void)
{
    if (g_power_state == POWER_SLEEP) {
        platform_on_wake();
        g_power_state = POWER_ACTIVE;
        return true;
    }
    return false;
}

power_state_t power_state(void)
{
    return g_power_state;
}

uint32_t power_sleep_count(void)
{
    return g_sleep_count;
}
