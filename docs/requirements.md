# Product requirements

Status: P0 requirement baseline. Acceptance evidence below is required future work, not a list of passed checks. There is no Chronos capture datapath, acquired core, or physical demonstration yet.

The target user is an RTL, firmware, or validation engineer investigating intermittent SoC failures. The workflow is configure, arm, run, trigger, retrieve, and inspect retained instructions and transactions with explicit timing and completeness. Temporal proximity alone does not prove causality.

## V1 scope

- One RV32 hart and retirement lane, one capture clock, and a 32-bit-instruction workload profile with explicit unsupported-instruction detection.
- Architectural retirement/traps, distinct interrupt pending/acceptance observations, simple accepted bus requests/completions, and user events.
- Passive observation, exact source time/order/epochs, raw and lossless bounded compression, circular independently decodable history, and explicit losses.
- Autonomous masked/range/type/source triggers, retained first-trigger metadata, bounded post window, coherent frozen readout, and deliberate clear.
- Offline capture/decoder software, firmware identity checks, searchable timeline with visible gaps, and a reproducible saved physical FPGA capture.

The core's full ISA and integration capability are documented independently of the trace workload profile. The first demo may use firmware or a test bus master; genuine DMA is a later integration. Synthetic input is always labeled synthetic. No physical FPGA board is currently available: a simulation preview is the initial delivery target, while the physical V1 acceptance gate remains open until board access and validation exist.

## Requirement register

| ID | Requirement | Required acceptance evidence | Target phase |
| --- | --- | --- | --- |
| OBS-01 | Architectural trace observes retirement/trap boundaries, never speculative execution as retirement | Adapter scoreboard against independent commit/trap log, including flush cases | P4 |
| OBS-02 | Type, source, epoch, order, and observation time have explicit semantics | Executable schema and independent semantic fixtures | P1 |
| OBS-03 | Disabled, filtered, dropped, overwritten, and unknown data are distinguishable | Counter conservation and decoder/UI checks | P3–P5 |
| OBS-04 | Same-cycle events coexist within the declared input width | All enabled-source collision cases | P2 |
| CAP-01 | Passive capture cannot drive CPU stalls or observed bus handshakes | Port/netlist review and enabled/disabled equivalence | P2/P4 |
| CAP-02 | Overload never produces apparently continuous corrupted trace | Saturation, loss markers, resynchronization tests | P3 |
| CAP-03 | Oldest exported history begins at a valid decode boundary | Every ring start and page wrap exercised | P3 |
| CAP-04 | Trigger reason survives ordinary FIFO congestion | Full-queue trigger and independent descriptor readback | P3 |
| CAP-05 | Frozen snapshots are coherent and stable | Drain/commit/readout assertions and repeat-read equality | P3 |
| CAP-06 | Reset and retention boundaries are explicit | Source-only, trace, and system-reset matrix | P3/P7 |
| CMP-01 | Lossless and raw modes reconstruct identical supported content and times | Independent decode against source oracle | P1/P3 |
| CMP-02 | Compression expansion, state, latency, and flush time are bounded | Worst-case model and RTL checks | P3 |
| TRG-01 | Trigger timing follows observation time and explicit ordering/boundaries | Reference trigger model and N-1/N/N+1 cases | P3/P6 |
| CDC-01 | Crossings preserve accepted payload and per-source order | CDC/timing review, formal safety, asynchronous stress | P7 |
| CDC-02 | Host never invents exact cross-domain order | Overlapping intervals, changing and stopped clocks | P7 |
| BUS-01 | AXI requests/responses match only through legal protocol relationships | Independent channel scoreboard, reused IDs, resets, overflow | P6 |
| HST-01 | Captures decode offline with format and firmware checks | CLI fixtures and end-to-end sessions | P4/P5 |
| HST-02 | Malformed input cannot cause unbounded allocation or execute content | Resource limits, corruption/fuzz corpus, hostile symbols | P4/P5 |
| PHY-01 | Advertised FPGA clock/resources derive from the actual routed configuration | Timing/utilization reports and board manifest | P5 |
| REP-01 | Fresh public checkout reproduces claimed checks independently | Clean-checkout offline verification with declared prerequisites | Each release |
| REP-02 | Every implementation phase ends with an evidence report | Commands, results, limitations, and next gate | Every phase |

## Quality and evidence rules

Publish only tested configurations: hart/source/lane counts, clock domains, bus profile, memory size, trigger slots, codecs, rates, and protocol versions. Parameterized RTL alone does not qualify every parameter combination. Favor correct loss reporting and restart integrity over compression ratio.

Trace data may contain code, addresses, values, and secrets. Capture starts disabled, data-value tracing defaults off, and platform authorization/clear semantics are explicit. Offline local use is the baseline; a network service and secure authentication are separate extensions.

Distinguish observed events, supported reconstructed control flow, legally associated transactions, and temporal correlation. Architectural replay requires checkpoints and complete nondeterministic inputs and is outside V1. Bus activity alone does not prove cache visibility or historical memory state.

Physical claims have separate gates: synthesis demonstrates logic feasibility, routed reports demonstrate the constrained FPGA implementation, and a real saved board capture demonstrates physical operation. ASIC logic studies, qualified SRAM-backed implementation, and full tapeout signoff remain separate evidence levels.

## Later scope and non-goals

V2 adds AXI4, real DMA/cache sources where hooks exist, temporal triggers, and asynchronous-domain time intervals. V3 adds multiple harts, a selected qualified E-Trace profile, optional measured-need DDR streaming, and release benchmarks. Neither custom container naming nor partial standard support establishes compliance.

RVC/RV64 profiles, Linux/MMU tracing, coherent multicore, extra bus families, full data trace, dictionaries, N-Trace/JTAG standards integration, remote access, checkpoint/replay, and tapeout are separate extensions. They do not waive V1 correctness or physical-evidence gates.

See [architecture](architecture.md), [interfaces](interfaces.md), and [verification](verification.md) for the required behavior and validation approach.
