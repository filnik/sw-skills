"""The app starts, goes to the background with Home and comes back without crashing. It needs no knowledge of the app,
only the package or bundle id of the pack: copy its shape for a first scenario.

    devtest.py --owner me --device pixel run examples/open-app.py --no-build-check

An app without `screen.*` tags sets `"launch": {"screen_tags": false}` in its pack, so `launch` returns once the app
is in front.
"""

from devtest_scenario import Scenario
from flows import phone

S = Scenario("EXAMPLE open the app", criteria={
    "E1": "A cold start puts the app in front",
    "E2": "After Home and back the app is in front again and has not crashed",
})


def in_front(d) -> bool:
    return d.dev.foreground() == d.dev.app


@S.pre
def setup(d):
    d.launch(restart=True)


@S.step("E1 the app is in front after a cold start", covers=["E1"])
def _(d):
    d.wait(lambda: in_front(d), 15, "the app in front")


@S.step("E2 Home, then back to the app", covers=["E2"])
def _(d):
    m = d.mark()
    phone.to_background(d)
    d.wait(lambda: not in_front(d), 10, "the app in the background")
    phone.to_foreground(d)
    d.wait(lambda: in_front(d), 15, "the app in front again")
    d.dev.crash_check(m.offset)
