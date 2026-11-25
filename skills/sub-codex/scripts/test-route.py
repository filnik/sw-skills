#!/usr/bin/env python3
# Verifica la strada di quota-route.py: Codex fino al 70%, poi fast o lento; dall'80% e sopra il budget su Claude
# solo se Claude ha margine (settimana al massimo al 60%, 5 ore sotto il 50%); Codex finito sempre su Claude.
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("qr", os.path.join(os.path.dirname(__file__), "quota-route.py"))
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)

now, fail = 1_800_000_000, 0
for name, codex_used, codex_spare, claude, expected in [
    ("Codex 50%: Codex", 50, -20, {"used": 20.0, "five": 10}, "codex"),
    ("Codex 85% nel budget: Codex", 85, 5, {"used": 20.0, "five": 10}, "codex"),
    ("Codex 75% sopra il budget: Codex lento", 75, -10, {"used": 20.0, "five": 10}, "codex"),
    ("Codex 82% sopra il budget, Claude 20%: Claude", 82, -10, {"used": 20.0, "five": 10}, "claude"),
    ("Codex 82% sopra il budget, Claude 5 ore al 55%: Codex lento", 82, -10, {"used": 20.0, "five": 55}, "codex"),
    ("Codex 82% sopra il budget, Claude settimana al 65%: Codex lento", 82, -10, {"used": 65.0, "five": 10}, "codex"),
    ("Codex 82% sopra il budget, limiti di Claude illeggibili: Codex lento", 82, -10, None, "codex"),
    ("Codex 100%: Claude", 100, -30, {"used": 90.0, "five": 90}, "claude"),
]:
    qr.spare = lambda w, now, s=codex_spare: s
    got, _ = qr.route({"used": float(codex_used), "reset": now + 86400}, claude, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected

# Scala degli agent Claude: mai Sonnet o Opus sotto high, mai Fable sotto medium.
floors = {"sonnet": "high", "opus": "high", "fable": "medium"}
order = ["low", "medium", "high", "xhigh", "max"]
for kind, bands in qr.LADDER.items():
    for band, (m, e) in bands.items():
        ok = order.index(e) >= order.index(floors[m])
        print(f"{'ok  ' if ok else 'FAIL'} minimo rispettato: {kind} {band} {m} {e}")
        fail |= not ok
for name, claude_spare, claude, expected in [
    ("Claude 11%, 5 ore 16%, avanzo 40: ample", 40, {"used": 11.0, "five": 16}, "ample"),
    ("Claude 30%, 5 ore 35%: normal", 40, {"used": 30.0, "five": 35}, "normal"),
    ("Claude 60%, avanzo 15: normal", 15, {"used": 60.0, "five": 10}, "normal"),
    ("Claude 40%, avanzo negativo: tight", -5, {"used": 40.0, "five": 10}, "tight"),
    ("Claude 80%: tight", 15, {"used": 80.0, "five": 10}, "tight"),
    ("Claude 5 ore al 65%: tight", 40, {"used": 20.0, "five": 65}, "tight"),
    ("limiti di Claude illeggibili: normal", 40, None, "normal"),
]:
    qr.spare = lambda w, now, s=claude_spare: s
    got = qr.claude_band(claude and {**claude, "reset": now + 86400}, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
# Quanti agent in parallelo: Claude 3/2/1 per fascia; Codex 3 sotto il 70%, 2 sopra nel budget, 1 sopra il budget.
for name, got, expected in [
    ("Claude ample: 3 in parallelo", qr.CLAUDE_PARALLEL["ample"], 3),
    ("Claude normal: 2 in parallelo", qr.CLAUDE_PARALLEL["normal"], 2),
    ("Claude tight: 1 alla volta", qr.CLAUDE_PARALLEL["tight"], 1),
]:
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
for name, codex_used, codex_spare, expected in [
    ("Codex 40%: 3 run in parallelo", 40, -30, 3),
    ("Codex 75% nel budget: 2 run", 75, 5, 2),
    ("Codex 75% sopra il budget: 1 run", 75, -20, 1),
]:
    qr.spare = lambda w, now, s=codex_spare: s
    got = qr.codex_parallel({"used": float(codex_used), "reset": now + 86400}, now)
    print(f"{'ok  ' if got == expected else 'FAIL'} {name}")
    fail |= got != expected
print(f"{'ok  ' if qr.codex_parallel(None, now) == 2 else 'FAIL'} uso di Codex illeggibile: 2 run")
fail |= qr.codex_parallel(None, now) != 2
sys.exit(fail)
