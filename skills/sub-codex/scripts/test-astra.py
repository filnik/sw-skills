#!/usr/bin/env python3
# Verifica la regola di Astra in quota-route.py: reset entro 3 giorni e avanzo di almeno 10 punti per giorno rimasto.
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("qr", os.path.join(os.path.dirname(__file__), "quota-route.py"))
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)

now, day, fail = 1_800_000_000, 86400, 0
for name, days_left, spare, expected in [
    ("4 giorni al reset, avanzo 60: troppo presto", 4, 60, False),
    ("3 giorni al reset, avanzo 31: acceso", 3, 31, True),
    ("2,5 giorni al reset, avanzo 20: sotto i 25", 2.5, 20, False),
    ("1 giorno al reset, avanzo 38: acceso", 1, 38, True),
    ("1 giorno al reset, avanzo 8: sotto i 10", 1, 8, False),
    ("6 ore al reset, avanzo 3: sopra i 2,5", 0.25, 3, True),
    ("1 giorno al reset, avanzo negativo: spento", 1, -2, False),
]:
    qr.spare = lambda w, now, s=spare: s
    got = qr.astra({"used": 50.0, "reset": now + days_left * day}, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
sys.exit(fail)
