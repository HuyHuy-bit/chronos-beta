# Ibex boot layout: trap vectors at 0, reset at 0x80. main's return value, or mcause after an unexpected trap,
# is written to the halt register.
    .section .text.vectors, "ax"
    .option norvc
    .globl _start
    .rept 32
    j trap
    .endr
_start:
    la sp, stack_top
    call main
halt:
    li t0, 0x20000000
    sw a0, 0(t0)
1:  j 1b
trap:
    csrr a0, mcause
    j halt
