import collections
import hashlib
import io
import json
import sys
import unittest

from model.chronos.capture_session import decode_capture
from model.chronos.compact_decode import decode_page
from model.chronos.events import Observation
from model.chronos.predicates import KINDS
from model.chronos.registers import MAP, MAP_PATH, RegisterFile, pack, unpack
from scripts.config import ROOT, read_json
from scripts.model_check import fingerprints


def readout(config, folder):
    registers = RegisterFile(config)
    registers.write('CFG_SPLIT', pack('CFG_SPLIT', pre_pages=config['pre_pages'], post_pages=config['post_pages']))
    registers.write('CFG_MODE', pack('CFG_MODE', codec='compact-v1', keep_kinds=0x7F))
    registers.write('CFG_POST_TICKS_LO', 8)
    registers.write('TRIG0_CTRL', pack('TRIG0_CTRL', enable=1, mode='range', kinds=1 << KINDS.index('BUS_REQ')))
    registers.write('TRIG0_BASE', 0x9000)
    registers.write('TRIG0_LIMIT', 0x9100)
    registers.write('COMMAND', pack('COMMAND', configure=1, arm=1))
    tick = 1
    registers.cycle(tick)
    for index in range(96):
        tick += 1
        items = [Observation('RETIRE', dict(pc=0x1000 + 4 * index, next_pc=0x1004 + 4 * index, length=4, boundary=0))]
        if index % 16 == 15:
            items.append(Observation('BUS_REQ', dict(transaction=index, address=0x8F00 + 16 * index, write=False,
                                                     data=0, mask=15)))
        registers.cycle(tick, items, service=True)
    while unpack('STATUS', registers.read('STATUS'))['state'] != 'FROZEN':
        tick += 1
        registers.cycle(tick, service=True)
    registers.write('READ_SESSION_LO', registers.read('SESSION_ID_LO'))
    length = registers.read('READ_LENGTH')
    wire = b''.join(registers.read('READ_DATA').to_bytes(4, 'little') for _ in range(-(-length // 4)))[:length]
    path = folder / 'readout.pages'
    path.write_bytes(wire)
    page_bytes = config['page_bytes']
    events = tuple(event for offset in range(0, length, page_bytes)
                   for event in decode_page(wire[offset:offset + page_bytes], page_bytes=page_bytes)['events'])
    decoded = decode_capture(registers.controller.read()[1])
    trigger = decoded['metadata']['capture']['trigger']
    if events != decoded['events'] or trigger['matches'] != [dict(source=1, lane=0, reason='slot0')]:
        raise RuntimeError('register readout differs from the frozen capture or trigger slot')
    return dict(status=unpack('STATUS', registers.read('STATUS')), trigger=trigger,
                trigger_match=unpack('TRIG_MATCH', registers.read('TRIG_MATCH')),
                retained_events=len(decoded['events']), codec=decoded['fragment']['manifest']['codecs'][0],
                bytes=length, sha256=hashlib.sha256(wire).hexdigest(), artifact=str(path.relative_to(ROOT)))


def main():
    folder = ROOT / 'build/registers'
    folder.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/p1g.json'
    report = dict(status='failed', scope='P1g register map and trigger slots')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/model'), pattern='test_registers.py')
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (folder / 'tests.log').write_text(log.getvalue())
        report['tests'] = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                               skipped=len(result.skipped))
        if not result.wasSuccessful() or not result.testsRun or result.skipped:
            print(log.getvalue(), file=sys.stderr)
            raise RuntimeError('register checks failed or were skipped')
        report['map'] = dict(path=str(MAP_PATH.relative_to(ROOT)), sha256=hashlib.sha256(MAP_PATH.read_bytes()).hexdigest(),
                             registers=len(MAP['registers']), window_bytes=MAP['window_bytes'],
                             groups=dict(collections.Counter(r['group'] for r in MAP['registers'].values())))
        report['readout'] = readout(read_json(ROOT / 'configs/baseline.json'), folder)
        if before != fingerprints():
            raise RuntimeError('source changed during register checks')
        report.update(status='passed', source_sha256=before, pending=[
            'cycle-level page commit and SRAM port timing (P2)', 'register bus timing and access widths (P2)',
            'CPU qualification', 'Chronos RTL', 'physical board'])
        print(f"PASS: P1g register map and trigger slots, {result.testsRun} test methods; {len(MAP['registers'])} registers")
        print(f"Register-driven capture read back through READ_DATA: {report['readout']['bytes']} bytes")
        print('Receipt: build/p1g.json; capture: build/registers/')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
