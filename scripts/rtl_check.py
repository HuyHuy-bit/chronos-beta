from dataclasses import dataclass, field
from itertools import combinations
import json
import math
import random
import struct
import subprocess
import sys

from model.chronos.capacity import completion_budget, measured_inventory, service_envelope
from model.chronos.events import Event, Observation, normalize
from model.chronos.predicates import KINDS, matcher, slot_mask
from model.chronos.raw_decode import decode_page
from model.chronos.raw_encode import encode_record
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
COUNTERS = ('observed', 'filtered', 'admitted', 'ingress_dropped', 'fifo_dropped', 'capacity_dropped')
ADDRESS = {name: register['offset'] for name, register in MAP['registers'].items()}
CONFIG = read_json(ROOT / 'configs/baseline.json')
FOREVER = (1 << 64) - 1


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

    def cycle(self, *items, arm=False, stop=False, clear=False, soft=False, ready=True):
        self.cycles.append(dict(items=list(items), arm=arm, stop=stop, clear=clear, soft=soft, ready=ready))


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

    capacity = Scenario('capacity', sizes=SIZES, zero_drop=False, stop='capacity', post=smallest_safe,
                        triggers=[dict(kinds=['RETIRE'], mode='equal', value=0x1000 + 4 * 20, mask=0xFFFFFFFF)])
    capacity.cycle(arm=True)
    for index in range(600):
        capacity.cycle(*six(index))
    result.append(capacity)
    return result


def slot_words(observation):
    raw = encode_record(Event(0, observation.source, 0, 0, 0, observation))
    return raw[0], raw[1], struct.unpack('<5I', raw[32:].ljust(20, b'\0'))


def cycle_line(ready, op='-', name='CHRONOS_ID', data=0):
    return f'c {int(ready)} {op} {ADDRESS[name]:x} {data:x}'


def observation_line(item):
    kind, flags, words = slot_words(item)
    return f'o {SLOTS[item.kind]} {kind} {flags} ' + ' '.join(f'{word:x}' for word in words)


def configuration(page_bytes, post, keep=KINDS, post_ticks=FOREVER, triggers=()):
    pages = SRAM_BYTES // page_bytes
    writes = [('CFG_SPLIT', pack('CFG_SPLIT', pre_pages=pages - post, post_pages=post)),
              ('CFG_MODE', pack('CFG_MODE', keep_kinds=sum(1 << KINDS.index(kind) for kind in keep))),
              ('CFG_POST_TICKS_LO', post_ticks & 0xFFFFFFFF), ('CFG_POST_TICKS_HI', post_ticks >> 32)]
    for index, slot in enumerate(triggers):
        if slot is None:
            continue
        writes.append((f'TRIG{index}_CTRL', pack(f'TRIG{index}_CTRL', enable=1, mode=slot['mode'],
                                                 kinds=sum(1 << KINDS.index(kind) for kind in slot['kinds']))))
        names = ('value', 'mask') if slot['mode'] == 'equal' else ('base', 'limit')
        writes += [(f'TRIG{index}_{name.upper()}', slot[name]) for name in names]
    return [cycle_line(1, 'w', name, value) for name, value in writes]


def script(scenario, page_bytes):
    lines = [f'cfg {scenario.drain_ready} {scenario.seed}']
    lines += configuration(page_bytes, scenario.post(page_bytes), scenario.keep, scenario.post_ticks, scenario.triggers)
    arms = 0
    for cycle in scenario.cycles:
        commands = dict(stop=int(cycle['stop']), clear=int(cycle['clear']), software_trigger=int(cycle['soft']))
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
    armed, tick, table, bundles, trigger, pin = False, 0, None, None, None, None
    for cycle in scenario.cycles:
        ends = False
        if armed and not cycle['stop']:
            groups = normalize(cycle['items'])
            if trigger is None:
                hits = [(source, lane, reason) for source, group in enumerate(groups)
                        for lane, item in enumerate(group) for reason in [match(item)] if reason]
                if hits or cycle['soft']:
                    trigger, pin = dict(tick=tick, matches=hits, software=cycle['soft']), tick
            for source, group in enumerate(groups):
                events = [Event(tick, source, 0, len(table[source]) + lane, lane, item) for lane, item in enumerate(group)]
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


def score(scenario, dump, page_bytes, arms):
    name = scenario.name
    lines = [line.split() for line in dump.splitlines()]
    drain = int(lines[0][1])
    reads = iter(int(value) for tag, *rest in lines[1:] for value in rest[1:] if tag == 'r')
    take = lambda register: unpack(register, next(reads))
    identity = [next(reads), take('VERSION'), take('CAPS'), next(reads)]
    status, outcome, session, config_tag = take('STATUS'), take('OUTCOME'), next(reads), next(reads)
    latched = (next(reads), take('TRIG_MATCH'))
    caps = dict(sources=4, trigger_slots=4, fifo_depth_log2=4, page_bytes_log2=page_bytes.bit_length() - 1, codecs=1,
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
    decoded = [decode_page(page, page_bytes=page_bytes) for page in pages]
    first = decoded[0]['generation'] if decoded else 0
    if any((page['generation'], page['session_id'], page['config_tag']) != (first + index, arms, 1)
           for index, page in enumerate(decoded)):
        raise RuntimeError(f'{name}: pages are not a consecutive generation suffix')
    events = [event for page in decoded for event in page['events']]
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=arms, config_tag=1,
                    page_bytes=page_bytes, source_profile='rv32-single-clock-v1', codecs=['raw-v1'],
                    config_sha256='0' * 64, pages=[dict(generation=page['generation'], record_count=len(page['events']),
                                                         payload_crc32=page['payload_crc32']) for page in decoded])
    if decode_fragment(encode_fragment(pages, manifest))['events'] != tuple(events):
        raise RuntimeError(f'{name}: production fragment decode differs')
    kept, present, oldest = set(scenario.keep), set(), {}
    for source, counters in enumerate(acct):
        retained = [event for event in events if event.source == source]
        observed = counters['observed']
        if observed > len(table[source]) or (scenario.stop != 'capacity' and observed != len(table[source])):
            raise RuntimeError(f'{name}: source {source} observed {observed} of {len(table[source])}')
        for event in retained:
            if event.sequence >= observed or event != table[source][event.sequence] or event.observation.kind not in kept:
                raise RuntimeError(f'{name}: source {source} retained event differs from stimulus {event}')
            if trigger is not None and event.tick > trigger['tick'] + scenario.post_ticks:
                raise RuntimeError(f'{name}: event admitted after the post window {event}')
            present.add((source, event.sequence))
        sequences = [event.sequence for event in retained]
        oldest[source] = sequences[0] if sequences else observed
        eligible = [event for event in table[source][:observed] if event.observation.kind in kept]
        before = [event.sequence for event in eligible if pin is not None and event.tick < pin]
        if sequences != sorted(set(sequences)) or counters['filtered'] != observed - len(eligible) or \
                observed != counters['filtered'] + counters['admitted'] + counters['ingress_dropped'] or \
                counters['ingress_dropped'] != counters['fifo_dropped'] + counters['capacity_dropped'] or \
                counters['admitted'] < len(retained):
            raise RuntimeError(f'{name}: source {source} accounting {counters} for {len(retained)} retained')
        if scenario.zero_drop and (counters['ingress_dropped'] or
                                   sequences != [event.sequence for event in eligible][len(eligible) - len(sequences):] or
                                   any(event.sequence not in sequences for event in eligible
                                       if pin is not None and event.tick >= pin) or
                                   (trigger is not None and before and before[-1] not in sequences)):
            raise RuntimeError(f'{name}: source {source} lost history that must be retained')
    evicted = sum(counters['admitted'] for counters in acct) - len(events)
    if (evicted == 0) != (first == 0):
        raise RuntimeError(f'{name}: {evicted} evicted events but first retained generation {first}')
    for group in bundles:
        members = [event for event in group if event.observation.kind in kept]
        source = group[0].source
        if members and members[0].sequence >= oldest[source] and members[-1].sequence < acct[source]['observed'] \
                and len({(source, event.sequence) in present for event in members}) != 1:
            raise RuntimeError(f'{name}: bundle admitted partially {members}')
    return dict(records=len(events), pages=len(pages), first_generation=first, evicted_events=evicted,
                dropped=sum(counters['ingress_dropped'] for counters in acct),
                capacity_dropped=sum(counters['capacity_dropped'] for counters in acct),
                drain_cycles=drain, lane1=sum(event.lane == 1 for event in events))


ORDER = ('trace_reset', 'clear', 'configure', 'arm', 'stop', 'reset_source', 'software_trigger')


def race_oracle(state, commands):
    outcomes, blocked, following = {}, False, state
    legal = dict(clear=state in ('DISABLED', 'FROZEN'), configure=state in ('DISABLED', 'FROZEN'),
                 arm=state == 'DISABLED', stop=state in ('ARMED', 'POST_TRIGGER'), software_trigger=state == 'ARMED')
    ignored = dict(stop='DRAINING', software_trigger='POST_TRIGGER')
    for name in ORDER:
        if name in commands:
            outcomes[name] = ('superseded' if blocked else 'ignored' if ignored.get(name) == state
                              else 'accepted' if legal.get(name) else 'rejected')
            if outcomes[name] == 'accepted' and name in ('clear', 'arm', 'stop', 'software_trigger'):
                blocked = blocked or name != 'software_trigger'
                following = dict(clear='DISABLED', arm='ARMED', stop='FROZEN', software_trigger='POST_TRIGGER')[name]
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
    cases.append(('DISABLED', ('configure', 'compact codec'), dict(configure='rejected'), 'DISABLED'))
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


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p3a.json'
    report = dict(status='failed', scope='P3a ring, triggers, and post reserve in RTL (Verilator simulation)')
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
            for page_bytes in scenario.sizes:
                stem = BUILD / f'{scenario.name}-{page_bytes}'
                text, arms = script(scenario, page_bytes)
                stem.with_suffix('.script').write_text(text)
                run([BUILD / f'obj{page_bytes}/tb', stem.with_suffix('.script'), stem.with_suffix('.dump')],
                    stem.with_suffix('.log'))
                results[f'{scenario.name}/{page_bytes}'] = score(scenario, stem.with_suffix('.dump').read_text(), page_bytes, arms)
        text, cases = race_script()
        (BUILD / 'races.script').write_text(text)
        run([BUILD / 'obj1024/tb', BUILD / 'races.script', BUILD / 'races.dump'], BUILD / 'races.log')
        report['command_races'] = score_races((BUILD / 'races.dump').read_text(), cases)
        throughput = {}
        for page_bytes in SIZES:
            rate = results[f'max-record-rate/{page_bytes}']
            envelope = service_envelope(geometry(page_bytes, 1), 'raw-v1')
            # Sustained full pages at the P1f rate, plus writing out the final partial page and a few control cycles.
            bound = math.ceil(rate['records'] * envelope['cycles_per_event']) + page_bytes // 8 + 4
            throughput[page_bytes] = dict(records=rate['records'], drain_cycles=rate['drain_cycles'], bound=bound)
            if rate['drain_cycles'] > bound:
                raise RuntimeError(f'{page_bytes}-byte pages drain {rate["drain_cycles"]} cycles, envelope bound {bound}')
        report.update(scenarios=results, throughput=throughput, synthesis=synthesize(BUILD))
        if before != fingerprints():
            raise RuntimeError('source changed during RTL checks')
        report.update(status='passed', source_sha256=before, pending=[
            'loss journals, source and trace reset, and small-block formal checks (P3b)', 'compression in RTL (P3c)',
            'CPU qualification', 'physical board'])
        wraps = sum(item['evicted_events'] > 0 for item in results.values())
        print(f"PASS: P3a RTL ring and triggers, {len(results)} register-driven scenario runs "
              f"({wraps} with eviction), {sum(item['records'] for item in results.values())} records decoded")
        print(f"{report['command_races']} same-cycle command cases match the priority oracle; "
              f"capacity stops at the smallest safe post pool without storage failure")
        print('Max-record drain within the P1f envelope: ' + ', '.join(
            f"{size} B pages {item['drain_cycles']}/{item['bound']} cycles" for size, item in throughput.items()))
        print('Receipt: build/p3a.json; scripts and dumps: build/rtl/')
        return 0
    except (OSError, ValueError, RuntimeError, StopIteration, IndexError) as error:
        report['error'] = str(error) or type(error).__name__
        print(f'FAIL: {report["error"]}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
