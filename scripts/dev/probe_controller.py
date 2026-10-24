#!/usr/bin/env python3
"""Compatibility entry point: the P3 probes now live in the durable P4 suite."""
import runpy
from pathlib import Path

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parent / 'tests' / 'run.py'), run_name='__main__')
