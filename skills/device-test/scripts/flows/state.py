"""Per-phone state of one run: what the flows remember between steps (the last launch, what the run
created, the colleague phone). It lives on the device object, which outlives the per-step Driver; the
runner empties it at the start of every run, so `--repeat` starts clean."""

import time


def of(x) -> dict:
    """The run state of x's phone."""
    if not hasattr(x.dev, "flow"):
        x.dev.flow = {}
    return x.dev.flow


def stamp(x, platform_letter: bool = True) -> str:
    """HHMM taken once per run, plus the platform's letter: phones that share an account never make the same name.
    A two-phone scenario that names things on both phones passes platform_letter=False and adds its own letter."""
    st = of(x)
    key = "stamp" if platform_letter else "stamp_bare"
    if key not in st:
        st[key] = time.strftime("%H%M") + (x.platform[0] if platform_letter else "")
    return st[key]


def created(x) -> list:
    """(name, local id) of everything this run created on x's phone, in order: a cleanup flow reads it."""
    return of(x).setdefault("created", [])
