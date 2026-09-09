import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

from model.chronos.admission import CaptureModel
from model.chronos.capacity import completion_budget, default_inventory
from model.chronos.events import Observation
from scripts.config import read_json


ROOT = Path(__file__).resolve().parents[1]


def fingerprints():
    paths = [ROOT / '.gitignore', ROOT / 'Makefile']
    for name in ('configs', 'spec', 'model', 'scripts', 'tests', 'third_party'):
        paths.extend(path for path in (ROOT / name).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts
                     and path.suffix != '.pyc')
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def observations(tick):
    return (
        Observation('RETIRE', dict(pc=4096 + tick * 4, next_pc=4100 + tick * 4,
                                   length=4, boundary=tick)),
        Observation('BUS_RESP', dict(transaction=tick, data=tick, error=False)),
        Observation('BUS_REQ', dict(transaction=tick + 1, address=8192,
                                    write=True, data=tick, mask=15)),
        Observation('TRAP', dict(pc=None, cause=11, target=256, boundary=tick)),
        Observation('IRQ_PENDING', dict(previous=tick & 1, current=(tick + 1) & 1)),
        Observation('USER_EVENT', dict(value=tick)),
    )


def scenarios(config):
    results = {}
    for name in ('no_service', 'continuous_payload_grants', 'burst_payload_grants',
                 'trigger_with_full_queues'):
        trigger = (lambda item: 'user-20' if item.kind == 'USER_EVENT' and
                   item.fields['value'] == 20 else None) if name.startswith('trigger') else None
        capture = CaptureModel(config, post_ticks=2, match=trigger)
        grants = 0
        for tick in range(32):
            grant = name == 'continuous_payload_grants' or (
                name == 'burst_payload_grants' and tick % 8 >= 4)
            grants += grant
            capture.step(tick, observations(tick), service=grant)
        capture.stop()
        results[name] = dict(payload_grants=grants, summary=capture.summary())
    return results


def main():
    destination = ROOT / 'build' / 'p1a.json'
    destination.parent.mkdir(exist_ok=True)
    report = {'status': 'failed', 'scope': 'P1a synthetic admission and abstract completion reserve'}
    destination.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests' / 'model'))
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (ROOT / 'build' / 'p1a-tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures),
                               errors=len(result.errors), skipped=len(result.skipped))
        if not result.wasSuccessful() or result.testsRun == 0 or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('model checks failed or were skipped')
        config = read_json(ROOT / 'configs' / 'baseline.json')
        unsafe = dict(config, pre_pages=24, post_pages=8)
        report.update(
            config=config,
            assumptions={
                'max_events_per_tick_by_source': [1, 2, 2, 1],
                'fifo_entries_count_normalized_events': True,
                'bytes_per_event_upper_bound': config['max_record_bytes'],
                'bytes_per_payload_grant': config['sink_width_bits'] // 8,
                'completion_inventory': 'four full queues, four runs, one skid, one builder page, two controls',
                'extra_restart_bytes': 0,
                'tail_allowance_per_post_page': config['max_record_bytes'] - 1,
                'summary_storage': 'unbounded reference counters and diagnostic output',
                'service_scope': 'explicit record-payload grants; excludes header/commit scheduling',
            },
            budgets={
                'baseline': dict(config=config, result=completion_budget(config, default_inventory(config))),
                'unsafe_eight_post_pages': dict(config=unsafe, result=completion_budget(unsafe, default_inventory(unsafe))),
            },
            scenarios=scenarios(config),
            pending=['wire bytes and independent decoder', 'page ownership and restart',
                     'compression', 'bounded hardware loss metadata', 'encoded cost/service reconciliation',
                     'CPU qualification', 'Chronos RTL', 'physical board'],
        )
        if before != fingerprints():
            raise RuntimeError('source changed while model checks were running')
        report.update(status='passed', source_sha256=before)
        print(f"PASS: P1a model, {result.testsRun} test methods; full P1 remains open")
        print('Reserve model: 16 post pages margin 3216 bytes; 8 post pages deficit 3448 bytes')
        print('Receipt: build/p1a.json')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        destination.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    sys.exit(main())
