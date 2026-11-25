"""Two phones in one scenario, signed in as the same test user. The scenario runs on one phone (`d`); the other,
leased by the same owner, is its colleague: a Driver kept for the whole run."""

import threading
import time

from devtest_common import DEVICES, LEASES, Blocked, Fail, cfg, current_lease, load_json, owner, resolve

from . import state


def _partners(platform: str) -> tuple:
    """The pack's pair_partners for this platform, else every pack phone of the other platform."""
    named = (cfg("pair_partners") or {}).get(platform)
    return tuple(named if named is not None else (a for a, (p, _) in DEVICES.items() if p != platform))


def _other_leased(d):
    """Any other phone this owner leased, by its id: two phones of one platform have no partner alias."""
    own = resolve(d.dev.alias)[1]
    return tuple(p.stem for p in sorted(LEASES.glob("*.json"))
                 if p.stem != own and (current_lease(p.stem) or {}).get("owner") == owner())


def colleague(d, alias=None, require_current_build=True):
    """The other phone this owner leased (a partner of the other platform first, else any other phone this owner
    leased), as a Driver for the whole run, its build checked against the worktree. BLOCKED when no
    such phone is leased. Created once per run (in pre); later calls return the same Driver.
    require_current_build=False is only for a peer that occupies hardware, not a tested app."""
    st = state.of(d)
    if "colleague" in st:
        return st["colleague"]
    from devtest import make_device
    from devtest_scenario import Driver
    names = (alias,) if alias else _partners(d.platform) + _other_leased(d)
    for name in names:
        _, ident, _ = resolve(name)
        lease = current_lease(ident)
        if lease and lease.get("owner") == owner():
            break
    else:
        raise Blocked("no %s leased by '%s': this scenario needs a second phone (devtest --owner %s --device %s lease "
                      "acquire)" % (" or ".join(names) or "second phone", owner(), owner() or "X",
                                    names[0] if names else "ALIAS"))
    dev = make_device(name)
    if require_current_build:
        info = dev.build_info(d.top)
    else:  # the colleague only occupies some hardware: its build is not the build under test
        receipt = load_json(dev.dir / "installed.json", {})
        info = "occupancy peer only; installed receipt=" + str(receipt)
    x = Driver(dev, d.top, d.evidence.parent / ("peer-" + dev.alias), {})
    dev.flow = {}
    st["colleague"] = x
    d.note("%s: %s" % (dev.alias, info))
    return x


def together(*fns, timeout: float = 120) -> list:
    """Runs fns (each taking no argument) at once on their own threads, released by one barrier: two taps on two
    phones within a fraction of a second. Returns each one's start time after the barrier; Fail if any raised."""
    at, errs = [None] * len(fns), []
    gate = threading.Barrier(len(fns))

    def go(i, fn):
        try:
            gate.wait(30)
            at[i] = time.time()
            fn()
        except Exception as e:  # noqa: BLE001 - reported below with the others
            errs.append("%d: %s" % (i, str(e)[:160]))
    threads = [threading.Thread(target=go, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    if errs:
        raise Fail("the simultaneous actions failed: %s" % "; ".join(errs))
    return at
