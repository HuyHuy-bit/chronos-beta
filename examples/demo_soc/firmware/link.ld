ENTRY(_start)
MEMORY { ram (rwx) : ORIGIN = 0, LENGTH = 64K }
SECTIONS {
    .text : { KEEP(*(.text.vectors)) *(.text .text.*) } > ram
    .rodata : { *(.rodata .rodata.* .srodata .srodata.*) } > ram
    .data : { *(.data .data.* .sdata .sdata.*) } > ram
    .bss : { *(.bss .bss.* .sbss .sbss.* COMMON) } > ram
    stack_top = ORIGIN(ram) + LENGTH(ram);
}
