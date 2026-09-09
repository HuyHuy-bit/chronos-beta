.section .text.start
.global _start
_start:
    la sp, __stack_top
    call smoke_main
1:
    j 1b
