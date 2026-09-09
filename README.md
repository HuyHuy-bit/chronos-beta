# Chronos

Hardware execution-history capture and forensic debugging for RISC-V SoCs.

Chronos is being built to record selected CPU and SoC observations in a circular trace memory, preserve the history around a trigger, and decode that history on a host. The initial profile targets one RV32 hart and one clock domain. Missing observations and unsupported reconstruction must remain explicit.

## Current status

The repository foundation is implemented: provisional configuration validation, standalone architecture/verification contracts, and offline toolchain probes. The probe simulates a small counter and compiles a freestanding RV32 ELF; it is not the Chronos datapath or an executing RISC-V SoC.

Ibex is the preferred future demo core, pending acquisition and interface qualification. No third-party CPU source is included. The current target is simulation; no FPGA board has been selected or demonstrated. Packet bytes and core-specific interfaces remain provisional until the executable model phase.

## Run the foundation checks

Required tools: Python 3.10 or newer, Git, Make, a C++ compiler, Verilator, and RISC-V GCC/binutils capable of RV32I/ILP32 output. These commands use existing tools and do not install or download dependencies.

```bash
make doctor
make foundation-check
```

Results and logs are written under `build/`. To include the optional Yosys synthesis probe from an existing OSS CAD Suite installation:

```bash
OSS_CAD_SUITE=/absolute/path/to/oss-cad-suite make foundation-check
```

Individual executable overrides include `VERILATOR`, `CXX`, `RISCV_CC`, `RISCV_OBJDUMP`, and `YOSYS`. Overrides name executable paths, not shell commands with flags. `make synth-smoke` fails if no Yosys is configured; the foundation gate reports an unavailable optional synthesis probe separately.

## Technical contracts

- [Architecture](docs/architecture.md)
- [Requirements](docs/requirements.md)
- [Interfaces](docs/interfaces.md)
- [Verification and gates](docs/verification.md)
- [Configuration](spec/README.md)
- [Toolchain and dependencies](docs/toolchain.md)
- [Baseline decision](docs/adr/0001-baseline.md)

The first product release will reconstruct recorded observations and supported instruction flow. Arbitrary historical register/memory restoration and deterministic replay require additional mechanisms and are outside that baseline.

## Ownership and licensing

The intended project scope is Chronos trace IP, host software, verification, and a reference SoC integration. An imported CPU will retain its upstream license and notices. A distribution license for newly authored project files has not yet been selected; no license grant is implied by a placeholder file or by this development repository.
