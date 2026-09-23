from dataclasses import dataclass, field
from itertools import combinations
import json
import math
import random
import struct
import subprocess
import sys

from model.chronos.capacity import service_envelope
from model.chronos.events import Event, Observation, normalize
from model.chronos.predicates import KINDS
from model.chronos.raw_decode import decode_page
from model.chronos.raw_encode import encode_record
from model.chronos.raw_session import decode_fragment, encode_fragment
from model.chronos.registers import MAP, pack, unpack
from scripts.config import ROOT, read_json
from scripts.doctor import tool_path
from scripts.model_check import fingerprints

BUILD = ROOT / 'build/rtl'
SOURCES = [ROOT / path for path in ('rtl/common/chronos_pkg.sv', 'rtl/common/trace_fifo.sv', 'rtl/common/trace_sram.sv',
                                    'rtl/capture/trace_ingress.sv', 'rtl/capture/trace_page_writer.sv',
                                    'rtl/capture/chronos_capture.sv')]
SRAM_BYTES = 32768
SLOTS = {'RETIRE': 0, 'BUS_RESP': 2, 'BUS_REQ': 3, 'TRAP': 4, 'IRQ_ACCEPT': 4, 'IRQ_PENDING': 5, 'USER_EVENT': 6}
COUNTERS = ('observed', 'filtered', 'admitted', 'ingress_dropped', 'fifo_dropped')
ADDRESS = {name: register['offset'] for name, register in MAP['registers'].items()}


@dataclass
class Scenario:
    name: str
    cycles: list = field(default_factory=list)
    keep: tuple = KINDS
    drain_ready: int = 100
    seed: int = 1
    zero_drop: bool = True
    storage_full: bool = False

    def cycle(self, *items, arm=False, stop=False, clear=False, ready=True):
        self.cycles.append(dict(items=list(items), arm=arm, stop=stop, clear=clear, ready=ready))


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
            items.append(user(value))
    return items


def scenarios():
    result = []
    each = Scenario('each-kind')
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
    stalls = Scenario('random-stalls', drain_ready=50, seed=7)
    stalls.cycle(arm=True)
    for _ in range(12000):
        stalls.cycle(*random_items(rng, 0.006), ready=rng.random() < 0.6)
    stalls.cycle(stop=True)
    result.append(stalls)

    overload = Scenario('overload', zero_drop=False)
    overload.cycle(arm=True)
    for index in range(200):
        overload.cycle(*six(index))
    overload.cycle(stop=True)
    result.append(overload)

    full = Scenario('storage-full', zero_drop=False, storage_full=True)
    full.cycle(arm=True)
    for index in range(8000):
        full.cycle(*[item for item, due in ((retire(0x1000 + 4 * index), index % 3 == 0),
                                            (request(index, 0x8000 + index), index % 5 == 0)) if due])
    result.append(full)

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

    rate = Scenario('max-record-rate')
    rate.cycle(arm=True)
    for index in range(16):
        rate.cycle(retire(0x1000 + 4 * index), boundary('TRAP', index, 0, 0), ready=False)
    rate.cycle(stop=True, ready=False)
    result.append(rate)
    return result


def slot_words(observation):
    raw = encode_record(Event(0, observation.source, 0, 0, 0, observation))
    return raw[0], raw[1], struct.unpack('<5I', raw[32:].ljust(20, b'\0'))


def cycle_line(ready, op='-', name='CHRONOS_ID', data=0):
    return f'c {int(ready)} {op} {ADDRESS[name]:x} {data:x}'


def script(scenario):
    keep = sum(1 << KINDS.index(kind) for kind in scenario.keep)
    lines = [f'cfg {scenario.drain_ready} {scenario.seed}', cycle_line(1, 'w', 'CFG_MODE', pack('CFG_MODE', keep_kinds=keep))]
    arms = 0
    for cycle in scenario.cycles:
        commands = {}
        if cycle['arm']:
            commands = dict(configure=int(arms == 0), arm=1)
            arms += 1
        elif cycle['stop'] or cycle['clear']:
            commands = dict(stop=int(cycle['stop']), clear=int(cycle['clear']))
        lines.append(cycle_line(cycle['ready'], 'w', 'COMMAND', pack('COMMAND', **commands)) if commands
                     else cycle_line(cycle['ready']))
        for item in cycle['items']:
            kind, flags, words = slot_words(item)
            lines.append(f'o {SLOTS[item.kind]} {kind} {flags} ' + ' '.join(f'{word:x}' for word in words))
    lines.append(f"u 4 {ADDRESS['STATUS']:x}")
    reads = ['CHRONOS_ID', 'VERSION', 'CAPS', 'CAPS_SRAM_BYTES', 'STATUS', 'OUTCOME', 'SESSION_ID_LO', 'CONFIG_TAG_LO']
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
    armed, tick, table, bundles = False, 0, None, None
    for cycle in scenario.cycles:
        if armed and not cycle['stop']:
            for source, bundle in enumerate(normalize(cycle['items'])):
                group = []
                for lane, item in enumerate(bundle):
                    event = Event(tick, source, 0, len(table[source]), lane, item)
                    table[source].append(event)
                    group.append(event)
                if group:
                    bundles.append(group)
        if armed:
            tick += 1
            armed = not cycle['stop']
        if cycle['arm'] and not armed:
            armed, tick, table, bundles = True, 0, [[] for _ in range(4)], []
    return table, bundles


def score(scenario, dump, page_bytes, arms):
    name = scenario.name
    lines = [line.split() for line in dump.splitlines()]
    drain = int(lines[0][1])
    reads = iter(int(value) for tag, *rest in lines[1:] for value in rest[1:] if tag == 'r')
    take = lambda register: unpack(register, next(reads))
    identity = [next(reads), take('VERSION'), take('CAPS'), next(reads)]
    status, outcome, session, config_tag = take('STATUS'), take('OUTCOME'), next(reads), next(reads)
    caps = dict(sources=4, trigger_slots=0, fifo_depth_log2=4, page_bytes_log2=page_bytes.bit_length() - 1, codecs=1,
                max_record_bytes=128, sink_bytes=8)
    if identity != [0x4E524843, dict(map_minor=1, map_major=1), caps, SRAM_BYTES]:
        raise RuntimeError(f'{name}: identity registers {identity}')
    stop = 'storage_failure' if scenario.storage_full else 'manual'
    storage = 'post_capacity' if scenario.storage_full else 'none'
    if (status['state'], status['stop_reason'], status['storage_error'], status['configured']) != ('FROZEN', stop, storage, 1):
        raise RuntimeError(f'{name}: status {status}')
    last = dict(configure='accepted', arm='accepted') if scenario.storage_full else dict(stop='accepted')
    if {key: value for key, value in outcome.items() if value != 'none'} != last or (session, config_tag) != (arms, 1):
        raise RuntimeError(f'{name}: outcome {outcome}, session {session}, config tag {config_tag}')
    acct = [{counter: next(reads) | next(reads) << 32 for counter in COUNTERS} for _ in range(4)]
    stale = [take('READ_STATUS'), next(reads)]
    valid, length = take('READ_STATUS'), next(reads)
    if stale != [dict(valid=0, stale=1), 0] or valid != dict(valid=1, stale=0) or length % page_bytes:
        raise RuntimeError(f'{name}: readout window {stale} {valid} {length}')
    image = b''.join(struct.pack('<I', value) for value in reads)[:length]
    pages = [image[offset:offset + page_bytes] for offset in range(0, length, page_bytes)]
    decoded = [decode_page(page, page_bytes=page_bytes) for page in pages]
    events = [event for page in decoded for event in page['events']]
    if any((page['generation'], page['session_id'], page['config_tag']) != (index, arms, 1)
           for index, page in enumerate(decoded)):
        raise RuntimeError(f'{name}: page identity or generation mismatch')
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=arms, config_tag=1,
                    page_bytes=page_bytes, source_profile='rv32-single-clock-v1', codecs=['raw-v1'],
                    config_sha256='0' * 64, pages=[dict(generation=page['generation'], record_count=len(page['events']),
                                                         payload_crc32=page['payload_crc32']) for page in decoded])
    if decode_fragment(encode_fragment(pages, manifest))['events'] != tuple(events):
        raise RuntimeError(f'{name}: production fragment decode differs')
    table, bundles = expected(scenario)
    kept, present, waiting = set(scenario.keep), set(), 0
    for source, counters in enumerate(acct):
        decoded_source = [event for event in events if event.source == source]
        observed = counters['observed']
        if observed > len(table[source]) or (not scenario.storage_full and observed != len(table[source])):
            raise RuntimeError(f'{name}: source {source} observed {observed} of {len(table[source])}')
        for event in decoded_source:
            if event.sequence >= observed or event != table[source][event.sequence] or event.observation.kind not in kept:
                raise RuntimeError(f'{name}: source {source} decoded event differs from stimulus {event}')
            present.add((source, event.sequence))
        sequences = [event.sequence for event in decoded_source]
        eligible = [event.sequence for event in table[source][:observed] if event.observation.kind in kept]
        waiting += counters['admitted'] - len(decoded_source)
        if sequences != sorted(set(sequences)) or counters['filtered'] != observed - len(eligible) or \
                counters['ingress_dropped'] != counters['fifo_dropped'] or \
                observed != counters['filtered'] + counters['admitted'] + counters['ingress_dropped'] or \
                not 0 <= counters['admitted'] - len(decoded_source) <= (16 if scenario.storage_full else 0):
            raise RuntimeError(f'{name}: source {source} accounting {counters} for {len(decoded_source)} decoded')
        if scenario.zero_drop and (counters['ingress_dropped'] or sequences != eligible):
            raise RuntimeError(f'{name}: source {source} lost eligible observations')
    newest = {source: max((event.sequence for event in events if event.source == source), default=-1) for source in range(4)}
    for group in bundles:
        members = [event for event in group if event.observation.kind in kept]
        source = group[0].source
        if members and members[-1].sequence < acct[source]['observed'] and members[-1].sequence <= newest[source] \
                and len({(source, event.sequence) in present for event in members}) != 1:
            raise RuntimeError(f'{name}: bundle admitted partially {members}')
    return dict(records=len(events), pages=len(pages), dropped=sum(counters['ingress_dropped'] for counters in acct),
                pending=waiting, drain_cycles=drain, kinds=len({event.observation.kind for event in events}),
                lane1=sum(event.lane == 1 for event in events))


ORDER = ('trace_reset', 'clear', 'configure', 'arm', 'stop', 'reset_source', 'software_trigger')


def race_oracle(state, commands):
    outcomes, blocked, following = {}, False, state
    legal = dict(clear=state in ('DISABLED', 'FROZEN'), configure=state in ('DISABLED', 'FROZEN'),
                 arm=state == 'DISABLED', stop=state == 'ARMED')
    for name in ORDER:
        if name in commands:
            outcomes[name] = ('superseded' if blocked else 'ignored' if name == 'stop' and state == 'DRAINING'
                              else 'accepted' if legal.get(name) else 'rejected')
            if outcomes[name] == 'accepted' and name in ('clear', 'arm', 'stop'):
                blocked = True
                following = dict(clear='DISABLED', arm='ARMED', stop='FROZEN')[name]
    return outcomes, following


def race_script():
    wait = f"u 4 {ADDRESS['STATUS']:x}"
    kind, flags, words = slot_words(user(1))
    lines, cases = ['cfg 100 5', cycle_line(0, 'w', 'COMMAND', pack('COMMAND', configure=1))], []
    command = lambda **bits: lines.append(cycle_line(0, 'w', 'COMMAND', pack('COMMAND', **bits)))
    lines.append(cycle_line(0, 'w', 'CFG_MODE', pack('CFG_MODE', codec='compact-v1', keep_kinds=0x7F)))
    command(configure=1)
    lines += [cycle_line(0, 'r', 'OUTCOME'), cycle_line(0, 'r', 'STATUS'),
              cycle_line(0, 'w', 'CFG_MODE', pack('CFG_MODE', keep_kinds=0x7F))]
    cases.append(('DISABLED', ('configure', 'compact codec'), dict(configure='rejected'), 'DISABLED'))
    for state in ('DISABLED', 'ARMED', 'DRAINING', 'FROZEN'):
        for size in range(len(ORDER) + 1):
            for chosen in combinations(ORDER, size):
                if state != 'DISABLED':
                    command(arm=1)
                if state == 'DRAINING':
                    lines += [cycle_line(0), f"o {SLOTS['USER_EVENT']} {kind} {flags} " + ' '.join(f'{w:x}' for w in words)]
                if state in ('DRAINING', 'FROZEN'):
                    command(stop=1)
                if state == 'FROZEN':
                    lines.append(wait)
                command(**dict.fromkeys(chosen, 1))
                lines += [cycle_line(0)] * 4 + [cycle_line(0, 'r', 'OUTCOME'), cycle_line(0, 'r', 'STATUS')]
                outcomes, following = race_oracle(state, chosen)
                cases.append((state, chosen, outcomes, following))
                if following == 'ARMED':
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
    receipt = ROOT / 'build/p2.json'
    report = dict(status='failed', scope='P2 raw RTL capture with register interface (Verilator simulation)')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        verilator = tool_path('verilator')
        if not verilator:
            raise RuntimeError('verilator is required; configure its path, no download attempted')
        for page_bytes in (256, 1024, 4096):
            text = run([verilator, '--lint-only', '-Wall', '--top-module', 'chronos_capture', f'-GPAGE_BYTES={page_bytes}',
                        *SOURCES], BUILD / f'lint-{page_bytes}.log')
            if '%Warning' in text or '%Error' in text:
                raise RuntimeError(f'lint findings for {page_bytes}-byte pages')
            run([verilator, '--cc', '--exe', '--build', '--assert', '-Wall', '-O2', '--x-assign', 'unique',
                 '--x-initial', 'unique', '--top-module', 'chronos_capture', f'-GPAGE_BYTES={page_bytes}',
                 '--Mdir', BUILD / f'obj{page_bytes}', *SOURCES, ROOT / 'tests/rtl/tb_capture.cpp', '-o', 'tb'],
                BUILD / f'build-{page_bytes}.log')
        results = {}
        wide = ('each-kind', 'random-stalls', 'overload', 'storage-full', 'max-record-rate')
        for scenario in scenarios():
            text, arms = script(scenario)
            for page_bytes in (256, 1024, 4096) if scenario.name in wide else (1024,):
                stem = BUILD / f'{scenario.name}-{page_bytes}'
                stem.with_suffix('.script').write_text(text)
                run([BUILD / f'obj{page_bytes}/tb', stem.with_suffix('.script'), stem.with_suffix('.dump')],
                    stem.with_suffix('.log'))
                results[f'{scenario.name}/{page_bytes}'] = score(scenario, stem.with_suffix('.dump').read_text(), page_bytes, arms)
        text, cases = race_script()
        (BUILD / 'races.script').write_text(text)
        run([BUILD / 'obj1024/tb', BUILD / 'races.script', BUILD / 'races.dump'], BUILD / 'races.log')
        report['command_races'] = score_races((BUILD / 'races.dump').read_text(), cases)
        config = read_json(ROOT / 'configs/baseline.json')
        throughput = {}
        for page_bytes in (256, 1024, 4096):
            rate = results[f'max-record-rate/{page_bytes}']
            envelope = service_envelope(dict(config, page_bytes=page_bytes,
                                             pre_pages=SRAM_BYTES // page_bytes - 1, post_pages=1), 'raw-v1')
            # Sustained full pages at the P1f rate, plus writing out the final partial page and a few control cycles.
            bound = math.ceil(rate['records'] * envelope['cycles_per_event']) + page_bytes // 8 + 4
            throughput[page_bytes] = dict(records=rate['records'], drain_cycles=rate['drain_cycles'], bound=bound,
                                          envelope_cycles_per_event=round(envelope['cycles_per_event'], 3))
            if rate['drain_cycles'] > bound:
                raise RuntimeError(f'{page_bytes}-byte pages drain {rate["drain_cycles"]} cycles, envelope bound {bound}')
        report.update(scenarios=results, throughput=throughput, synthesis=synthesize(BUILD))
        if before != fingerprints():
            raise RuntimeError('source changed during RTL checks')
        report.update(status='passed', source_sha256=before, pending=[
            'source and trace reset, circular ring, triggers, compression, and formal checks (P3)',
            'CPU qualification', 'physical board'])
        print(f"PASS: P2 RTL capture, {len(results)} register-driven scenario runs, "
              f"{sum(item['records'] for item in results.values())} records decoded by the production decoder")
        print(f"{report['command_races']} same-cycle command cases match the priority oracle")
        print('Max-record drain within the P1f envelope: ' + ', '.join(
            f"{size} B pages {item['drain_cycles']}/{item['bound']} cycles" for size, item in throughput.items()))
        print('Receipt: build/p2.json; scripts and dumps: build/rtl/')
        return 0
    except (OSError, ValueError, RuntimeError, StopIteration) as error:
        report['error'] = str(error) or type(error).__name__
        print(f'FAIL: {report["error"]}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
