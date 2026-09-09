OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY { RAM (rwx) : ORIGIN = 0x1000, LENGTH = 16K }
SECTIONS {
    .text : { *(.text.start) *(.text*) } > RAM
    .rodata : { *(.rodata*) } > RAM
    .data : { *(.data*) *(.sdata*) } > RAM
    .bss (NOLOAD) : { *(.bss*) *(.sbss*) *(COMMON) } > RAM
    __stack_top = ORIGIN(RAM) + LENGTH(RAM);
}
