import hashlib
import json
import struct
import sys

from model.chronos.registers import MAP, pack
from scripts.config import ROOT
from scripts.doctor import tool_path
from scripts.model_check import fingerprints
from scripts.rtl_check import ADDRESS, CODECS, SOURCES, decode_capture, read_back, readout, run

BUILD = ROOT / 'build/soc'
DEMO = ROOT / 'examples/demo_soc'
IBEX = ROOT / 'third_party/ibex'
PACKAGES = ('vendor/lowrisc_ip/ip/prim/rtl/prim_secded_pkg.sv', 'vendor/lowrisc_ip/ip/prim/rtl/prim_mubi_pkg.sv',
            'vendor/lowrisc_ip/ip/prim_generic/rtl/prim_ram_1p_pkg.sv', 'rtl/ibex_pkg.sv', 'rtl/ibex_cheriot_pkg.sv')
INCLUDED = ('vendor/lowrisc_ip/ip/prim/rtl/prim_assert.sv', 'vendor/lowrisc_ip/ip/prim/rtl/prim_flop_macros.sv')
PAGE_BYTES = 1024
RAM_BYTES = 65536
MASK = 0xFFFFFFFF
# Keep masks: every event kind, and retirements only.
KEEP = dict(all=0x7F, retire=0x01)


def pinned():
    entry = json.loads((ROOT / 'third_party/manifest.json').read_text())['acquired'][0]
    files = {str(path.relative_to(IBEX)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in IBEX.rglob('*') if path.is_file()}
    if files != entry['files']:
        raise RuntimeError('third_party/ibex differs from its pinned manifest')
    modules = [name for name in sorted(files) if name.endswith('.sv') and name not in PACKAGES + INCLUDED]
    return entry['revision'], [IBEX / name for name in PACKAGES + tuple(modules)]


def firmware(codec, keep, folder):
    folder.mkdir(parents=True, exist_ok=True)
    defines = dict(REG_CFG_SPLIT=ADDRESS['CFG_SPLIT'], REG_CFG_MODE=ADDRESS['CFG_MODE'], REG_COMMAND=ADDRESS['COMMAND'],
                   REG_STATUS=ADDRESS['STATUS'], CFG_SPLIT=pack('CFG_SPLIT', pre_pages=16, post_pages=16),
                   CFG_MODE=pack('CFG_MODE', codec=codec, keep_kinds=keep), CMD_ARM=pack('COMMAND', configure=1, arm=1),
                   CMD_STOP=pack('COMMAND', stop=1), STATE_FROZEN=MAP['enums']['state'].index('FROZEN'))
    cc = tool_path('riscv_cc')
    run([cc, '-march=rv32im', '-mabi=ilp32', '-O2', '-nostdlib', '-ffreestanding', '-mno-relax',
         '-T', DEMO / 'firmware/link.ld', *(f'-D{key}={value:#x}' for key, value in defines.items()),
         DEMO / 'firmware/crt0.S', DEMO / 'firmware/main.c', '-o', folder / 'firmware.elf'], folder / 'cc.log')
    run([cc.removesuffix('gcc') + 'objcopy', '-O', 'binary', folder / 'firmware.elf', folder / 'firmware.bin'],
        folder / 'objcopy.log')
    image = (folder / 'firmware.bin').read_bytes()
    words = struct.unpack(f'<{-(-len(image) // 4)}I', image.ljust(-(-len(image) // 4) * 4, b'\0'))
    (BUILD / 'firmware.hex').write_text(''.join(f'{word:08x}\n' for word in words))
    return words


def checksum():
    table = [index * 2654435761 & MASK for index in range(64)]
    total = 0
    for round_ in range(8):
        for index, value in enumerate(table):
            table[index] = total = (total << 1 | total >> 31) & MASK ^ value ^ round_
    for index in range(16):
        table[index] = index
        total = total + table[index + 16] & MASK
    return total


def signed(value, bits):
    return value - (value >> (bits - 1) << bits)


def targets(word, pc):
    # Successors allowed by the instruction word alone; None when they depend on registers or CSRs (JALR, MRET, ECALL).
    opcode = word & 0x7F
    if opcode == 0x6F:
        return {pc + signed((word >> 31) << 20 | (word >> 12 & 0xFF) << 12 | (word >> 20 & 1) << 11
                            | (word >> 21 & 0x3FF) << 1, 21) & MASK}
    if opcode == 0x63:
        return {pc + 4, pc + signed((word >> 31) << 12 | (word >> 7 & 1) << 11 | (word >> 25 & 0x3F) << 5
                                    | (word >> 8 & 0xF) << 1, 13) & MASK}
    if opcode == 0x67 or (opcode == 0x73 and not word >> 12 & 7):
        return None
    return {pc + 4}


def score(name, codec, dump, words):
    lines = [line.split() for line in dump.splitlines()]
    log = [tuple(map(int, rest)) for tag, *rest in lines if tag == 't']
    result, cycles = next(tuple(map(int, rest)) for tag, *rest in lines if tag == 'h')
    if result != checksum():
        raise RuntimeError(f'{name}: firmware returned {result:#x}, expected {checksum():#x}')
    back = read_back(name, dump, PAGE_BYTES)
    status = back['status']
    if (status['state'], status['stop_reason'], status['storage_error'], status['configured'], back['session']) != \
            ('FROZEN', 'manual', 'none', 1, 1):
        raise RuntimeError(f'{name}: status {status}, session {back["session"]}')
    pages, first, events = decode_capture(name, back['image'], PAGE_BYTES, codec, 1)
    for source, counters in enumerate(back['acct']):
        if counters['observed'] != counters['filtered'] + counters['admitted'] + counters['ingress_dropped'] or \
                sum(event.source == source for event in events) > counters['admitted']:
            raise RuntimeError(f'{name}: source {source} accounting {counters}')
    retires = [event for event in events if event.source == 0]
    if not retires or {event.observation.kind for event in retires} != {'RETIRE'}:
        raise RuntimeError(f'{name}: no retirement history')
    # Core-side evidence: retirement n of the capture is RVFI retirement k + n, at a constant cycle offset from its tick.
    anchor = retires[0]
    fits = [k for k in range(len(log) - anchor.sequence)
            if all(k + event.sequence < len(log) and log[k + event.sequence][1:] == (event.observation.fields['pc'],
                   event.observation.fields['next_pc']) and log[k + event.sequence][0] - event.tick ==
                   log[k + anchor.sequence][0] - anchor.tick for event in retires)]
    if len(fits) != 1:
        raise RuntimeError(f'{name}: retirements match the RVFI log at {len(fits)} alignments')
    offset = log[fits[0] + anchor.sequence][0] - anchor.tick
    if log[fits[0]][0] < offset or (fits[0] and log[fits[0] - 1][0] >= offset):
        raise RuntimeError(f'{name}: sequence 0 is not the first retirement after arm')
    # Architectural evidence: the firmware image allows each next_pc, and consecutive retirements chain.
    previous = None
    for event in retires:
        pc, next_pc = event.observation.fields['pc'], event.observation.fields['next_pc']
        allowed = targets(words[pc // 4], pc) if pc < len(words) * 4 else set()
        if event.observation.fields['boundary'] or (allowed is not None and next_pc not in allowed) or \
                (previous and previous.sequence + 1 == event.sequence and previous.observation.fields['next_pc'] != pc):
            raise RuntimeError(f'{name}: retirement inconsistent with the firmware image {event}')
        previous = event
    # One request is outstanding at a time, so a response directly follows its request in the source sequence.
    previous = None
    for event in (event for event in events if event.source == 1):
        fields = event.observation.fields
        if event.observation.kind == 'BUS_REQ':
            if not fields['mask'] or (fields['data'] and not fields['write']) or \
                    not (fields['address'] < RAM_BYTES or fields['address'] >> 12 == 0x10000):
                raise RuntimeError(f'{name}: bus request outside the map or with read data {event}')
        elif fields['error'] or (previous and previous.sequence + 1 == event.sequence and
                                 (previous.observation.kind != 'BUS_REQ' or
                                  previous.observation.fields['transaction'] != fields['transaction'] or
                                  previous.observation.fields['write'] != (fields['data'] is None))):
            raise RuntimeError(f'{name}: bus response does not complete the preceding request {event}')
        previous = event
    return dict(cycles=cycles, rvfi_retirements=len(log), pages=pages, first_generation=first,
                retained=dict(retire=len(retires), bus=sum(event.source == 1 for event in events)),
                observed=[counters['observed'] for counters in back['acct']],
                dropped=[counters['ingress_dropped'] for counters in back['acct']])


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    receipt = ROOT / 'build/soc.json'
    report = dict(status='failed', scope='Ibex demo SoC with Chronos (Verilator simulation)')
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        before = fingerprints()
        revision, ibex = pinned()
        verilator = tool_path('verilator')
        if not verilator or not tool_path('riscv_cc'):
            raise RuntimeError('verilator and the RISC-V compiler are required; configure their paths, no download attempted')
        common = ['-Wall', '--top-module', 'chronos_soc', '+define+RVFI', f'-GFIRMWARE="{BUILD.relative_to(ROOT)}/firmware.hex"',
                  f'+incdir+{IBEX}/vendor/lowrisc_ip/ip/prim/rtl', f'+incdir+{IBEX}/vendor/lowrisc_ip/dv/sv/dv_utils',
                  ROOT / 'tests/integration/ibex.vlt', *ibex, *SOURCES, DEMO / 'rtl/chronos_soc.sv']
        text = run([verilator, '--lint-only', *common], BUILD / 'lint.log')
        if '%Warning' in text or '%Error' in text:
            raise RuntimeError('lint findings in the demo SoC; see build/soc/lint.log')
        run([verilator, '--cc', '--exe', '--build', '--assert', '-O2', '--x-assign', 'unique', '--x-initial', 'unique',
             '--Mdir', BUILD / 'obj', *common, ROOT / 'tests/integration/tb_soc.cpp', '-o', 'tb'], BUILD / 'build.log')
        script = BUILD / 'readout.script'
        script.write_text('\n'.join(readout(1)) + '\n')
        results = {}
        for codec in CODECS:
            for label, keep in KEEP.items():
                folder = BUILD / f'{codec}-{label}'
                words = firmware(codec, keep, folder)
                run([BUILD / 'obj/tb', script, folder / 'soc.dump', 2000000], folder / 'tb.log')
                results[f'{codec}/{label}'] = score(f'soc/{codec}/{label}', codec, (folder / 'soc.dump').read_text(), words)
        if before != fingerprints():
            raise RuntimeError('source changed during SoC checks')
        report.update(status='passed', ibex_revision=revision, configuration='upstream small, RVFI, RV32IM firmware',
                      runs=results, source_sha256=before,
                      pending=['trap and interrupt events need the trap cause (RVFI has none)', 'host tools', 'board'])
        print(f'PASS: Ibex {revision[:7]} demo SoC; firmware checksum, capture decode, RVFI and image consistency')
        for key, item in results.items():
            print(f"{key}: {item['rvfi_retirements']} retirements in {item['cycles']} cycles; retained "
                  f"{item['retained']['retire']} retirements and {item['retained']['bus']} bus events in "
                  f"{item['pages']} pages; dropped {item['dropped'][:2]} of {item['observed'][:2]}")
        print('Receipt: build/soc.json; firmware, dumps, and logs: build/soc/')
        return 0
    except (OSError, ValueError, RuntimeError, StopIteration, IndexError) as error:
        report['error'] = str(error) or type(error).__name__
        print(f'FAIL: {report["error"]}', file=sys.stderr)
        return 1
    finally:
        receipt.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
