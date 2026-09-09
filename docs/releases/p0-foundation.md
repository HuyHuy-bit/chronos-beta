# P0: repository and tool foundation

Date: 2026-09-08. Status: foundation gate passed; core qualification remains open. No Chronos capture datapath or CPU integration is implemented yet.

## Delivered

- Independent local repository and source/output exclusion rules.
- Public architecture, requirement IDs, interface assumptions, verification gates, and an initial architecture decision.
- Provisional JSON configuration and structural validator with negative cases.
- Offline tool inventory, self-checking Verilator probe, RV32 compile/link/ELF checks, and optional Yosys synthesis probe.
- Candidate core manifest, dependency/license decisions, ADR and phase report templates.

## Executed checks

The complete check used the existing OSS CAD Suite through its explicit `OSS_CAD_SUITE` path. No package or source acquisition was needed.

```bash
make foundation-check
```

| Check | Result |
| --- | --- |
| Configuration suite | 4 test methods; 14 rejected invalid-input cases plus the valid baseline |
| Verilator counter probe | 600 checked loop cycles, including hold and wrap; reset checked before and after |
| RV32 firmware compilation | ELF32, little-endian, RISC-V executable, entry `0x1000`, 9 disassembled 32-bit instructions |
| Yosys generic synthesis probe | Passed, including structural check |
| Isolated public-source copy | Fresh build passed without private planning files or existing build output |
| Invalid required-tool override | Failed as required; did not silently use another executable |
| Unavailable explicitly requested synthesis | Failed as required |

The RV32 firmware was compiled and inspected, not executed on a core. Counter simulation and synthesis validate toolchain operation only. No CPU ISA, Chronos function, FPGA timing, ASIC area, or formal proof claim follows from these probes.

Locally executed versions include Python 3.10.12, Verilator 5.048 (`v5.048-56-gc233a3905`), GCC/G++ 11.4.0 for host compilation, RISC-V GCC 10.2.0, binutils 2.35.1, and Yosys 0.68+195 (`435977e97-dirty`). The reported dirty tool revision is preserved as observed. SymbiYosys 0.68 and available solvers were version-probed only.

The [compact evidence record](../../evidence/p0-foundation.json) records source hashes and result scope. Fresh runs write local receipts/logs to `build/`. Source hashes exclude generated evidence/release summaries to avoid self-referential receipts.

## Pending gates

- Acquire the pinned Ibex candidate only after approval, inspect dependency/license closure, and qualify synthesizable retirement/trap/bus signals.
- Select a distribution license for newly authored files before publication.
- Physical FPGA validation awaits board access and target/tool qualification. Current development is simulation-first.
- Freeze wire bytes, register offsets, actual lane expansion, and capacity/service proofs during P1; passing the structural configuration validator does not establish those properties.

## Next scope

Finish the core-qualification subgate when source acquisition is approved. The foundation supports subsequent core-independent P1 format/capacity modeling without board access.
