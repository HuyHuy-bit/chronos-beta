// Arms Chronos, runs a checksum workload, stops, and waits for the frozen snapshot. Register offsets and field values
// come from spec/registers.json as -D flags (scripts/soc_check.py).
#define CHRONOS(offset) (*(volatile unsigned *)(0x10000000u + (offset)))

static unsigned table[64];

unsigned main(void) {
    CHRONOS(REG_CFG_SPLIT) = CFG_SPLIT;
    CHRONOS(REG_CFG_MODE) = CFG_MODE;
    CHRONOS(REG_COMMAND) = CMD_ARM;
    for (unsigned i = 0; i < 64; i++)
        table[i] = i * 2654435761u;
    unsigned sum = 0;
    for (unsigned round = 0; round < 8; round++)
        for (unsigned i = 0; i < 64; i++)
            table[i] = sum = (sum << 1 | sum >> 31) ^ table[i] ^ round;
    // A quiet tail the capture can keep up with: one store and one load between straight-line runs.
    for (unsigned i = 0; i < 16; i++) {
        table[i] = i;
        sum += table[i + 16];
        __asm__ volatile(".rept 60\n nop\n .endr");
    }
    CHRONOS(REG_COMMAND) = CMD_STOP;
    while ((CHRONOS(REG_STATUS) & 7) != STATE_FROZEN)
        ;
    return sum;
}
