# Interface assumptions

Status: P0 semantic requirements and provisional interface design. Signal widths, event/register schemas, binary offsets, CRC framing, and compatibility version are not frozen. P1 supplies executable schemas and independent byte fixtures. The core-specific adapter can freeze only after qualification of the acquired core revision.

## Observation interfaces

Source taps are passive inputs to Chronos. A source cannot retry or stall an architectural event because capture storage is full. Each adapter must declare its clock/reset domain, observation boundary, reporting latency, maximum simultaneous observations, legal combinations, and overflow behavior.

Normalized events require source identity, source epoch, source sequence, observation tick, event type, and valid-field flags. A session manifest maps sources to harts, buses, and clock domains. Source identity is stable while armed; sequences advance before admission. Proposed V1 ticks and sequences are unsigned 64-bit values. Wrap requires a new explicit epoch or a stopped capture with status.

| Event class | Required semantic content | Boundary |
| --- | --- | --- |
| Retirement | PC, instruction length, next PC or explicit discontinuity, execution-boundary context | Architectural completion; speculative/fetched instructions are not retirement |
| Trap | Exception PC, cause, interrupt flag, trap value/destination when valid | Architectural trap; retirement is not required |
| Interrupt pending | Line/vector and asserted state | External input observation |
| Interrupt accepted | Architectural cause and hart boundary | Hart acceptance, distinct from pending |
| Bus request | Interface, transaction identity when known, address, size/direction, attributes/strobes | Accepted request handshake |
| Bus response | Transaction identity when known, status, optional beat/last information | Accepted response/completion handshake |
| User event | Stable event ID and bounded scalar payload | Declared user observation point |
| Loss/reset/synchronization | Scope, epochs, known time/sequence bounds, count certainty, restart information | Explicit reconstruction discontinuity or restart |
| Trigger/end | Reason, event identity/time when known, capture completeness | Mirrored records; independent metadata is authoritative |

DMA/cache/beat/counter events are later capabilities with separately qualified hooks and profiles. Data values are optional and do not establish a historical memory image.

Same-cycle request and response need separate lanes or a bundle. So do trap/IRQ combinations where both can occur. A single source valid bit cannot silently discard one event. The precise expansion from physical lanes to queued records is a P1 capacity input.

Retirement events carry an execution-boundary epoch or equivalent barrier set before arbitration. A trap or accepted interrupt must break a pending run even if its separate event queue overflows. Adapter qualification defines whether same-cycle retirement belongs before or after that boundary.

## Queued links and memory

Internal links use valid/ready acceptance with payload stable while blocked. Source sequence/time remain unchanged by queue residence. Round-robin service provides a bound in successful grants; a clock-cycle bound additionally requires a bounded downstream stall assumption.

The SRAM wrapper must specify port count, synchronous read latency, address units, write masks, same-address read/write behavior, and any read-during-write restrictions. Initial evaluation uses a 64-bit write path. Large memory contents are not reset as a register array; validity state controls visibility, with explicit scrub for clearing.

Page builders own unpublished pages. Complete header/payload writes precede directory commit; committed pinned or frozen pages are immutable. Directory generation and session identity prevent stale pages from appearing current.

## Control and readout

Use an abstract register request/response interface with a platform-specific bus bridge. Numeric offsets, access widths, response timing, and command priorities freeze in P1. The required logical groups are:

| Group | Required behavior |
| --- | --- |
| Identity/capability | Build/configuration identity and supported source/codec/memory limits |
| Configuration | Atomic commit while disabled/frozen; reject unsupported or unsafe values |
| Commands | Arm, software trigger, stop, deliberate clear; illegal-state behavior explicit |
| Triggers | Source/type qualification, masked equality, half-open address ranges, reason mask |
| Status | Capture state, snapshot/session identity, stop reason, incomplete/saturation/unknown flags |
| Accounting | Observed, filtered, admitted, ingress-dropped, overwritten history, queue high-water state |
| Snapshot metadata | Stable page order/generations, retained bounds, independent trigger and loss information |
| Readout | Bounded reads of a named frozen snapshot; reject stale identity and invalid storage |

Trigger matching precedes filtering and queue admission. Four predicate slots with OR combination are a sizing hypothesis. First-trigger metadata is sticky; simultaneous reasons are preserved. A descriptor indicates when its triggering event was not retained.

Post-trigger duration uses observation ticks. P1 defines inclusive boundaries, simultaneous command/event priorities, final-cycle admission, and terminal metadata. Queue fullness cannot suppress the authoritative trigger descriptor or silently erase terminal loss status.

## Encoding and host boundary

Source semantics, page/record encoding, and host session packaging have separate versions. Transport framing is independent of trace semantics. No current byte layout is a supported public protocol.

P1 evaluates little-endian bounded records, no record crossing a page, page-local codec restarts, and header/payload CRC-32/ISO-HDLC with fully specified byte order and vectors. A parser consumes the authoritative bounded snapshot directory; arbitrary magic-byte scanning is not the normal decode path. Unknown required semantics stop dependent reconstruction; optional records may be skipped only under the frozen compatibility contract.

Raw and compressed profiles must preserve identical supported event content and exact timestamps. PC runs require fixed length/stride, consecutive sequences, unchanged boundary context, and sequential next PC for every implicit member. Irregular retirement spacing must split a run or be represented explicitly. Loss, epoch change, maximum run length/age, page boundary, and forced flush end a run.

The proposed `.chronos` container has a fixed preamble, bounded UTF-8 JSON manifest, and framed pages. Manifest fields cover version/capabilities, build/configuration identity, snapshot/epoch, firmware and code-image hashes, source/domain mapping, trigger/loss/terminal status, directory/checksums, and provenance (`model`, `RTL simulation`, or `physical board`). A wrong executable permits raw-PC inspection but cannot yield verified symbolization. Decoder resource limits apply to both encoded input and expanded output.

V1 supports local offline use. Session data and symbols are untrusted input: no executable content, archive extraction, or unbounded allocation is required.

## Unresolved integration contracts

Ibex is a preferred qualification target, not an acquired dependency. Establish exact revision/configuration, synthesizable RVFI/sideband availability, reporting latency, trap-without-retirement behavior, accepted-interrupt cause visibility, native bus outstanding limits, and reset semantics before accepting its adapter. A simulation file tracer does not provide an FPGA observation interface.

No physical FPGA board is currently available. Development and initial demonstrations target simulation; board model/part, clock, pin constraints, memory allocation, programming path, and independent debug transport remain pending a later platform decision. Asynchronous clocks, AXI4, multiple harts, and standard trace profiles require later contracts and tests; they are not supported by this P0 baseline.
