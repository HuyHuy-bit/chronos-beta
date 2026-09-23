import io
import hashlib
import json
import sys
import unittest

from model.chronos.capture_session import decode_capture
from model.chronos.events import Observation
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json
from scripts.model_check import fingerprints, observations
from scripts.raw_check import event_json


def save(capture, name, folder):
    wire = capture.freeze()
    path = folder / f'{name}.chronos'
    path.write_bytes(wire)
    decoded = decode_capture(path.read_bytes())
    if capture.freeze() != wire:
        raise RuntimeError('frozen capture changed during readout')
    (folder / f'{name}.json').write_text(json.dumps(dict(metadata=decoded['metadata'],
        events=[event_json(event) for event in decoded['events']]), sort_keys=True, indent=2) + '\n')
    return dict(metadata=decoded['metadata'], retained_events=len(decoded['events']),
                pages=len(decoded['fragment']['pages']), bytes=len(wire), sha256=hashlib.sha256(wire).hexdigest(),
                artifact=str(path.relative_to(ROOT)))


def examples(config, folder):
    wrapped = SnapshotCapture(dict(config, pre_pages=2, post_pages=30), post_ticks=25,
                               match=lambda item: 'user-99' if item.kind == 'USER_EVENT' else None)
    for tick in range(111):
        items = [Observation('BUS_REQ', dict(transaction=tick, address=8192 + 4 * tick,
                                              data=tick, write=True, mask=15))]
        if tick == 85:
            items.append(Observation('USER_EVENT', dict(value=99)))
        wrapped.step(tick, items)
        for _ in range(32):
            wrapped.service()
    retained = save(wrapped, 'retained', folder)
    if retained['retained_events'] != 52 or retained['metadata']['retention']['evicted_events'] != 60:
        raise RuntimeError('retention example differs from the literal 112-event ledger')
    overflow = SnapshotCapture(config, post_ticks=2, journal_capacity=1,
                               keep=lambda item: not (item.kind == 'USER_EVENT' and item.fields['value'] == 20),
                               match=lambda item: 'user-20' if item.kind == 'USER_EVENT' and item.fields['value'] == 20 else None)
    for tick in range(23):
        overflow.step(tick, observations(tick))
    for _ in range(4 * config['fifo_depth'] * 16):
        overflow.service()
    saturated = save(overflow, 'overflow', folder)
    rows = saturated['metadata']['capture']['sources']
    totals = {key: sum(row['counters'][key] for row in rows) for key in ('observed', 'filtered', 'admitted', 'ingress_dropped')}
    if totals != dict(observed=138, filtered=1, admitted=64, ingress_dropped=73) or not rows[3]['journal_overflow']:
        raise RuntimeError('overflow example differs from the literal observation ledger')
    return dict(retained=retained, overflow=saturated)


def main():
    folder = ROOT / 'build/snapshot'
    folder.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p1c.json'
    report = dict(status='failed', scope='P1c raw retention and authoritative model snapshot')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT / 'tests/model'), pattern=pattern)
                                  for pattern in ('test_retention.py', 'test_capture_metadata.py',
                                                  'test_snapshot.py', 'test_capture_session.py'))
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (folder / 'tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))
        if not result.wasSuccessful() or not result.testsRun or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('snapshot checks failed or were skipped')
        report['examples'] = examples(read_json(ROOT / 'configs/baseline.json'), folder)
        if before != fingerprints():
            raise RuntimeError('source changed during snapshot checks')
        report.update(status='passed', source_sha256=before,
                      pending=['compression', 'encoded completion and cycle-level SRAM service reconciliation',
                               'software-trigger and complete register command races', 'CPU qualification',
                               'Chronos RTL', 'physical board'])
        print(f'PASS: P1c snapshots, {result.testsRun} test methods')
        print('Retention: 52 saved / 60 evicted events; overload: 64 saved / 73 dropped / 1 filtered')
        print('Receipt: build/p1c.json; captures: build/snapshot/')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
