#!/usr/bin/env python3
"""Run the stdlib-only P4 suite and retain machine-readable per-case evidence."""
from contextlib import redirect_stdout, redirect_stderr
import json
from pathlib import Path
import sys
import time
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import RUN, resources


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.subcases = 0
        self.resourceBlocker = ''

    def startTest(self, test):
        self.began = time.monotonic()
        snapshot, blocker = resources()
        self.case = {'test': test.id(), 'status': 'passed', 'subcases': 0, 'resourcesBefore': snapshot}
        if blocker:
            self.resourceBlocker = blocker
            self.stop()
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases += 1
        self.case['subcases'] += 1
        if err:
            self.case['status'] = 'failed'
        super().addSubTest(test, subtest, err)

    def addFailure(self, test, err):
        self.case['status'] = 'failed'
        super().addFailure(test, err)

    def addError(self, test, err):
        self.case['status'] = 'error'
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.case.update(status='skipped', reason=reason)
        super().addSkip(test, reason)

    def stopTest(self, test):
        snapshot, blocker = resources()
        self.case['resourcesAfter'] = snapshot
        if blocker:
            self.resourceBlocker = blocker
            self.stop()
        self.case['seconds'] = round(time.monotonic() - self.began, 3)
        self.cases.append(self.case)
        super().stopTest(test)


def main():
    print(f'Test artifacts (preserved, never deleted): {RUN}', flush=True)
    # No installs or application/engine runtime prerequisites. This suite is
    # Linux-specific because the controller's ownership proof uses /proc/pidfd.
    if sys.platform != 'linux':
        raise SystemExit('The development controller suite requires Linux')
    began = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='test_*.py')
    with (RUN / 'unittest.log').open('w') as log, redirect_stdout(log), redirect_stderr(log):
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Result).run(suite)
    duration = round(time.monotonic() - began, 3)
    report = {'testsRun': result.testsRun, 'subcases': result.subcases,
              'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'seconds': duration,
              'successful': result.wasSuccessful() and not result.resourceBlocker,
              'resourceBlocker': result.resourceBlocker, 'cases': result.cases}
    (RUN / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print((RUN / 'unittest.log').read_text())
    print(json.dumps({key: value for key, value in report.items() if key != 'cases'}))
    print(f'Per-case timings/results: {RUN / "report.json"}')
    raise SystemExit(0 if report['successful'] else 1)


if __name__ == '__main__':
    main()
