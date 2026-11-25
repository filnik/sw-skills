"""flows: what scenarios share instead of copying. Layers: drivers (devtest_android/ios) -> DSL (devtest_scenario)
-> flows -> scenarios.

The modules here know no app: `phone` (settle, keyboard, background and foreground, scrolling, log lines), `fields`,
`state`, `pair` (two phones in one scenario), `storagefiles`. The active pack's flows/ folder is merged into this
package, so a scenario and a pack flow import both kinds the same way:

    from flows import phone, state, pair            # the engine's
    from flows import nav, tags as T, logs as L     # the pack's (flows/nav.py, tags.py, logs.py in the pack)

Rules: a flow branches on the platform inside, so a scenario never does; a flow waits on stored state (DB, prefs,
log) rather than on pixels; a lesson learnt on a phone is fixed in the flow once. A helper used by one scenario stays
in it; once a second scenario needs it, it moves to a flow (`devtest lint` finds the copies). The index a scenario
writer reads is API.md (`devtest api` regenerates it).
"""

from devtest_common import PACK

if PACK and (PACK / "flows").is_dir():
    __path__.append(str(PACK / "flows"))
