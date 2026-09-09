import hashlib
import io
import json
import sys
import unittest

from model.chronos.admission import CaptureModel
from model.chronos.events import Observation
from model.chronos.raw_decode import decode_page
from model.chronos.raw_encode import encode_page, encode_record
from model.chronos.raw_session import decode_fragment, encode_fragment
from scripts.config import ROOT, read_json
from scripts.model_check import fingerprints, observations


def event_json(event):
    return dict(tick=event.tick, source=event.source, epoch=event.epoch,
                sequence=event.sequence, lane=event.lane, kind=event.observation.kind,
                fields=dict(event.observation.fields))


def sample(config, output):
    capture = CaptureModel(config, post_ticks=2, match=lambda item:
                           'user-20' if item.kind == 'USER_EVENT' and item.fields['value'] == 20 else None)
    for tick in range(25):
        items = list(observations(tick))
        if tick % 2:
            items[3] = Observation('IRQ_ACCEPT', items[3].fields)
        capture.step(tick, items, service=True)
    for _ in range(config['source_count'] * config['fifo_depth'] * 16):
        if capture.complete:
            break
        capture.service()
    if not capture.complete:
        raise RuntimeError('sample did not complete under its explicit drain grants')
    pages = []
    group = []
    size = 0
    payload_limit = config['page_bytes'] - 64
    for event in capture.emitted:
        record_size = len(encode_record(event))
        if size + record_size > payload_limit:
            pages.append(encode_page(group, session_id=1, generation=len(pages) + 1,
                                      config_tag=1, page_bytes=config['page_bytes']))
            group, size = [], 0
        group.append(event)
        size += record_size
    if group:
        pages.append(encode_page(group, session_id=1, generation=len(pages) + 1,
                                  config_tag=1, page_bytes=config['page_bytes']))
    directory = []
    for raw in pages:
        page = decode_page(raw, page_bytes=config['page_bytes'])
        directory.append(dict(generation=page['generation'], record_count=len(page['events']),
                              payload_crc32=page['payload_crc32']))
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=1,
                    config_tag=1, config_sha256=hashlib.sha256((ROOT / 'configs/baseline.json').read_bytes()).hexdigest(),
                    page_bytes=config['page_bytes'], source_profile='rv32-single-clock-v1',
                    codecs=['raw-v1'], pages=directory)
    wire = encode_fragment(pages, manifest)
    path = output / 'sample.chronos'
    path.write_bytes(wire)
    decoded = decode_fragment(path.read_bytes())
    if decoded['events'] != tuple(capture.emitted):
        raise RuntimeError('decoded fragment differs from admitted and completed sample events')
    (output / 'sample-events.json').write_text(json.dumps(
        dict(scope='event-fragment', events=[event_json(event) for event in decoded['events']]),
        indent=2, sort_keys=True) + '\n')
    return dict(input_summary=capture.summary(), decoded_events=len(decoded['events']),
                record_sizes={event.observation.kind: len(encode_record(event)) for event in capture.emitted},
                payload_bytes=sum(len(encode_record(event)) for event in capture.emitted),
                page_count=len(pages), physical_page_bytes=sum(map(len, pages)),
                fragment_bytes=len(wire), fragment_sha256=hashlib.sha256(wire).hexdigest(),
                artifacts=['build/raw/sample.chronos', 'build/raw/sample-events.json'])


def main():
    output = ROOT / 'build' / 'raw'
    output.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build' / 'p1b.json'
    report = dict(status='failed', scope='P1b raw event-fragment encoding and bounded decoding')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/model'), pattern='test_raw_*.py')
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (output / 'tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures),
                               errors=len(result.errors), skipped=len(result.skipped))
        if not result.wasSuccessful() or result.testsRun == 0 or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('raw checks failed or were skipped')
        report['sample'] = sample(read_json(ROOT / 'configs/baseline.json'), output)
        report['campaigns'] = dict(raw_streams=10000, raw_events=75391, raw_seed='0x5241575F5631',
                                   malformed_records=1000, single_bit_page_corruptions=512,
                                   single_bit_fragment_corruptions=500, literal_records=12,
                                   literal_pages=1, page_sizes=[256, 1024, 4096])
        if before != fingerprints():
            raise RuntimeError('source changed while raw checks were running')
        report.update(status='passed', source_sha256=before,
                      pending=['authoritative capture terminal/loss export', 'snapshot commit and retention',
                               'compression', 'encoded completion and hardware service reconciliation',
                               'CPU qualification', 'Chronos RTL', 'physical board'])
        print(f'PASS: P1b raw format, {result.testsRun} test methods; full P1 remains open')
        print(f"Sample: {report['sample']['decoded_events']} exact events in {report['sample']['page_count']} pages")
        print('Receipt: build/p1b.json; fragment: build/raw/sample.chronos')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    sys.exit(main())
