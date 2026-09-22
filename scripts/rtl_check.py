from dataclasses import dataclass, field
import json
import random
import struct
import subprocess
import sys

from model.chronos.events import Event, Observation, normalize
from model.chronos.predicates import KINDS
from model.chronos.raw_decode import decode_page
from model.chronos.raw_encode import encode_record
from model.chronos.raw_session import decode_fragment, encode_fragment
from scripts.config import ROOT
from scripts.doctor import tool_path
from scripts.model_check import fingerprints

BUILD = ROOT / 'build/rtl'
SOURCES = [ROOT / path for path in ('rtl/common/chronos_pkg.sv', 'rtl/common/trace_fifo.sv', 'rtl/common/trace_sram.sv',
                                    'rtl/capture/trace_ingress.sv', 'rtl/capture/trace_page_writer.sv',
                                    'rtl/capture/chronos_capture.sv')]
SRAM_BYTES = 32768
SLOTS = {'RETIRE': 0, 'BUS_RESP': 2, 'BUS_REQ': 3, 'TRAP': 4, 'IRQ_ACCEPT': 4, 'IRQ_PENDING': 5, 'USER_EVENT': 6}


@dataclass
class Scenario:
    name: str
    cycles: list = field(default_factory=list)
    keep: tuple = KINDS
    session: int = 0x5E55
    config_tag: int = 0xC0F1
    drain_ready: int = 100
    seed: int = 1
    zero_drop: bool = True
    storage_full: bool = False

    def cycle(self, *items, arm=False, stop=False, ready=True):
        self.cycles.append(dict(items=list(items), arm=arm, stop=stop, ready=ready))


def run(arguments, log):
    with open(log, 'w') as output:
        result = subprocess.run([str(item) for item in arguments], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}); see {log.relative_to(ROOT)}')
    return log.read_text()


def require(key):
    path = tool_path(key)
    if not path:
        raise RuntimeError(f'{key} is required; configure its path, no download attempted')
    return path


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
    rearm.cycle(arm=True)
    for index in range(3):
        rearm.cycle(retire(0x4000 + 4 * index), user(100 + index))
    rearm.cycle(stop=True)
    result.append(rearm)

    rate = Scenario('drain-rate')
    rate.cycle(arm=True)
    for index in range(16):
        rate.cycle(retire(0x1000 + 4 * index), request(index, 0x8000), boundary('TRAP', index, 0, 0), user(index),
                   ready=False)
    rate.cycle(stop=True, ready=False)
    result.append(rate)
    return result


def slot_words(observation):
    raw = encode_record(Event(0, observation.source, 0, 0, 0, observation))
    return raw[0], raw[1], struct.unpack('<5I', raw[32:].ljust(20, b'\0'))


def write_stimulus(scenario, path):
    keep = sum(1 << KINDS.index(kind) for kind in scenario.keep)
    lines = [f'cfg {keep:x} {scenario.session:x} {scenario.config_tag:x} {scenario.drain_ready} {scenario.seed}']
    for cycle in scenario.cycles:
        lines.append(f"c {int(cycle['arm'])} {int(cycle['stop'])} {int(cycle['ready'])}")
        for item in cycle['items']:
            kind, flags, words = slot_words(item)
            lines.append(f'o {SLOTS[item.kind]} {kind} {flags} ' + ' '.join(f'{word:x}' for word in words))
    path.write_text('\n'.join(lines) + '\n')


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


def parse(path):
    dump = dict(acct={}, directory={}, pages={})
    for line in path.read_text().splitlines():
        tag, *rest = line.split()
        if tag == 'result':
            keys = ('state', 'storage_full', 'cycles', 'stop_cycle', 'frozen_cycle', 'committed', 'ready_cycles')
            dump.update(zip(keys, map(int, rest)))
        elif tag == 'acct':
            dump['acct'][int(rest[0])] = dict(zip(('observed', 'filtered', 'admitted', 'dropped', 'pending'),
                                                  map(int, rest[1:])))
        elif tag == 'dir':
            dump['directory'][int(rest[0])] = (int(rest[1]), int(rest[2]))
        elif tag == 'page':
            dump['pages'][int(rest[0])] = bytes.fromhex(rest[1])
    return dump


def score(scenario, dump, page_bytes):
    name = scenario.name
    if dump['state'] != 4 or bool(dump['storage_full']) != scenario.storage_full:
        raise RuntimeError(f'{name}: final state {dump["state"]}, storage_full {dump["storage_full"]}')
    valid = [slot for slot, (flag, _) in sorted(dump['directory'].items()) if flag]
    if valid != list(range(len(valid))) or len(valid) != dump['committed'] or sorted(dump['pages']) != valid:
        raise RuntimeError(f'{name}: directory is not a committed linear prefix')
    pages, events = [], []
    for slot in valid:
        page = decode_page(dump['pages'][slot], page_bytes=page_bytes)
        if (page['generation'], dump['directory'][slot][1]) != (slot, slot) or \
                (page['session_id'], page['config_tag']) != (scenario.session, scenario.config_tag):
            raise RuntimeError(f'{name}: page {slot} identity or generation mismatch')
        pages.append(page)
        events.extend(page['events'])
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=scenario.session,
                    config_tag=scenario.config_tag, page_bytes=page_bytes, source_profile='rv32-single-clock-v1',
                    codecs=['raw-v1'], config_sha256='0' * 64,
                    pages=[dict(generation=page['generation'], record_count=len(page['events']),
                                payload_crc32=page['payload_crc32']) for page in pages])
    if decode_fragment(encode_fragment([dump['pages'][slot] for slot in valid], manifest))['events'] != tuple(events):
        raise RuntimeError(f'{name}: production fragment decode differs')
    table, bundles = expected(scenario)
    kept = set(scenario.keep)
    present = set()
    for source in range(4):
        counters = dump['acct'][source]
        decoded = [event for event in events if event.source == source]
        observed = counters['observed']
        if observed > len(table[source]) or (not scenario.storage_full and observed != len(table[source])):
            raise RuntimeError(f'{name}: source {source} observed {observed} of {len(table[source])}')
        for event in decoded:
            if event.sequence >= observed or event != table[source][event.sequence] or event.observation.kind not in kept:
                raise RuntimeError(f'{name}: source {source} decoded event differs from stimulus {event}')
            present.add((source, event.sequence))
        sequences = [event.sequence for event in decoded]
        if sequences != sorted(set(sequences)):
            raise RuntimeError(f'{name}: source {source} order or duplication')
        eligible = [event.sequence for event in table[source][:observed] if event.observation.kind in kept]
        if counters['filtered'] != observed - len(eligible) or \
                observed != counters['filtered'] + counters['admitted'] + counters['dropped'] or \
                counters['admitted'] != len(decoded) + counters['pending']:
            raise RuntimeError(f'{name}: source {source} accounting {counters} for {len(decoded)} decoded')
        if not scenario.storage_full and counters['pending']:
            raise RuntimeError(f'{name}: complete drain left pending work')
        if scenario.zero_drop and (counters['dropped'] or sequences != eligible):
            raise RuntimeError(f'{name}: source {source} lost eligible observations')
    last = {source: max((event.sequence for event in events if event.source == source), default=-1) for source in range(4)}
    for group in bundles:
        members = [event for event in group if event.observation.kind in kept]
        source = group[0].source
        if not members or members[-1].sequence >= dump['acct'][source]['observed'] or members[-1].sequence > last[source]:
            continue
        if len({(source, event.sequence) in present for event in members}) != 1:
            raise RuntimeError(f'{name}: bundle admitted partially {members}')
    return dict(records=len(events), pages=len(valid),
                dropped=sum(counters['dropped'] for counters in dump['acct'].values()),
                pending=sum(counters['pending'] for counters in dump['acct'].values()),
                cycles=dump['cycles'], drain_cycles=dump['frozen_cycle'] - dump['stop_cycle'],
                ready_fraction=round(dump['ready_cycles'] / dump['cycles'], 3),
                kinds=sorted({event.observation.kind for event in events}),
                lane1=sum(event.lane == 1 for event in events))


def synthesize(folder):
    yosys = tool_path('yosys')
    if not yosys:
        return dict(status='unavailable', required=False)
    script = folder / 'synth.ys'
    script.write_text('\n'.join([
        'read_slang ' + ' '.join(str(path.relative_to(ROOT)) for path in SOURCES) + ' --top chronos_capture -D SYNTHESIS',
        'hierarchy -check -top chronos_capture',
        'synth -flatten -top chronos_capture -run begin:fine',
        'select -assert-count 1 t:$mem_v2 r:SIZE=4096 %i r:WIDTH=64 %i',
        'select -assert-count 4 t:$mem_v2 r:SIZE=16 %i r:WIDTH=294 %i',
        'check -assert',
        'stat',
    ]) + '\n')
    run([yosys, '-m', 'slang', '-q', '-s', script, '-l', folder / 'synth.log'], folder / 'synth.out')
    if 'Warning' in (folder / 'synth.log').read_text():
        raise RuntimeError('synthesis warnings; see build/rtl/synth.log')
    return dict(status='passed', frontend='yosys-slang plugin from the installed OSS CAD Suite',
                scope='generic coarse synthesis; SRAM and four queues inferred as memories; '
                'no technology mapping, timing, or area claim')


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p2a.json'
    report = dict(status='failed', scope='P2a raw RTL capture vertical slice (Verilator simulation)')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        verilator = require('verilator')
        for page_bytes in (256, 1024, 4096):
            text = run([verilator, '--lint-only', '-Wall', '--top-module', 'chronos_capture', f'-GPAGE_BYTES={page_bytes}',
                        *SOURCES], BUILD / f'lint-{page_bytes}.log')
            if '%Warning' in text or '%Error' in text:
                raise RuntimeError(f'lint findings for {page_bytes}-byte pages')
        report['lint'] = 'verilator -Wall clean for 256/1024/4096-byte pages'
        binaries = {}
        for page_bytes in (256, 1024, 4096):
            folder = BUILD / f'obj{page_bytes}'
            run([verilator, '--cc', '--exe', '--build', '--assert', '-Wall', '-O2', '--x-assign', 'unique', '--x-initial', 'unique',
                 '--top-module', 'chronos_capture',
                 f'-GPAGE_BYTES={page_bytes}', f'-GSRAM_BYTES={SRAM_BYTES}', '-CFLAGS',
                 f'-DTB_PAGE_BYTES={page_bytes} -DTB_SRAM_BYTES={SRAM_BYTES}', '--Mdir', folder, *SOURCES,
                 ROOT / 'tests/rtl/tb_capture.cpp', '-o', 'tb'], BUILD / f'build-{page_bytes}.log')
            binaries[page_bytes] = folder / 'tb'
        results = {}
        wide = ('each-kind', 'random-stalls', 'overload', 'storage-full')
        for scenario in scenarios():
            for page_bytes in (256, 1024, 4096) if scenario.name in wide else (1024,):
                stem = BUILD / f'{scenario.name}-{page_bytes}'
                write_stimulus(scenario, stem.with_suffix('.stim'))
                run([binaries[page_bytes], stem.with_suffix('.stim'), stem.with_suffix('.dump')], stem.with_suffix('.log'))
                results[f'{scenario.name}/{page_bytes}'] = score(scenario, parse(stem.with_suffix('.dump')), page_bytes)
        rate = results['drain-rate/1024']
        report['scenarios'] = results
        report['throughput'] = dict(drain_cycles_per_record=round(rate['drain_cycles'] / rate['records'], 3),
                                    records=rate['records'], note='sink always ready; includes page seal and header writes')
        report['synthesis'] = synthesize(BUILD)
        if before != fingerprints():
            raise RuntimeError('source changed during RTL checks')
        report.update(status='passed', source_sha256=before, pending=[
            'register block implementing spec/registers.json and hardware readout image', 'source and trace reset',
            'small-block formal checks', 'circular ring, triggers, compression in RTL (P3)', 'CPU qualification',
            'physical board'])
        print(f"PASS: P2a RTL vertical slice, {len(results)} scenario runs, "
              f"{sum(item['records'] for item in results.values())} records decoded by the production decoder")
        print(f"Drain rate {report['throughput']['drain_cycles_per_record']} cycles/record; "
              f"synthesis {report['synthesis']['status']}")
        print('Receipt: build/p2a.json; logs and dumps: build/rtl/')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
