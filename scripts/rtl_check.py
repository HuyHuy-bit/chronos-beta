from dataclasses import dataclass, field
from itertools import combinations
import json
import math
import random
import struct
import subprocess
import sys

from model.chronos import compact_decode, compact_encode, raw_decode, raw_encode
from model.chronos.capacity import completion_budget, measured_inventory, service_envelope
from model.chronos.events import Event, Observation, normalize
from model.chronos.predicates import KINDS, matcher, slot_mask
from model.chronos.raw_session import decode_fragment, encode_fragment
from model.chronos.registers import LANES, MAP, pack, unpack
from scripts.config import ROOT, read_json
from scripts.doctor import tool_path
from scripts.model_check import fingerprints

BUILD = ROOT / 'build/rtl'
SOURCES = [ROOT / path for path in ('rtl/common/chronos_pkg.sv', 'rtl/common/trace_fifo.sv', 'rtl/common/trace_sram.sv',
                                    'rtl/capture/trace_ingress.sv', 'rtl/capture/trace_page_writer.sv',
                                    'rtl/capture/chronos_capture.sv')]
SRAM_BYTES = 32768
SIZES = (256, 1024, 4096)
SLOTS = {'RETIRE': 0, 'BUS_RESP': 2, 'BUS_REQ': 3, 'TRAP': 4, 'IRQ_ACCEPT': 4, 'IRQ_PENDING': 5, 'USER_EVENT': 6}
COUNTERS = ('observed', 'filtered', 'admitted', 'ingress_dropped', 'fifo_dropped', 'capacity_dropped', 'reset_discarded')
ADDRESS = {name: register['offset'] for name, register in MAP['registers'].items()}
CONFIG = read_json(ROOT / 'configs/baseline.json')
FOREVER = (1 << 64) - 1
CODECS = {'raw-v1': (raw_encode, raw_decode), 'compact-v1': (compact_encode, compact_decode)}
# Straight-line phases (stride, retirements): the 255-member cap, both sides of the 256-tick age limit, and no run.
PHASES = ((1, 300), (2, 131), (3, 88), (100, 5), (257, 2), (1, 1), (1, 2), (1, 20))


def geometry(page_bytes, post):
    pages = SRAM_BYTES // page_bytes
    return dict(CONFIG, page_bytes=page_bytes, pre_pages=pages - post, post_pages=post)


def smallest_safe(page_bytes):
    return next(post for post in range(1, SRAM_BYTES // page_bytes)
                if completion_budget(geometry(page_bytes, post), measured_inventory(geometry(page_bytes, post), 'raw-v1'))['safe'])


@dataclass
class Scenario:
    name: str
    cycles: list = field(default_factory=list)
    keep: tuple = KINDS
    triggers: list = field(default_factory=list)
    post_ticks: int = FOREVER
    post: object = lambda page_bytes: SRAM_BYTES // page_bytes // 2
    sizes: tuple = (1024,)
    drain_ready: int = 100
    seed: int = 1
    zero_drop: bool = True
    stop: str = 'manual'

    def cycle(self, *items, arm=False, stop=False, clear=False, soft=False, reset=None, trace=False, ready=True, repeat=1):
        self.cycles += [dict(items=list(items), arm=arm, stop=stop, clear=clear, soft=soft, reset=reset, trace=trace,
                             ready=ready)] * repeat


def run(arguments, log):
    with open(log, 'w') as output:
        result = subprocess.run([str(item) for item in arguments], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}); see {log.relative_to(ROOT)}')
    return log.read_text()


def retire(pc, target=None):
    return Observation('RETIRE', dict(pc=pc, next_pc=pc + 4 if target is None else target, length=4, boundary=0))


def request(transaction, address, write=False):
    return Observation('BUS_REQ', dict(transaction=transaction, address=address, write=write, data=transaction, mask=15))


def response(transaction, data=None, error=False):
    return Observation('BUS_RESP', dict(transaction=transaction, data=data, error=error))


def boundary(kind, cause, pc=None, target=None):
    return Observation(kind, dict(pc=pc, cause=cause, target=target, boundary=cause))


def pending(previous, current):
    return Observation('IRQ_PENDING', dict(previous=previous, current=current))


def user(value):
    return Observation('USER_EVENT', dict(value=value))


def six(index):
    kind = 'TRAP' if index % 2 else 'IRQ_ACCEPT'
    return (retire(0x1000 + 4 * index), response(index, index), request(index + 1, 0x8000 + 4 * index, True),
            boundary(kind, index, 0x2000 + index, None if index % 3 else 0x100), pending(index, index + 1), user(index))


def program(rng, phases):
    cycles, pc, address = [], 0x40000000, 0x40000000
    for _ in range(phases):
        (stride, length), mark = rng.choice(PHASES), rng.choice((0, 0, 1))
        noise, ready, jump = rng.choice((0, 0, 0.05)), rng.random() < 0.9, rng.random() < 0.5
        for index in range(length * stride):
            items = []
            if index % stride == 0:
                end = jump and index == (length - 1) * stride
                target = pc + (rng.choice((-0x8000, 0x7FFC, 0x8000, 0x12340)) if end else 4)
                items.append(Observation('RETIRE', dict(pc=pc, next_pc=target, length=4, boundary=mark)))
                pc = target
            if rng.random() < noise:
                address += rng.choice((4, -0x8000, 0x7FFF, 0x8000))
                items.append(request(index, address, rng.random() < 0.2))
            if rng.random() < noise / 3:
                items.append(rng.choice((response(index), boundary('TRAP', 1, pc, 0x100))))
            cycles.append((items, ready))
    return cycles


def random_items(rng, probability):
    items = []
    for kind in ('RETIRE', 'BUS_RESP', 'BUS_REQ', 'BOUNDARY', 'IRQ_PENDING', 'USER_EVENT'):
        if rng.random() >= probability:
            continue
        value = rng.getrandbits(32)
        if kind == 'RETIRE':
            items.append(retire(value & 0xFFFFFFF0, rng.getrandbits(32) if rng.random() < 0.2 else None))
        elif kind == 'BUS_RESP':
            items.append(response(value, rng.choice((None, rng.getrandbits(32))), rng.random() < 0.1))
        elif kind == 'BUS_REQ':
            items.append(request(value, rng.getrandbits(32), rng.random() < 0.5))
        elif kind == 'BOUNDARY':
            items.append(boundary(rng.choice(('TRAP', 'IRQ_ACCEPT')), value, rng.choice((None, rng.getrandbits(32))),
                                  rng.choice((None, rng.getrandbits(32)))))
        elif kind == 'IRQ_PENDING':
            items.append(pending(value, rng.getrandbits(32)))
        else:
            items.append(user(value & 0xFFFF))
    return items


def scenarios():
    result = []
    each = Scenario('each-kind', sizes=SIZES)
    each.cycle(arm=True)
    for items in ([retire(0x1000)], [response(1, None, True), request(2, 0x8000)], [request(3, 0x8004, True)],
                  [boundary('TRAP', 2, None, 0x100), pending(0, 1)], [boundary('IRQ_ACCEPT', 7, 0x2000, None)],
                  [pending(1, 0)], [user(0xFFFFFFFF)], [response(4, 0xDEADBEEF)], []):
        each.cycle(*items)
    each.cycle(stop=True)
    result.append(each)

    lanes = Scenario('six-lanes')
    lanes.cycle(arm=True)
    for index in range(6):
        lanes.cycle(*six(index))
    lanes.cycle(stop=True)
    result.append(lanes)

    rng = random.Random(0x52544C)
    stalls = Scenario('random-stalls', sizes=SIZES, drain_ready=50, seed=7)
    stalls.cycle(arm=True)
    for _ in range(12000):
        stalls.cycle(*random_items(rng, 0.006), ready=rng.random() < 0.6)
    stalls.cycle(stop=True)
    result.append(stalls)

    overload = Scenario('overload', sizes=SIZES, zero_drop=False)
    overload.cycle(arm=True)
    for index in range(200):
        overload.cycle(*six(index))
    overload.cycle(stop=True)
    result.append(overload)

    backlog = Scenario('stop-backlog', drain_ready=25, seed=3)
    backlog.cycle(arm=True)
    for index in range(3):
        backlog.cycle(*six(index), ready=False)
    backlog.cycle(stop=True, ready=False)
    result.append(backlog)

    kept = tuple(kind for kind in KINDS if kind not in ('BUS_RESP', 'IRQ_PENDING', 'USER_EVENT'))
    filtered = Scenario('filter', keep=kept)
    filtered.cycle(arm=True)
    filtered.cycle(response(1), request(2, 0x8000))
    filtered.cycle(boundary('TRAP', 3, 0x40), pending(0, 1))
    filtered.cycle(pending(1, 0), user(5))
    rng = random.Random(0x46494C)
    for _ in range(600):
        filtered.cycle(*random_items(rng, 0.01))
    filtered.cycle(stop=True)
    result.append(filtered)

    rearm = Scenario('rearm')
    rearm.cycle(arm=True)
    for index in range(4):
        rearm.cycle(*six(index))
    rearm.cycle(stop=True)
    for _ in range(3000):
        rearm.cycle()
    rearm.cycle(clear=True)
    rearm.cycle(arm=True)
    for index in range(3):
        rearm.cycle(retire(0x4000 + 4 * index), user(100 + index))
    rearm.cycle(stop=True)
    result.append(rearm)

    rate = Scenario('max-record-rate', sizes=SIZES)
    rate.cycle(arm=True)
    for index in range(16):
        rate.cycle(retire(0x1000 + 4 * index), boundary('TRAP', index, 0, 0), ready=False)
    rate.cycle(stop=True, ready=False)
    result.append(rate)

    rng = random.Random(0x57524150)
    wrap = Scenario('wrap-trigger', sizes=SIZES, post_ticks=1200, stop='post_window',
                    post=lambda page_bytes: SRAM_BYTES // page_bytes - max(1, SRAM_BYTES // page_bytes // 8),
                    triggers=[dict(kinds=['USER_EVENT'], mode='equal', value=0x10309, mask=0xFFFFFFFF)])
    wrap.cycle(arm=True)
    for index in range(6300):
        busy = 0.015 if index > 5000 else 0.004
        wrap.cycle(*random_items(rng, busy) + ([user(0x10309)] if index == 5000 else []))
    result.append(wrap)

    software = Scenario('software-trigger', post_ticks=0, stop='post_window',
                        triggers=[dict(kinds=['BUS_REQ'], mode='range', base=0x9000, limit=0x9100)])
    software.cycle(arm=True)
    for index in range(10):
        software.cycle(retire(0x1000 + 4 * index), request(index, 0x8000 + index))
    software.cycle(response(99), request(99, 0x9010), user(7), soft=True)
    software.cycle(retire(0x2000))
    result.append(software)

    blind = Scenario('filtered-trigger', keep=kept, post_ticks=5, stop='post_window',
                     triggers=[None, dict(kinds=['USER_EVENT'], mode='equal', value=0x55, mask=0xFF)])
    blind.cycle(arm=True)
    for index in range(30):
        blind.cycle(retire(0x1000 + 4 * index), *([user(0x1255)] if index == 12 else []))
    result.append(blind)

    reset = Scenario('source-reset')
    reset.cycle(arm=True)
    for index in range(4):
        reset.cycle(*six(index), ready=False)
    reset.cycle(*six(4), reset=1, ready=False)
    for index in range(5, 8):
        reset.cycle(*six(index))
    reset.cycle(stop=True)
    result.append(reset)

    abort = Scenario('trace-reset')
    abort.cycle(arm=True)
    for index in range(5):
        abort.cycle(*six(index), ready=False)
    abort.cycle(*six(5), trace=True)
    abort.cycle(arm=True)
    for index in range(3):
        abort.cycle(retire(0x5000 + 4 * index), user(200 + index))
    abort.cycle(stop=True)
    result.append(abort)

    capacity = Scenario('capacity', sizes=SIZES, zero_drop=False, stop='capacity', post=smallest_safe,
                        triggers=[dict(kinds=['RETIRE'], mode='equal', value=0x1000 + 4 * 20, mask=0xFFFFFFFF)])
    capacity.cycle(arm=True)
    for index in range(600):
        capacity.cycle(*six(index))
    result.append(capacity)

    runs = Scenario('runs', sizes=SIZES, zero_drop=False)
    runs.cycle(arm=True)
    for items, ready in program(random.Random(0x52554E53), 60):
        runs.cycle(*items, ready=ready)
    runs.cycle(stop=True)
    result.append(runs)

    # Directed codec edges, drained apart and on one 4 KiB page so each context is deterministic.
    edges = Scenario('edges', sizes=(4096,), zero_drop=False)
    edges.cycle(arm=True)
    pc = 0x1000
    for stride, length in PHASES[:5]:
        for _ in range(length):
            edges.cycle(retire(pc))
            edges.cycle(repeat=stride - 1)
            pc += 4
    # A 65,536-tick gap exceeds both delta time fields.
    edges.cycle(repeat=40)
    edges.cycle(retire(0x5000, 0), request(1, 0x9000))
    edges.cycle(repeat=65535)
    edges.cycle(retire(0x5004, 0), request(2, 0x9004))
    # A run may not wrap the 32-bit PC.
    edges.cycle(repeat=40)
    for pc, target in ((0xFFFFFFF8, None), (0xFFFFFFFC, 0), (0, None)):
        edges.cycle(retire(pc, target))
    # PC and address changes of 32,767 and -32,768 fit a delta; one more does not.
    edges.cycle(repeat=40)
    pc, address = 0x100000, 0x200000
    for index, step in enumerate((0, 0x7FFF, 0x8000, -0x8000, -0x8001)):
        pc, address = pc + step, address + step
        edges.cycle(retire(pc, 0), request(10 + index, address))
    # A request at sequence 1 of a new epoch must not link to sequence 0 of the previous one.
    for items in ([request(3, 0x9008)], [response(3), request(4, 0x900C)]):
        edges.cycle(repeat=40)
        edges.cycle(*items, reset=1)
    edges.cycle(stop=True)
    result.append(edges)

    # Compression measurement: a 31-instruction loop retiring every other cycle.
    loop = Scenario('loop', sizes=SIZES, zero_drop=False)
    loop.cycle(arm=True)
    for index in range(20000):
        step = index % 31
        loop.cycle(retire(0x1000 + 4 * step, 0x1000 if step == 30 else None))
        loop.cycle()
    loop.cycle(stop=True)
    result.append(loop)

    # The P1f service guarantee: one event per envelope interval never drops (a retirement and a trap every two).
    for page_bytes in SIZES:
        spacing = math.ceil(2 * service_envelope(geometry(page_bytes, 1), 'raw-v1')['cycles_per_event'])
        steady = Scenario(f'sustained-{page_bytes}', sizes=(page_bytes,))
        steady.cycle(arm=True)
        for index in range(2000):
            steady.cycle(retire(0x1000 + 4 * index), boundary('TRAP', index, 0, 0))
            steady.cycle(repeat=spacing - 1)
        steady.cycle(stop=True)
        result.append(steady)
    return result


def slot_words(observation):
    raw = raw_encode.encode_record(Event(0, observation.source, 0, 0, 0, observation))
    return raw[0], raw[1], struct.unpack('<5I', raw[32:].ljust(20, b'\0'))


def cycle_line(ready, op='-', name='CHRONOS_ID', data=0):
    return f'c {int(ready)} {op} {ADDRESS[name]:x} {data:x}'


def observation_line(item):
    kind, flags, words = slot_words(item)
    return f'o {SLOTS[item.kind]} {kind} {flags} ' + ' '.join(f'{word:x}' for word in words)


def configuration(page_bytes, post, keep=KINDS, post_ticks=FOREVER, triggers=(), codec='raw-v1'):
    pages = SRAM_BYTES // page_bytes
    writes = [('CFG_SPLIT', pack('CFG_SPLIT', pre_pages=pages - post, post_pages=post)),
              ('CFG_MODE', pack('CFG_MODE', codec=codec, keep_kinds=sum(1 << KINDS.index(kind) for kind in keep))),
              ('CFG_POST_TICKS_LO', post_ticks & 0xFFFFFFFF), ('CFG_POST_TICKS_HI', post_ticks >> 32)]
    for index, slot in enumerate(triggers):
        if slot is None:
            continue
        writes.append((f'TRIG{index}_CTRL', pack(f'TRIG{index}_CTRL', enable=1, mode=slot['mode'],
                                                 kinds=sum(1 << KINDS.index(kind) for kind in slot['kinds']))))
        names = ('value', 'mask') if slot['mode'] == 'equal' else ('base', 'limit')
        writes += [(f'TRIG{index}_{name.upper()}', slot[name]) for name in names]
    return [cycle_line(1, 'w', name, value) for name, value in writes]


def script(scenario, page_bytes, codec):
    lines = [f'cfg {scenario.drain_ready} {scenario.seed}']
    lines += configuration(page_bytes, scenario.post(page_bytes), scenario.keep, scenario.post_ticks, scenario.triggers,
                           codec)
    arms = 0
    for cycle in scenario.cycles:
        commands = dict(stop=int(cycle['stop']), clear=int(cycle['clear']), software_trigger=int(cycle['soft']),
                        trace_reset=int(cycle['trace']), reset_source=int(cycle['reset'] is not None),
                        reset_source_id=cycle['reset'] or 0)
        if cycle['arm']:
            commands = dict(configure=int(arms == 0), arm=1)
            arms += 1
        lines.append(cycle_line(cycle['ready'], 'w', 'COMMAND', pack('COMMAND', **commands)) if any(commands.values())
                     else cycle_line(cycle['ready']))
        lines += [observation_line(item) for item in cycle['items']]
    lines.append(f"u 4 {ADDRESS['STATUS']:x}")
    reads = ['CHRONOS_ID', 'VERSION', 'CAPS', 'CAPS_SRAM_BYTES', 'STATUS', 'OUTCOME', 'SESSION_ID_LO', 'CONFIG_TAG_LO',
             'TRIG_TICK_LO', 'TRIG_MATCH']
    lines += [cycle_line(1, 'r', name) for name in reads]
    for source in range(4):
        for counter in COUNTERS:
            lines.append(cycle_line(1, 'w', 'ACCT_SELECT', pack('ACCT_SELECT', source=source, counter=counter)))
            lines += [cycle_line(1, 'r', 'ACCT_VALUE_LO'), cycle_line(1, 'r', 'ACCT_VALUE_HI')]
    lines += [cycle_line(1, 'w', 'READ_SESSION_LO', arms + 1), cycle_line(1, 'r', 'READ_STATUS'),
              cycle_line(1, 'r', 'READ_LENGTH'), cycle_line(1, 'w', 'READ_SESSION_LO', arms),
              cycle_line(1, 'r', 'READ_STATUS'), cycle_line(1, 'r', 'READ_LENGTH')]
    lines += [cycle_line(1, 'r', 'READ_DATA')] * (SRAM_BYTES // 4)
    return '\n'.join(lines) + '\n', arms


def expected(scenario):
    match = matcher(scenario.triggers, 4)
    armed, tick, table, bundles, trigger, pin, epochs, sequences = False, 0, None, None, None, None, None, None
    for cycle in scenario.cycles:
        if cycle['trace']:
            armed = False
            continue
        ends = False
        if armed and not cycle['stop']:
            if cycle['reset'] is not None:
                epochs[cycle['reset']] += 1
                sequences[cycle['reset']] = 0
            groups = normalize(cycle['items'])
            if trigger is None:
                hits = [(source, lane, reason) for source, group in enumerate(groups)
                        for lane, item in enumerate(group) for reason in [match(item)] if reason]
                if hits or cycle['soft']:
                    trigger, pin = dict(tick=tick, matches=hits, software=cycle['soft']), tick
            for source, group in enumerate(groups):
                events = [Event(tick, source, epochs[source], sequences[source] + lane, lane, item)
                          for lane, item in enumerate(group)]
                sequences[source] += len(group)
                table[source] += events
                if events:
                    bundles.append(events)
            ends = trigger is not None and tick == trigger['tick'] + scenario.post_ticks
        if armed:
            if cycle['stop'] and pin is None:
                pin = tick
            tick += 1
            armed = not cycle['stop'] and not ends
        if cycle['arm'] and not armed:
            armed, tick, table, bundles, trigger, pin = True, 0, [[] for _ in range(4)], [], None, None
            epochs, sequences = [0] * 4, [0] * 4
    return table, bundles, trigger, pin


def descriptor(trigger):
    if trigger is None:
        return 0, dict(lanes=0, primary=7, software=0, slots=0)
    pairs = [LANES.index((source, lane)) for source, lane, _ in trigger['matches']]
    slots = 0
    for _, _, reason in trigger['matches']:
        slots |= slot_mask(reason)
    return trigger['tick'], dict(lanes=sum(1 << index for index in set(pairs)), primary=pairs[0] if pairs else 7,
                                 software=int(trigger['software']), slots=slots)


def score(scenario, dump, page_bytes, arms, codec):
    name, (encoder, decoder) = f'{scenario.name}/{codec}', CODECS[codec]
    lines = [line.split() for line in dump.splitlines()]
    drain = int(lines[0][1])
    reads = iter(int(value) for tag, *rest in lines[1:] for value in rest[1:] if tag == 'r')
    take = lambda register: unpack(register, next(reads))
    identity = [next(reads), take('VERSION'), take('CAPS'), next(reads)]
    status, outcome, session, config_tag = take('STATUS'), take('OUTCOME'), next(reads), next(reads)
    latched = (next(reads), take('TRIG_MATCH'))
    caps = dict(sources=4, trigger_slots=4, fifo_depth_log2=4, page_bytes_log2=page_bytes.bit_length() - 1, codecs=3,
                max_record_bytes=128, sink_bytes=8)
    if identity != [0x4E524843, dict(map_minor=1, map_major=1), caps, SRAM_BYTES]:
        raise RuntimeError(f'{name}: identity registers {identity}')
    table, bundles, trigger, pin = expected(scenario)
    if (status['state'], status['stop_reason'], status['storage_error'], status['configured'],
            status['trigger_latched'], status['trigger_software']) != \
            ('FROZEN', scenario.stop, 'none', 1, int(trigger is not None), int(bool(trigger and trigger['software']))):
        raise RuntimeError(f'{name}: status {status}')
    if latched != descriptor(trigger):
        raise RuntimeError(f'{name}: trigger {latched}, expected {descriptor(trigger)}')
    if scenario.stop == 'manual' and {key: value for key, value in outcome.items() if value != 'none'} != dict(stop='accepted'):
        raise RuntimeError(f'{name}: outcome {outcome}')
    if (session, config_tag) != (arms, 1):
        raise RuntimeError(f'{name}: session {session}, config tag {config_tag}')
    acct = [{counter: next(reads) | next(reads) << 32 for counter in COUNTERS} for _ in range(4)]
    stale = [take('READ_STATUS'), next(reads)]
    valid, length = take('READ_STATUS'), next(reads)
    if stale != [dict(valid=0, stale=1), 0] or valid != dict(valid=1, stale=0) or length % page_bytes:
        raise RuntimeError(f'{name}: readout window {stale} {valid} {length}')
    image = b''.join(struct.pack('<I', value) for value in reads)[:length]
    pages = [image[offset:offset + page_bytes] for offset in range(0, length, page_bytes)]
    decoded = [decoder.decode_page(page, page_bytes=page_bytes) for page in pages]
    for page, entry in zip(pages, decoded):
        if encoder.encode_page(entry['events'], session_id=arms, generation=entry['generation'], config_tag=1,
                               page_bytes=page_bytes) != page:
            raise RuntimeError(f"{name}: page {entry['generation']} differs from the model encoding of its events")
    first = decoded[0]['generation'] if decoded else 0
    if any((page['generation'], page['session_id'], page['config_tag']) != (first + index, arms, 1)
           for index, page in enumerate(decoded)):
        raise RuntimeError(f'{name}: pages are not a consecutive generation suffix')
    events = [event for page in decoded for event in page['events']]
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=arms, config_tag=1,
                    page_bytes=page_bytes, source_profile='rv32-single-clock-v1', codecs=[codec],
                    config_sha256='0' * 64, pages=[dict(generation=page['generation'], record_count=len(page['events']),
                                                         payload_crc32=page['payload_crc32']) for page in decoded])
    if decode_fragment(encode_fragment(pages, manifest))['events'] != tuple(events):
        raise RuntimeError(f'{name}: production fragment decode differs')
    kept, present, oldest = set(scenario.keep), set(), {}
    positions = [{(event.epoch, event.sequence): index for index, event in enumerate(events_of)} for events_of in table]
    for source, counters in enumerate(acct):
        position = positions[source]
        retained = [event for event in events if event.source == source]
        observed = counters['observed']
        if observed > len(table[source]) or (scenario.stop != 'capacity' and observed != len(table[source])):
            raise RuntimeError(f'{name}: source {source} observed {observed} of {len(table[source])}')
        for event in retained:
            index = position.get((event.epoch, event.sequence), observed)
            if index >= observed or event != table[source][index] or event.observation.kind not in kept:
                raise RuntimeError(f'{name}: source {source} retained event differs from stimulus {event}')
            if trigger is not None and event.tick > trigger['tick'] + scenario.post_ticks:
                raise RuntimeError(f'{name}: event admitted after the post window {event}')
            present.add((source, index))
        order = [position[(event.epoch, event.sequence)] for event in retained]
        oldest[source] = order[0] if order else observed
        eligible = [index for index, event in enumerate(table[source][:observed]) if event.observation.kind in kept]
        before = [index for index in eligible if pin is not None and table[source][index].tick < pin]
        missing = [index for index in eligible if index not in set(order)]
        if order != sorted(set(order)) or counters['filtered'] != observed - len(eligible) or \
                observed != counters['filtered'] + counters['admitted'] + counters['ingress_dropped'] or \
                counters['ingress_dropped'] != counters['fifo_dropped'] + counters['capacity_dropped'] or \
                counters['admitted'] < len(retained) + counters['reset_discarded']:
            raise RuntimeError(f'{name}: source {source} accounting {counters} for {len(retained)} retained')
        rule = missing == eligible[:len(missing)] if not counters['reset_discarded'] else \
            len(missing) == counters['reset_discarded'] and table[source][missing[-1]].epoch < table[source][eligible[-1]].epoch
        if scenario.zero_drop and (counters['ingress_dropped'] or not rule or
                                   any(index in missing for index in eligible
                                       if pin is not None and table[source][index].tick >= pin) or
                                   (trigger is not None and before and before[-1] in missing)):
            raise RuntimeError(f'{name}: source {source} lost history that must be retained')
    evicted = sum(counters['admitted'] - counters['reset_discarded'] for counters in acct) - len(events)
    if (evicted == 0) != (first == 0):
        raise RuntimeError(f'{name}: {evicted} evicted events but first retained generation {first}')
    for group in bundles:
        source = group[0].source
        members = [positions[source][(event.epoch, event.sequence)] for event in group if event.observation.kind in kept]
        if members and members[0] >= oldest[source] and members[-1] < acct[source]['observed'] \
                and len({(source, index) in present for index in members}) != 1 and not acct[source]['reset_discarded']:
            raise RuntimeError(f'{name}: bundle admitted partially {members}')
    return dict(records=len(events), pages=len(pages), first_generation=first, evicted_events=evicted,
                dropped=sum(counters['ingress_dropped'] for counters in acct),
                capacity_dropped=sum(counters['capacity_dropped'] for counters in acct),
                drain_cycles=drain, lane1=sum(event.lane == 1 for event in events))


ORDER = ('trace_reset', 'clear', 'configure', 'arm', 'stop', 'reset_source', 'software_trigger')


def race_oracle(state, commands):
    outcomes, blocked, following = {}, False, state
    legal = dict(trace_reset=True, clear=state in ('DISABLED', 'FROZEN'), configure=state in ('DISABLED', 'FROZEN'),
                 arm=state == 'DISABLED', stop=state in ('ARMED', 'POST_TRIGGER'), software_trigger=state == 'ARMED',
                 reset_source=state in ('ARMED', 'POST_TRIGGER'))
    ignored = dict(stop='DRAINING', software_trigger='POST_TRIGGER')
    for name in ORDER:
        if name in commands:
            outcomes[name] = ('superseded' if blocked else 'ignored' if ignored.get(name) == state
                              else 'accepted' if legal.get(name) else 'rejected')
            if outcomes[name] == 'accepted' and name in ('trace_reset', 'clear', 'arm', 'stop', 'software_trigger'):
                blocked = blocked or name != 'software_trigger'
                following = dict(trace_reset='DISABLED', clear='DISABLED', arm='ARMED', stop='FROZEN',
                                 software_trigger='POST_TRIGGER')[name]
    return outcomes, following


def race_script():
    wait = f"u 4 {ADDRESS['STATUS']:x}"
    lines, cases = ['cfg 100 5'] + configuration(1024, 16), []
    command = lambda **bits: lines.append(cycle_line(0, 'w', 'COMMAND', pack('COMMAND', **bits)))
    command(configure=1)
    lines.append(cycle_line(0, 'w', 'CFG_MODE', pack('CFG_MODE', codec='compact-v1', keep_kinds=0x7F)))
    command(configure=1)
    lines += [cycle_line(0, 'r', 'OUTCOME'), cycle_line(0, 'r', 'STATUS'),
              cycle_line(0, 'w', 'CFG_MODE', pack('CFG_MODE', keep_kinds=0x7F))]
    cases.append(('DISABLED', ('configure', 'compact codec'), dict(configure='accepted'), 'DISABLED'))
    lines.append(cycle_line(0, 'w', 'CFG_SPLIT', pack('CFG_SPLIT', pre_pages=33 - smallest_safe(1024), post_pages=smallest_safe(1024) - 1)))
    command(configure=1)
    lines += [cycle_line(0, 'r', 'OUTCOME'), cycle_line(0, 'r', 'STATUS')] + configuration(1024, 16)[:1]
    cases.append(('DISABLED', ('configure', 'unsafe post pool'), dict(configure='rejected'), 'DISABLED'))
    for state in ('DISABLED', 'ARMED', 'POST_TRIGGER', 'DRAINING', 'FROZEN'):
        for size in range(len(ORDER) + 1):
            for chosen in combinations(ORDER, size):
                if state != 'DISABLED':
                    command(arm=1)
                if state == 'POST_TRIGGER':
                    command(software_trigger=1)
                if state == 'DRAINING':
                    lines += [cycle_line(0), observation_line(user(1))]
                if state in ('DRAINING', 'FROZEN'):
                    command(stop=1)
                if state == 'FROZEN':
                    lines.append(wait)
                command(**dict.fromkeys(chosen, 1))
                lines += [cycle_line(0)] * 4 + [cycle_line(0, 'r', 'OUTCOME'), cycle_line(0, 'r', 'STATUS')]
                outcomes, following = race_oracle(state, chosen)
                cases.append((state, chosen, outcomes, following))
                if following in ('ARMED', 'POST_TRIGGER'):
                    command(stop=1)
                if following != 'DISABLED':
                    lines.append(wait)
                    command(clear=1)
    return '\n'.join(lines) + '\n', cases


def score_races(dump, cases):
    values = [int(line.split()[2]) for line in dump.splitlines() if line.startswith('r ')]
    for index, (state, chosen, outcomes, following) in enumerate(cases):
        outcome = {key: value for key, value in unpack('OUTCOME', values[2 * index]).items() if value != 'none'}
        if outcome != outcomes or unpack('STATUS', values[2 * index + 1])['state'] != following:
            raise RuntimeError(f'command race {state} {chosen}: {outcome} {unpack("STATUS", values[2 * index + 1])}')
    return len(cases)


def synthesize(folder):
    yosys = tool_path('yosys')
    if not yosys:
        return dict(status='unavailable', required=False)
    script_path = folder / 'synth.ys'
    script_path.write_text('\n'.join([
        'read_slang ' + ' '.join(str(path.relative_to(ROOT)) for path in SOURCES) + ' --top chronos_capture -D SYNTHESIS',
        'hierarchy -check -top chronos_capture',
        'synth -flatten -top chronos_capture -run begin:fine',
        'select -assert-count 1 t:$mem_v2 r:SIZE=4096 %i r:WIDTH=64 %i',
        'select -assert-count 4 t:$mem_v2 r:SIZE=16 %i r:WIDTH=294 %i',
        'check -assert',
        'stat',
    ]) + '\n')
    run([yosys, '-m', 'slang', '-q', '-s', script_path, '-l', folder / 'synth.log'], folder / 'synth.out')
    if 'Warning' in (folder / 'synth.log').read_text():
        raise RuntimeError('synthesis warnings; see build/rtl/synth.log')
    return dict(status='passed', frontend='yosys-slang plugin from the installed OSS CAD Suite',
                scope='generic coarse synthesis; SRAM and four queues inferred as memories; no mapping, timing, or area claim')


def prove(folder):
    sby = tool_path('sby')
    if not sby:
        return dict(status='unavailable', required=False)
    run([sby, '-f', '-d', folder / 'formal-fifo', ROOT / 'tests/formal/trace_fifo.sby'], folder / 'formal-fifo.log')
    return dict(status='passed', scope='trace_fifo (W=8, DEPTH=4) k-induction: count bound, pointer consistency, FIFO order')


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/rtl.json'
    report = dict(status='failed', scope='Chronos RTL: capture, ring, triggers, reserve, resets, raw-v1 and compact-v1 '
                                          '(Verilator simulation)')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        verilator = tool_path('verilator')
        if not verilator:
            raise RuntimeError('verilator is required; configure its path, no download attempted')
        for page_bytes in SIZES:
            text = run([verilator, '--lint-only', '-Wall', '--top-module', 'chronos_capture', f'-GPAGE_BYTES={page_bytes}',
                        *SOURCES], BUILD / f'lint-{page_bytes}.log')
            if '%Warning' in text or '%Error' in text:
                raise RuntimeError(f'lint findings for {page_bytes}-byte pages')
            run([verilator, '--cc', '--exe', '--build', '--assert', '-Wall', '-O2', '--x-assign', 'unique',
                 '--x-initial', 'unique', '--top-module', 'chronos_capture', f'-GPAGE_BYTES={page_bytes}',
                 '--Mdir', BUILD / f'obj{page_bytes}', *SOURCES, ROOT / 'tests/rtl/tb_capture.cpp', '-o', 'tb'],
                BUILD / f'build-{page_bytes}.log')
        results = {}
        for scenario in scenarios():
            for codec in CODECS:
                for page_bytes in scenario.sizes:
                    stem = BUILD / f'{scenario.name}-{codec}-{page_bytes}'
                    text, arms = script(scenario, page_bytes, codec)
                    stem.with_suffix('.script').write_text(text)
                    run([BUILD / f'obj{page_bytes}/tb', stem.with_suffix('.script'), stem.with_suffix('.dump')],
                        stem.with_suffix('.log'))
                    results[f'{scenario.name}/{codec}/{page_bytes}'] = score(scenario, stem.with_suffix('.dump').read_text(),
                                                                             page_bytes, arms, codec)
        text, cases = race_script()
        (BUILD / 'races.script').write_text(text)
        run([BUILD / 'obj1024/tb', BUILD / 'races.script', BUILD / 'races.dump'], BUILD / 'races.log')
        report['command_races'] = score_races((BUILD / 'races.dump').read_text(), cases)
        density = {key.split('/', 1)[1]: dict(events=item['records'], pages=item['pages'])
                   for key, item in results.items() if key.startswith('loop/')}
        report.update(scenarios=results, loop_history=density, synthesis=synthesize(BUILD), formal=prove(BUILD))
        if before != fingerprints():
            raise RuntimeError('source changed during RTL checks')
        report.update(status='passed', source_sha256=before, pending=[
            'per-range loss journals and drain timeout (deferred)',
            'CPU qualification', 'physical board'])
        wraps = sum(item['evicted_events'] > 0 for item in results.values())
        print(f"PASS: RTL, {len(results)} register-driven scenario runs "
              f"({wraps} with eviction), {sum(item['records'] for item in results.values())} records decoded")
        print(f"{report['command_races']} same-cycle command cases match the priority oracle; "
              f"capacity stops at the smallest safe post pool without storage failure")
        print('Both codecs sustain the raw P1f envelope rate without drops (sustained-256/1024/4096)')
        print('Loop history retained: ' + ', '.join(f"{key} {item['events']} events/{item['pages']} pages"
                                                     for key, item in density.items()))
        print(f"Formal: {report['formal']['status']} ({report['formal'].get('scope', 'sby not installed')})")
        print('Receipt: build/rtl.json; scripts and dumps: build/rtl/')
        return 0
    except (OSError, ValueError, RuntimeError, StopIteration, IndexError) as error:
        report['error'] = str(error) or type(error).__name__
        print(f'FAIL: {report["error"]}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
