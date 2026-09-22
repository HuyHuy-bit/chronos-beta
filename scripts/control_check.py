import hashlib
import io
import json
import sys
import unittest

from model.chronos.capture_session import decode_capture
from model.chronos.controller import CaptureController
from model.chronos.events import Observation
from scripts.config import ROOT, read_json
from scripts.model_check import fingerprints
from scripts.raw_check import event_json


def save(controller, name, folder):
    session, wire = controller.read()
    if controller.read() != (session, wire):
        raise RuntimeError('frozen readout changed between reads')
    path = folder / f'{name}.chronos'
    path.write_bytes(wire)
    decoded = decode_capture(path.read_bytes())
    (folder / f'{name}.json').write_text(json.dumps(dict(metadata=decoded['metadata'],
        events=[event_json(event) for event in decoded['events']]), sort_keys=True, indent=2) + '\n')
    return dict(session_id=session, terminal=decoded['metadata']['terminal'],
                trigger=decoded['metadata']['capture']['trigger'], retained_events=len(decoded['events']),
                bytes=len(wire), sha256=hashlib.sha256(wire).hexdigest(), artifact=str(path.relative_to(ROOT)))


def examples(config, folder):
    controller = CaptureController()
    tick = 0

    def cycle(*items, **commands):
        nonlocal tick
        tick += 1
        return controller.cycle(tick, items, **commands)

    def request(value):
        return Observation('BUS_REQ', dict(transaction=value, address=0x8000 + 4 * value, write=True,
                                           data=value, mask=15))

    cycle(configure=dict(config=config, post_ticks=2), arm=True)
    for value in range(10):
        cycle(request(value), software_trigger=value == 6, service=True)
    while controller.state == 'DRAINING':
        cycle(service=True)
    triggered = save(controller, 'software-trigger', folder)
    if (triggered['trigger'] != dict(tick=8, matches=[], primary=None, software=True)
            or triggered['retained_events'] != 9 or triggered['terminal']['reason'] != 'post_window'):
        raise RuntimeError('software-trigger example differs from its literal ledger')
    cycle(clear=True)
    while controller.state == 'CLEARING':
        cycle()
    cycle(configure=dict(config=config, post_ticks=0, drain_limit=24), arm=True)
    cycle(request(1), Observation('USER_EVENT', dict(value=1)))
    cycle(stop=True)
    while controller.state == 'DRAINING':
        cycle(service=True)
    timeout = save(controller, 'drain-timeout', folder)
    if (not controller.status()['drain_timeout'] or timeout['terminal']['pending_events'] != [0, 0, 0, 1]
            or timeout['terminal']['drain_complete'] or timeout['retained_events'] != 1 or timeout['session_id'] != 2):
        raise RuntimeError('drain-timeout example differs from its literal ledger')
    return dict(software_trigger=triggered, drain_timeout=timeout)


def main():
    folder = ROOT / 'build/control'
    folder.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p1e.json'
    report = dict(status='failed', scope='P1e capture controller and streaming run flush')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT / 'tests/model'), pattern=pattern)
                                   for pattern in ('test_controller.py', 'test_run_stream.py'))
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (folder / 'tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                               skipped=len(result.skipped))
        if not result.wasSuccessful() or not result.testsRun or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('control checks failed or were skipped')
        report['command_race_cases'] = 6 * 128
        report['campaigns'] = dict(watermark=dict(seed='0x53545245414D', streams=2000, events=79563),
                                   flush=dict(seed='0x464C555348', streams=2000))
        report['examples'] = examples(read_json(ROOT / 'configs/baseline.json'), folder)
        if before != fingerprints():
            raise RuntimeError('source changed during control checks')
        report.update(status='passed', source_sha256=before, pending=[
            'compressed snapshot retention with RunStream page restart', 'register bit layout',
            'encoded completion and cycle-level SRAM service reconciliation', 'CPU qualification',
            'Chronos RTL', 'physical board'])
        print(f'PASS: P1e controller and streaming flush, {result.testsRun} test methods; full P1 remains open')
        print('768 same-cycle command cases; 2,000 watermark + 2,000 flush streams')
        print('Receipt: build/p1e.json; captures: build/control/')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
