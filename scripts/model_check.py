import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def fingerprints():
    paths = [ROOT / '.gitignore', ROOT / 'Makefile']
    for name in ('configs', 'spec', 'model', 'rtl', 'scripts', 'tests', 'third_party'):
        paths.extend(path for path in (ROOT / name).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def main():
    receipt = ROOT / 'build/model.json'
    receipt.parent.mkdir(exist_ok=True)
    before = fingerprints()
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.discover(str(ROOT / 'tests/model')))
    (ROOT / 'build/model-tests.log').write_text(log.getvalue())
    passed = result.wasSuccessful() and result.testsRun and not result.skipped and before == fingerprints()
    receipt.write_text(json.dumps(dict(status='passed' if passed else 'failed', tests=result.testsRun,
                                       failures=len(result.failures), errors=len(result.errors),
                                       source_sha256=before), sort_keys=True, indent=2) + '\n')
    if not passed:
        print(log.getvalue(), file=sys.stderr)
        print('FAIL: model tests failed, were skipped, or sources changed while running', file=sys.stderr)
        return 1
    print(f'PASS: model, {result.testsRun} test methods; receipt build/model.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
