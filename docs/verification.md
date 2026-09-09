# Verification strategy

Status: P0 verification contract. This document specifies future acceptance work; it does not report passing Chronos datapath, core-integration, formal, synthesis, or board tests. P0 tool smoke results must be reported separately from product verification.

## Evidence chain

P1 generates semantic observations independently of encoded bytes and checks raw/optimized models against an independently structured decoder. Preserve manually calculated literal-byte fixtures to catch common schema and endianness mistakes. A trigger/retention model predicts eligible observations, losses, and retained pages without translating the RTL state machine.

P2 adds an RTL driver that records its oracle outside Chronos, injects source collisions and sink stalls, and compares the production decoder's output against the retained oracle subset. A separate storage scoreboard checks page ownership, commit, eviction, checksums, and snapshot stability. P4 adds an independent architectural commit/trap log and bus scoreboard; adapter inputs cannot be the only architectural reference.

Every result names source revision, configuration, tool version, exact command, seed/input, expected outcome, and observed outcome. Missing tools leave checks unavailable; skipped checks cannot count as passed. Model throughput/compression is not RTL throughput or FPGA evidence.

## Accounting and invariants

For an armed source epoch with exact nonsaturated counting:

`observed = filtered + admitted + ingress_dropped`

Track admitted work through queues, encoding, retention, overwrite, explicit discard, and export without double counting. Ring eviction is distinct from ingress loss. At freeze, pending work is zero or explicitly accounted for as incomplete. Unknown/saturated counters suspend exact conservation claims.

Required properties include stable stalled internal payloads, FIFO accepted order, one arbitration grant per output, complete bounded records, page-local decode restarts, no writes to committed pinned/frozen pages, sticky first-trigger metadata, configuration stability while armed, and finite completion under declared clock/service assumptions.

Check trigger and loss metadata during queue saturation, including terminal loss without a following ordinary record. A lost trap event must still terminate retirement runs through observation-side boundary context.

## Directed coverage

| Area | Required scenarios |
| --- | --- |
| Observation | Idle, each lane, all legal simultaneous lanes, retire/trap coincidence, exception without retirement |
| CPU integration | Sequential and branching code, indirect target, call/return, stalls/flushes, fault, accepted interrupt, trap return, unsupported instruction length |
| Codec/time | Absolute/delta limits, signed-address edges, raw escape, irregular retirement spacing, changed final next PC, max run count/age, epoch/wrap/loss flush |
| Pages/ring | Empty, exact fit, tail padding, every retained starting page, repeated wraps, generation boundaries, partial page at trigger/reset |
| Trigger/control | Filtered matching event, full queues, simultaneous reasons, zero post cycles, N-1/N/N+1 boundaries, capacity stop, command races, invalid configuration |
| Reset/readout | Source/trace/system reset, reset during build/trigger/readout, repeated frozen reads, stale snapshot, clear/rearm |
| Loss | Single/burst loss, filtering mixed with overflow, journal saturation, unknown count, resynchronization, overwritten request with retained response |
| Host/transport | Wrong ELF/version, unknown required/optional records, corrupt checksums, truncation, absurd lengths/counts, hostile symbols, partial/lost/duplicate frames and retry |
| Later AXI4 | Independent AW/W timing including W first, concurrent reads/writes, reused IDs, bursts/last/strobes, interleaved responses, reset and tracker overflow |
| Later CDC | Swept phases/ratios, nearly equal clocks, full/empty crossings, stop/restart, asymmetric reset, frequency change, ambiguous intervals |

Explicitly combine known hazards: trigger/wrap/full queues; stop/run flush/page tail; source reset/outstanding transaction; corruption adjacent to a restart page. Coverage percentage does not replace these behavioral checks.

## Random and adversarial campaigns

Initial P1 targets are at least 10,000 short seeded streams per codec family plus curated boundaries. For RTL, start with directed cases and approximately 20 deterministic per-change stress seeds. A phase gate targets at least 200 varied seeds over supported configurations and 10 million aggregate main-datapath observations. Before V1, target 100,000 bounded malformed-input mutations plus explicit decoded-expansion limits. Record actual executed counts and runtime; these are future budgets, not current results or a proof by volume.

Temporarily mutate loss flags, timestamps, ring behavior, and trigger retention to establish that scoreboards detect these failures. Preserve a minimized failing semantic input, seed, configuration, and compact trace; large waveforms are generated artifacts.

## Formal, CDC, and physical checks

Use assertions from P2. With available approved formal tools, prove bounded FIFO, arbitration, packetizer, page-lifetime, trigger, and capture-controller properties under explicit assumptions. Fairness in successful grants does not imply a time bound when downstream service can stop indefinitely.

Later CDC proofs preserve accepted records and source order within uninterrupted coordinated epochs. Check one-bit transitions on source-registered Gray pointers; destination samples may skip multiple increments. Digital simulation and formal safety do not establish analog metastability reliability. CDC/RDC structure, synchronizer constraints, reset sequencing, and physical timing review remain separate gates.

For the actual core integration, compare identical deterministic firmware/stimuli with trace disabled and enabled: architectural output, retirement order, bus handshakes, and completion cycles must match. This evidence applies to that integration and does not imply zero routing, power, or timing cost. A later DMA sink requires separate shared-bus interference measurements.

FPGA evidence names exact part/configuration, clock constraints, RAM inference, unconstrained endpoints, setup/hold results, CDC exceptions, and routed resources. A programmed bitstream or LED alone cannot qualify the debugger; retrieve and independently decode a real capture. ASIC studies explicitly state memory treatment, technology/corners, and unperformed signoff checks.

## Phase and release gates

| Phase | Required result |
| --- | --- |
| P0 | Reproducible project/tool foundation, public requirements and provisional interfaces, explicit unavailable/pending decisions |
| P1 | Frozen core-independent schema/bytes, exact semantic/time round trips, independent fixtures, restart proof cases, worst-case capacity and supported input envelope |
| P2 | Synthetic observations through RTL memory to decoder; collisions/backpressure/order/noninterference checks |
| P3 | Verified history, compression, trigger preservation, loss reporting, bounded drain, immutable snapshots |
| P4 | Qualified real-core adapter and independently checked firmware-driven simulated forensic session |
| P5 | Actual board capture, routed evidence, usable offline viewer, fresh-checkout reproduction |
| Later | Separate AXI4, CDC, multicore/standards, streaming, and physical-study acceptance |

CI should separate quick model/lint/RTL checks from scheduled stress and controlled physical runs. Pin dependencies/actions, minimize permissions, and fail or explicitly mark unavailable required checks. No check may silently fetch missing software. Each phase report includes what works, exact executed validation, limitations, open gates, and the next phase scope.

The requirement IDs in [requirements](requirements.md) are the stable mapping for fixtures, tests, and reports. Wire/semantic fixtures and executable mappings arrive in P1; their absence in P0 must remain explicit.
