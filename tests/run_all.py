"""Run every test file. Usage: python tests/run_all.py"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failed = []
for name in sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py")):
    r = subprocess.run([sys.executable, os.path.join(HERE, name)], capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {name}  {last if r.returncode == 0 else ''}")
    if r.returncode:
        failed.append(name)
        print((r.stdout + r.stderr)[-3000:])
sys.exit(1 if failed else 0)
