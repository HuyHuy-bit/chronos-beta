# Provisional configuration contract

`config.schema.json` defines the primitive field types, supported baseline profile, required keys, and scalar limits. `scripts/config.py` implements this schema's deliberately limited flat-object subset using the Python standard library. It is not a general JSON Schema engine and performs no remote schema resolution.

The validator additionally rejects duplicate JSON keys, non-power-of-two SRAM/FIFO sizes, incomplete page allocations, and records larger than page payload. A Boolean is not an integer configuration value.

The proposed baseline is in [configs/baseline.json](../configs/baseline.json): RV32, one retirement lane, four sources, a 64-bit sink, 32 KiB SRAM, and equal 16-page pre/post pools. `target_clock_hz` is an objective, not achieved timing. `board: null` and `target: simulation` describe the current platform.

Validation proves structural consistency only. It does not prove a lossless service envelope, safe post-trigger flush credit, or support for every accepted configuration in hardware. The executable model phase must freeze record bytes, codec restart state, actual source-lane widths, register offsets, and worst-case capacity accounting before RTL consumes those contracts. No event/register wire schema is presented as final in this phase.
