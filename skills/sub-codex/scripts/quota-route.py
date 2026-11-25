#!/usr/bin/env python3
# Dove fare i task medi che Claude delegherebbe da solo: su Codex, tranne fuori DocuSketch o con Codex finito;
# sopra il budget sul tier lento; dall'80% e sopra il budget, con Claude in margine, su agent Claude (sub-claude)
# e Codex resta di riserva per i connettori che ha solo lui.
# Stampa una riga per il contesto (hook UserPromptSubmit); con --codex-tier l'uso settimanale di Codex, il tier
# di sub-codex (fast o default) e il modello delle run --important (astra o sol); con --sub-route la strada di
# sub-codex.sh (codex, claude reserve o claude usedup); con --claude-pick hard|judgement|gathering modello ed effort
# dell'agent Claude, che la riga riporta anche per tutte e tre le difficoltà.
#
# avanzo = 100 - usato - ritmo x ore pesate fino al reset: la quota che a quel ritmo scade senza essere usata.
# ritmo = il maggiore fra quello misurato in questa finestra e 100 / ore pesate di una settimana.
# Nel primo quarto della finestra la proiezione cresce con le ore trascorse: il ritmo iniziale è poco affidabile.
# Pesi dai transcript di ott 2026 (70% lun-ven 9-18:30, 19% sera, 11% weekend): giorno feriale 1,
# sera e weekend 0.25, 3:00-9:00 zero.
import glob, json, os, sys, time
from datetime import datetime

WEEK = 7 * 86400
CODEX_MAX = 100       # Codex settimanale da cui lo si considera finito
FAST_ALWAYS = 70      # Codex settimanale fino a cui sub-codex va sempre sul fast tier
FAST_SLACK = 1.5      # punti che il fast può sforare al reset (~un'ora di lavoro): se Codex finisce prima c'è Claude
ASTRA_DAYS = 3        # giorni prima del reset da cui le run --important di sub-codex possono andare su Astra
ASTRA_PER_DAY = 10    # avanzo minimo per giorno che manca al reset: 30 punti a tre giorni, 10 a uno
CODEX_RESERVE = 80    # Codex settimanale da cui, sopra il budget, il lavoro va su Claude se Claude ha margine
CLAUDE_WEEK_MAX = 60  # margine di Claude: settimana usata al massimo al 60% (l'uso, non l'avanzo: a inizio
CLAUDE_FIVE_MAX = 50  # settimana la proiezione sbaglia) e finestra di 5 ore sotto la metà, che ferma anche la chat

# Modello ed effort degli agent Claude (sub-claude) per difficoltà del task e quota di Claude. Minimi dell'utente
# (25/11): Sonnet e Opus mai sotto high, Fable mai sotto medium. Prezzo per token: Sonnet 1, Opus 2, Fable 4.
LADDER = {
    "hard":      {"ample": ("fable", "high"),  "normal": ("fable", "medium"), "tight": ("opus", "high")},
    "judgement": {"ample": ("opus", "xhigh"),  "normal": ("opus", "high"),    "tight": ("sonnet", "high")},
    "gathering": {"ample": ("sonnet", "high"), "normal": ("sonnet", "high"),  "tight": ("sonnet", "high")},
}


# Agent Claude in parallelo al massimo, per fascia di quota (l'utente, 25/11: "bilanciato in base a quanti token hai").
CLAUDE_PARALLEL = {"ample": 3, "normal": 2, "tight": 1}


def weight(ts):
    t = datetime.fromtimestamp(ts)
    h = t.hour + t.minute / 60
    if 3 <= h < 9:
        return 0.0
    return 1.0 if t.weekday() < 5 and 9 <= h < 18.5 else 0.25


def hours(a, b):
    """Ore pesate fra due epoch, a passi di un quarto d'ora."""
    total, t = 0.0, a
    while t < b:
        total += weight(t) * min(900, b - t) / 3600
        t += 900
    return total


def window(used, reset, now):
    """Uso e reset della finestra settimanale; passato il reset la finestra riparte da zero."""
    if used is None or not reset:
        return None
    if reset <= now:
        used = 0
        while reset <= now:
            reset += WEEK
    return {"used": float(used), "reset": float(reset)}


def read_claude(now):
    # scritto dalla statusline a ogni aggiornamento: rate_limits arriva solo a lei
    name = os.path.basename((os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")).rstrip("/"))
    try:
        with open(os.path.expanduser(f"~/.cache/claude-statusline/limits-{name}.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    w = window(d.get("week_pct"), d.get("week_at"), now)
    if w:
        w["five"] = (d.get("five_pct") or 0) if (d.get("five_at") or 0) > now else 0
    return w


def read_codex(now):
    """Ultimo limite settimanale di Codex nelle sue sessioni (coda degli ultimi file)."""
    files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")), key=os.path.getmtime)
    for path in reversed(files[-5:]):
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 512 * 1024))
            lines = f.read().decode(errors="replace").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                limits = json.loads(line)["payload"]["rate_limits"] or {}
            except (ValueError, KeyError, TypeError):
                continue
            if (limits.get("limit_id") or "codex") != "codex":
                continue
            for w in (limits.get("primary"), limits.get("secondary")):
                if w and w.get("window_minutes") == 10080:
                    return window(w.get("used_percent"), w.get("resets_at"), now)
    return None


def spare(w, now):
    nominal = 100 / hours(now, now + WEEK)
    elapsed = hours(w["reset"] - WEEK, now)
    rate = max(nominal, w["used"] / elapsed) if elapsed >= 1 else nominal
    remaining = hours(now, w["reset"])
    rate *= min(1, 4 * elapsed / (elapsed + remaining))
    return 100 - w["used"] - rate * remaining


def fast_tier(w, now):
    # deciso a ogni lancio: se il fast consuma troppo l'avanzo scende e si torna al default
    return w["used"] <= FAST_ALWAYS or spare(w, now) >= -FAST_SLACK


def astra(w, now):
    # deciso a ogni lancio: ogni run Astra abbassa l'avanzo, e sotto la soglia si torna a Sol
    days = (w["reset"] - now) / 86400
    return days <= ASTRA_DAYS and spare(w, now) >= ASTRA_PER_DAY * days


def claude_margin(claude):
    return bool(claude) and claude["used"] <= CLAUDE_WEEK_MAX and claude.get("five", 0) < CLAUDE_FIVE_MAX


def claude_band(claude, now):
    """ample, normal o tight: quanto possono spendere gli agent Claude. A inizio settimana l'avanzo è ottimista e
    decide l'uso; a fine settimana l'uso è alto anche con quota libera e decide l'avanzo."""
    if not claude:
        return "normal"
    used, five, left = claude["used"], claude.get("five", 0), spare(claude, now)
    if used > 75 or five >= 60 or left < 0:
        return "tight"
    if used <= 50 and five < 30 and left >= 10:
        return "ample"
    return "normal"


def codex_parallel(w, now):
    """Run Codex in parallelo: 3 sotto il 70%, 2 sopra ma nel budget (o uso illeggibile), 1 sopra il budget."""
    if not w:
        return 2
    if w["used"] < FAST_ALWAYS:
        return 3
    return 2 if fast_tier(w, now) else 1


def ladder_line(claude, now):
    band = claude_band(claude, now)
    picks = " · ".join(f"{c} {m} {e}" for c, (m, e) in ((c, LADDER[c][band]) for c in LADDER))
    return f"Claude agents' model and effort (Claude quota {band}, at most {CLAUDE_PARALLEL[band]} in parallel): {picks}."


def route(codex, claude, now):
    """('claude' | 'codex', motivo) per i task medi che Claude delegherebbe da solo."""
    if codex and codex["used"] >= CODEX_MAX:
        return "claude", "Codex is used up"
    if not codex:
        return "codex", "Codex usage unknown, default tier"
    if codex["used"] < FAST_ALWAYS:
        return "codex", "Codex under 70% of its week, fast tier"
    if fast_tier(codex, now):
        return "codex", "fast tier, within budget"
    if codex["used"] >= CODEX_RESERVE and claude_margin(claude):
        return "claude", "Codex kept for its connectors, Claude has margin"
    return "codex", "slower tier, over budget"


def describe(name, w, now):
    if not w:
        return f"{name} unknown"
    s = f"{name} {w['used']:.0f}% weekly, spare {spare(w, now):.0f} pts until {datetime.fromtimestamp(w['reset']):%a %H:%M}"
    return s + (f", 5h {w['five']:.0f}%" if "five" in w else "")


def in_docusketch(path):
    """sub-codex lavora solo nelle cartelle DocuSketch (stessa regola di sub-codex.sh)."""
    root = os.path.realpath(os.path.expanduser("~/Documents/projects/docusketch"))
    p = os.path.realpath(path)
    return p == root or p.startswith(root + os.sep)


def main():
    now = time.time()
    codex = read_codex(now)
    if sys.argv[1:] == ["--codex-tier"]:
        if codex:
            print(f"{codex['used']:g} {'fast' if fast_tier(codex, now) else 'default'}"
                  f" {'astra' if astra(codex, now) else 'sol'}")
        return
    if sys.argv[1:] == ["--sub-route"]:
        side, why = route(codex, read_claude(now), now)
        print("codex" if side == "codex" else "claude usedup" if why.endswith("used up") else "claude reserve")
        return
    if sys.argv[1:2] == ["--claude-pick"]:
        kind = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in LADDER else "judgement"
        print(*LADDER[kind][claude_band(read_claude(now), now)])
        return
    try:
        cwd = json.load(sys.stdin).get("cwd") or os.getcwd() if not sys.stdin.isatty() else os.getcwd()
    except Exception:
        cwd = os.getcwd()
    claude = read_claude(now)
    if not in_docusketch(cwd):
        print("quota route: claude (not a DocuSketch folder: sub-codex and the *-codex skills run only there)."
              f" Do every task with Claude, also when the user asks for Codex. {ladder_line(claude, now)}")
        return
    side, why = route(codex, claude, now)
    if side == "claude":
        action = ("Medium tasks go to Claude agents. Use sub-codex and the *-codex skills as usual: sub-codex.sh does"
                  " not start Codex, exits 5 and prints one Agent call (sub-claude, or the Claude agent the calling"
                  " skill names, e.g. device-tester). Exit 5 is not a Codex failure: make that call as printed, then"
                  " go on with the calling skill's steps; tell the user in one line that the task runs on Claude and why.")
        action += (" Only PostHog, Crashlytics and SigNoz stag/sand runs (--needs-codex) still go to Codex."
                   if why.startswith("Codex kept") else
                   " PostHog, Crashlytics and SigNoz stag/sand cannot run until Codex resets: say so.")
    elif why.startswith("slower"):
        action = ("Hand medium tasks to Codex as usual: sub-codex picks the slower tier by itself. Only an urgent task"
                  " may go to Claude instead; if unsure it is urgent, ask the user.")
    else:
        action = "Hand medium tasks to Codex as usual."
    if side == "codex":
        action += f" At most {codex_parallel(codex, now)} Codex runs in parallel."
    if side == "codex" and codex and astra(codex, now):
        action += (" Astra is on (checked again at every run): pass --important to implementations, code reviews and"
                   " merges; complex implementations, subtle diagnoses and critical fixes go to Codex too, and Claude"
                   " reviews the result.")
    print(f"quota route: {side} ({why}) - {describe('Claude', claude, now)} · {describe('Codex', codex, now)}. {action}"
          f" {ladder_line(claude, now)}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # l'hook non deve mai disturbare il prompt
