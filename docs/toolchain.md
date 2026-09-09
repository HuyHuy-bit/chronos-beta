# Toolchain and dependency contract

The default foundation check uses existing Python, Git, Make, a C++ compiler, Verilator, and RISC-V GCC/binutils. All build commands are offline. Missing required tools fail the check; missing optional tools are reported without implying successful validation.

`make doctor` records executable locations and version probes in `build/doctor.json`. `make foundation-check` records source SHA-256 values, configuration status, tool identities, and results in `build/foundation.json`. Receipts begin in a failed state and are marked passed only after checks finish. Generated receipts and local paths remain outside source control.

Yosys is optional for the repository foundation and required for `make synth-smoke`. The synthesis probe is a small counter, not Chronos area or timing evidence. SymbiYosys/solvers may be inventoried but no Chronos formal proof exists at this stage. Vivado and FPGA hardware are not prerequisites for the model work.

Point `OSS_CAD_SUITE` at the root of an already installed suite to discover its `bin/` executables after checking normal PATH. Explicit tool overrides take precedence and must name executables. An invalid explicit override does not fall back to another tool silently. A relative override is resolved from the project invocation directory.

## Acquisition register

| Candidate | Purpose | State |
| --- | --- | --- |
| Ibex source at the revision in the manifest | Qualify retirement/trap taps and a demo integration | Not acquired; acquisition approval needed |
| Ibex packages/primitives/build helpers | Complete the selected core's source closure | Determine from approved source inspection; no automatic recursive fetch |
| ELF library | Rich symbolization later | Optional; baseline uses binutils/standard-library ELF header checks |
| Additional test/frontend packages | Only if they improve a concrete next task | No baseline acquisition required |
| FPGA tools/board files and hardware | Qualify a physical target | Board selection remains pending |
| PDKs, SRAM views, physical tools | Later technology-specific implementation | Outside current phase |

Read-only remote revision lookup does not acquire source and does not validate its contents. The [third-party manifest](../third_party/manifest.json) distinguishes candidates from acquired, licensed, qualified dependencies. Before acquisition, specify source/revision, purpose, destination, storage if known, and transitive download behavior. Preserve all upstream notices and record modifications separately.

## License decision

The license for newly authored RTL/software remains an owner decision before public distribution. Continue local implementation without an invented license grant. Upstream core licensing cannot supply a license for independently authored Chronos files, and any imported notices must be retained. A future release must resolve this decision and its third-party manifest before publication.
