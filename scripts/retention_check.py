import hashlib
import io
import json
import sys
import unittest

from model.chronos.capacity import completion_budget, measured_inventory, metadata_bytes, service_envelope
from model.chronos.capture_session import decode_capture
from model.chronos.events import Observation
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json
from scripts.model_check import fingerprints
from scripts.raw_check import event_json

CODECS = ('raw-v1', 'compact-v1')


def geometry(config, page_bytes, post_pages):
    pages = config['sram_bytes'] // page_bytes
    return dict(config, page_bytes=page_bytes, pre_pages=pages - post_pages, post_pages=post_pages)


def capacity(config):
    table = {}
    for page_bytes in (256, 1024, 4096):
        for codec in CODECS:
            pages = config['sram_bytes'] // page_bytes
            safe = [post for post in range(1, pages)
                    if completion_budget(geometry(config, page_bytes, post),
                                         measured_inventory(geometry(config, page_bytes, post), codec))['safe']]
            table[f'{page_bytes}/{codec}'] = dict(smallest_safe_post_pages=safe[0], total_pages=pages)
    baseline = {codec: completion_budget(config, measured_inventory(config, codec)) for codec in CODECS}
    return dict(smallest_safe=table, baseline=baseline)


def history(config, folder):
    results = {}
    for codec in CODECS:
        capture = SnapshotCapture(config, post_ticks=50, codec=codec, measured=True,
                                  match=lambda item: 'fault' if item.kind == 'USER_EVENT' else None)
        pc = 0x1000
        for tick in range(20000):
            target = 0x1000 if tick % 64 == 63 else pc + 4
            items = [Observation('RETIRE', dict(pc=pc, next_pc=target, length=4, boundary=0))]
            pc = target
            if tick % 25 == 0:
                items.append(Observation('BUS_REQ', dict(transaction=tick, address=0x8000 + 4 * tick,
                                                         write=True, data=tick, mask=15)))
            if tick == 19900:
                items.append(Observation('USER_EVENT', dict(value=1)))
            capture.step(tick, items)
            for _ in range(16):
                if not capture.model.complete:
                    capture.service()
        while not capture.model.complete:
            capture.service()
        wire = capture.freeze()
        path = folder / f'history-{codec}.chronos'
        path.write_bytes(wire)
        decoded = decode_capture(path.read_bytes())
        events = decoded['events']
        (folder / f'history-{codec}.json').write_text(json.dumps(dict(metadata=decoded['metadata'],
            events=[event_json(event) for event in events]), sort_keys=True, indent=2) + '\n')
        results[codec] = dict(retained_events=len(events), oldest_tick=events[0].tick, newest_tick=events[-1].tick,
                              evicted_events=decoded['metadata']['retention']['evicted_events'],
                              pages=len(decoded['fragment']['pages']), bytes=len(wire),
                              sha256=hashlib.sha256(wire).hexdigest(), artifact=str(path.relative_to(ROOT)))
    raw, compact = results['raw-v1'], results['compact-v1']
    if compact['retained_events'] <= 5 * raw['retained_events'] or compact['oldest_tick'] >= raw['oldest_tick']:
        raise RuntimeError('compressed retention did not extend history as expected')
    return results


def main():
    folder = ROOT / 'build/retention'
    folder.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p1f.json'
    report = dict(status='failed', scope='P1f compressed retention and measured capacity')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/model'), pattern='test_compressed_retention.py')
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (folder / 'tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                               skipped=len(result.skipped))
        if not result.wasSuccessful() or not result.testsRun or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('retention checks failed or were skipped')
        config = read_json(ROOT / 'configs/baseline.json')
        report['capacity'] = capacity(config)
        report['envelope'] = {codec: service_envelope(config, codec) for codec in CODECS}
        report['metadata_storage'] = metadata_bytes(config)
        report['history'] = history(config, folder)
        if before != fingerprints():
            raise RuntimeError('source changed during retention checks')
        report.update(status='passed', source_sha256=before, pending=[
            'register bit layout and trigger-slot model', 'cycle-level page commit and SRAM port timing (P2)',
            'CPU qualification', 'Chronos RTL', 'physical board'])
        raw, compact = report['history']['raw-v1'], report['history']['compact-v1']
        print(f'PASS: P1f compressed retention and measured capacity, {result.testsRun} test methods')
        print(f"Same 32 KiB memory: raw keeps {raw['retained_events']} events, compact keeps {compact['retained_events']}")
        print('Receipt: build/p1f.json; captures: build/retention/')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
