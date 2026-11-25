#!/usr/bin/env python3
# Checks quota-route.py's Astra rule: the reset within 3 days and a spare of at least 10 points per day left.
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("qr", os.path.join(os.path.dirname(__file__), "quota-route.py"))
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)

now, day, fail = 1_800_000_000, 86400, 0
for name, days_left, spare, expected in [
    ("4 days to the reset and spare 60: too early", 4, 60, False),
    ("3 days to the reset and spare 31: on", 3, 31, True),
    ("2.5 days to the reset and spare 20: under 25", 2.5, 20, False),
    ("1 day to the reset and spare 38: on", 1, 38, True),
    ("1 day to the reset and spare 8: under 10", 1, 8, False),
    ("6 hours to the reset and spare 3: over 2.5", 0.25, 3, True),
    ("1 day to the reset and negative spare: off", 1, -2, False),
]:
    qr.spare = lambda w, now, s=spare: s
    got = qr.astra({"used": 50.0, "reset": now + days_left * day}, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
sys.exit(fail)
