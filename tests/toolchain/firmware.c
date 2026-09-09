volatile unsigned int result;

_Static_assert(sizeof(unsigned int) == 4, "32-bit unsigned int required");

void smoke_main(void) {
    unsigned int sum = 0;
    for (unsigned int i = 0; i < 16; ++i)
        sum += i;
    result = sum;
}
