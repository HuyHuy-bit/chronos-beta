# ADR 0001: Passive single-clock baseline with an existing core

- Status: accepted for project scope; numerical sizing and core/platform qualification remain provisional
- Phase: P0
- Date: 2026-09-08

## Context

Chronos needs to demonstrate trustworthy retained execution and bus history around a failure. Developing another CPU would add a separate ISA-verification project. Introducing multiple asynchronous domains, standards encoding, and live streaming before an exact raw path would obscure the capture correctness gate.

## Decision

Build the portable capture IP independently of a CPU. Start with one RV32 hart, one retirement lane, a single capture clock, and a 32-bit-instruction workload profile. Preserve exact observation times, source sequences/epochs, architectural boundaries, explicit gaps, independently decodable pages, and trigger metadata outside ordinary queues. Keep raw mode as the comparison baseline for later bounded lossless codecs.

Use passive CPU/bus taps and a synchronous SRAM sink. Qualify the model and synthetic RTL path before the full firmware/host/board demonstration. Declare simultaneous-input capacity and derive admission/flush capacity using worst-case encoding, including restart, padding, and metadata.

Prefer Ibex for the first core-qualification spike. Its [upstream repository](https://github.com/lowRISC/ibex), [integration documentation](https://ibex-core.readthedocs.io/en/latest/02_user/integration.html), and [RVFI documentation](https://ibex-core.readthedocs.io/en/latest/03_reference/rvfi.html) identify the candidate integration surfaces. These links are references, not revision-specific qualification evidence. The [candidate manifest](../../third_party/manifest.json) records a revision observed through read-only remote metadata. No source has been acquired, no revision has been integration-qualified, and no Chronos adapter is certified.

Acquisition requires prior approval for the bounded core/dependency set. Pin the full source dependency closure and preserve required upstream licensing/notices. Verify a chosen configuration's synthesizable retirement/trap/accepted-interrupt sideband, reporting latency, unsupported instruction handling, and native bus handshakes/outstanding limits. A simulation file tracer is insufficient for hardware ingress.

PicoRV32 is a possible reduced-demo fallback only after an explicit revised contract; different interrupt/trap semantics cannot silently replace the required baseline. Existing local CPUs are not automatic dependencies.

## Consequences

P1 core-independent format/capacity work can proceed while core qualification is pending. Its event/register/wire schemas must freeze before P2 consumes them. The core-specific signal mapping remains provisional until qualification, and P4 cannot pass without it.

V1 does not promise architectural checkpoint/replay, RVC/RV64 profiles, Linux/MMU tracing, coherent multicore, asynchronous time correlation, or standard protocol compliance. Later phases add each through explicit contracts and evidence. A CPU that implements compressed instructions can still run the restricted demo workload; Chronos must detect unsupported observed lengths.

No physical FPGA board is currently available, so initial implementation and demonstrations target simulation. The physical FPGA gate remains pending later board access; no board purchase is a prerequisite for starting the project. Exact part, memory fit, clock, transport, and license choice require separate recorded decisions. Initial SRAM/page/queue sizes and 50 MHz clock objective are hypotheses, not measured implementation claims.

## Revisit criteria

Revisit the core choice if qualification cannot provide synthesizable architectural boundary/cause information or if its dependency/resource cost prevents the target demonstration. Revisit storage/lanes/widths if P1 worst-case expansion or post-reserve analysis fails. Preserve loss visibility, exact supported timing, page restart integrity, and independent trigger retention through any revision.
