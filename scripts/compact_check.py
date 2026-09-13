import hashlib
import io
import json
import sys
import unittest

from model.chronos import compact_encode, raw_encode
from model.chronos.events import Event, Observation
from model.chronos.raw_session import decode_fragment, encode_fragment
from scripts.config import ROOT, read_json
from scripts.model_check import fingerprints
from scripts.raw_check import event_json


def workloads():
    streams = {name: [] for name in ('straight-line', 'irregular-timing', 'branch-heavy',
                                    'sequential-bus', 'request-response')}
    tick = 0
    for index in range(128):
        tick += 1 + index % 3
        for name, timestamp, pc, target in (
                ('straight-line', index, 4096 + 4 * index, 4100 + 4 * index),
                ('irregular-timing', tick, 4096 + 4 * index, 4100 + 4 * index),
                ('branch-heavy', index, 4096 + 4 * (index % 8), 4096 + 4 * ((index + 1) % 8))):
            streams[name].append(Event(timestamp, 0, 0, index, 0, Observation('RETIRE',
                dict(pc=pc, next_pc=target, length=4, boundary=0))))
        request = Observation('BUS_REQ', dict(transaction=index, address=8192 + 4 * index,
                                              data=index, write=True, mask=15))
        streams['sequential-bus'].append(Event(index, 1, 0, index, 0, request))
        streams['request-response'].append(Event(index, 1, 0, index, 0,
            request if index % 2 == 0 else Observation('BUS_RESP', dict(transaction=index - 1,
                                                                       data=index, error=False))))
    return streams


def pack(events, codec, page_bytes):
    encoder = compact_encode if codec == 'compact-v1' else raw_encode
    groups, group = [], []
    for event in events:
        candidate = group + [event]
        size = (sum(map(len, compact_encode.encode_records(candidate))) if codec == 'compact-v1'
                else sum(len(raw_encode.encode_record(item)) for item in candidate))
        if size > page_bytes - 64:
            if not group:
                raise RuntimeError('single event exceeds page payload')
            groups.append(group)
            group = []
        group.append(event)
    if group:
        groups.append(group)
    return [encoder.encode_page(group, session_id=1, generation=index, config_tag=1,
                                page_bytes=page_bytes) for index, group in enumerate(groups)]


def measure(events, codec, page_bytes, folder, name):
    pages = pack(events, codec, page_bytes)
    directory = [dict(generation=index, record_count=int.from_bytes(
        page[56:60] if codec == 'compact-v1' else page[36:40], 'little'),
        payload_crc32=int.from_bytes(page[48:52], 'little')) for index, page in enumerate(pages)]
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=1,
        config_tag=1, config_sha256=hashlib.sha256((ROOT / 'configs/baseline.json').read_bytes()).hexdigest(),
        page_bytes=page_bytes, source_profile='rv32-single-clock-v1', codecs=[codec], pages=directory)
    wire = encode_fragment(pages, manifest)
    path = folder / f'{name}-{codec}.chronos'
    path.write_bytes(wire)
    decoded = decode_fragment(path.read_bytes())
    if decoded['events'] != tuple(events):
        raise RuntimeError(f'{name} {codec} changed semantic events')
    (folder / f'{name}-{codec}.json').write_text(json.dumps(dict(scope='event-fragment',
        events=[event_json(event) for event in decoded['events']]), sort_keys=True, indent=2) + '\n')
    return dict(events=len(events), payload_bytes=sum(int.from_bytes(page[32:36], 'little') for page in pages),
        encoded_records=sum(int.from_bytes(page[36:40], 'little') for page in pages),
        page_count=len(pages), physical_page_bytes=sum(map(len, pages)), fragment_bytes=len(wire),
        fragment_sha256=hashlib.sha256(wire).hexdigest(), artifact=str(path.relative_to(ROOT)))


def main():
    folder = ROOT / 'build/compact'
    folder.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p1d.json'
    report = dict(status='failed', scope='P1d exact-time compact blocks and event fragments')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/model'), pattern='test_compact*.py')
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (folder / 'tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                               skipped=len(result.skipped))
        if not result.wasSuccessful() or not result.testsRun or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('compact checks failed or were skipped')
        page_bytes = read_json(ROOT / 'configs/baseline.json')['page_bytes']
        report['page_bytes'] = page_bytes
        report['packing'] = 'greedy prefix page cuts, codec state restarted per page; not an optimal packing claim'
        report['workloads'] = {name: {codec: measure(events, codec, page_bytes, folder, name)
            for codec in ('raw-v1', 'compact-v1')} for name, events in workloads().items()}
        report['campaigns'] = dict(pc_run=dict(seed='0x50435F52554E', streams=10000, events=212225),
                                   delta=dict(seed='0x44454C54415F', streams=10000, events=159953),
                                   literal_blocks=3, literal_pages=1)
        if before != fingerprints():
            raise RuntimeError('source changed during compact checks')
        report.update(status='passed', source_sha256=before, pending=[
            'streaming run watchdog and controller command races', 'compressed snapshot retention',
            'encoded completion and cycle-level SRAM service reconciliation', 'CPU qualification',
            'Chronos RTL', 'physical board'])
        print(f'PASS: P1d compact codecs, {result.testsRun} test methods; full P1 remains open')
        print('20,000 deterministic streams / 372,178 exact events; five framed size comparisons')
        print('Receipt: build/p1d.json; fragments: build/compact/')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
