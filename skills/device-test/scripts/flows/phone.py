"""What any app needs from the phone around its own flows, on both platforms: the pause after a navigation, the
keyboard, Home and back to the app, scrolling a selector into reach, dragging a sheet down, a log line as it appears."""

import re
import time

from devtest_common import Blocked, cfg, read_since

from . import state


def settle(x) -> None:
    """The pause after a navigation before the screen is read again. iOS: reading the accessibility tree while a
    Compose screen animates can hang WDA for over a minute, so it waits for the transition to end."""
    time.sleep(2.5 if x.platform == "ios" else 1)


def to_background(x) -> None:
    """The app sent to the background with Home, its process left running. The request time is kept in the run state
    (last_home_requested_epoch), apart from Home's acknowledgement, for lifecycle timing."""
    state.of(x)["last_home_requested_epoch"] = time.time()
    if x.platform == "android":
        x.dev.sh("input keyevent KEYCODE_HOME")
    else:
        x.dev.http("POST", "/wda/homescreen", timeout=30)


def to_foreground(x) -> None:
    """The app back in front as the user left it: the same process and screen, not a relaunch. Clears the runner's
    stopped-on-purpose marker, or a later step would treat the live app as stopped."""
    if x.platform == "android":
        x.dev.sh("monkey -p %s -c android.intent.category.LAUNCHER 1" % x.dev.app)
    else:
        x.dev.http("POST", "/session/%s/wda/apps/activate" % x.dev.sid(), {"bundleId": x.dev.app}, timeout=30)
    settle(x)
    x.stopped_on_purpose = False


def dismiss_keyboard(x) -> None:
    """Hides the keyboard on either platform. iOS: only a Done that belongs to the keyboard or its accessory toolbar
    is tapped, never a form's own Done button."""
    if x.platform == "ios":
        try:
            buttons = x.dev.elements("xpath", "//XCUIElementTypeKeyboard//*[@name='Done'] | "
                                     "//XCUIElementTypeToolbar//*[@name='Done']")
            if buttons:
                x.dev.http("POST", "/session/%s/element/%s/click" % (x.dev.sid(), buttons[-1]), {})
        except Blocked:
            pass
    x.hide_keyboard()


def scroll_to(x, sel: str, swipes: int = 4, viewport_screens=(), top: int = 110, bottom: int = 95) -> bool:
    """Brings sel on screen; False when it is not there after `swipes` swipes up.
    iOS: the whole column is in the WDA tree, so WDA scrolls the element into view; a lazy list composes it only once
    scrolled near. On the screens named in viewport_screens WDA reports off-screen elements as displayed (and its
    scroll can pick a row's swipe action instead of the list), so there the element's centre must lie between the
    top and bottom bars (`top` and `bottom` points) and plain swipes do the scrolling.
    Android: a tag composed off screen has empty bounds, and a field's wrapper can show while its EditText is still
    off screen, so the node must have a tap point and its EditText must be inside the visible area."""
    if x.platform == "ios" and any(s in x.dev.screens() for s in viewport_screens):
        _, height = x.dev.window()
        for _ in range(swipes + 1):
            el = x.dev._element_for(sel)
            if el is not None and x.dev.visible(sel):
                from devtest_ios import Stale
                try:
                    el = x.dev._element_for(sel)  # WDA may expire an element between displayed and rect
                    if el is None:
                        continue
                    rect = x.dev.rect(el)
                except Stale:
                    settle(x)
                    continue
                center = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
                if top < center < height - bottom:
                    return True
            x.swipe("up")
            settle(x)
        return False
    if x.platform == "ios":
        for _ in range(swipes):
            n = x.dev.node(sel, 3)
            if n is not None:
                if not x.dev.visible(sel):
                    try:
                        x.dev.http("POST", "/session/%s/wda/element/%s/scroll" % (x.dev.sid(), n.el_id),
                                   {"toVisible": True})
                    except Blocked:
                        pass
                return True
            x.swipe("up")
        return x.dev.node(sel, 3) is not None

    def android_reachable(n):
        if n is None or x.dev._tap_point(n) is None:
            return False
        edits = [e for e in n.el.iter() if e.get("class", "").endswith("EditText")]
        if not edits:
            return True
        _, height, top_edge, bottom_edge = x.dev.geometry()
        bounds = [int(v) for v in re.findall(r"\d+", edits[0].get("bounds", ""))]
        return (len(bounds) == 4 and bounds[2] - bounds[0] >= 12 and bounds[3] - bounds[1] >= 12
                and top_edge + height * 0.04 < (bounds[1] + bounds[3]) / 2 < bottom_edge - height * 0.08)

    for _ in range(swipes):
        if android_reachable(x.dev.node(sel, 3)):
            return True
        x.swipe("up")
    return android_reachable(x.dev.node(sel, 3))


def swipe_down(x, from_y: int) -> int:
    """iOS: one finger dragged from from_y, at mid width, to the bottom of the screen, as a user closing a sheet
    would. Returns the y it ended at."""
    w, h = x.dev.window()
    to_y = int(h * 0.92)
    x.dev.http("POST", "/session/%s/actions" % x.dev.sid(), {"actions": [{
        "type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": [
            {"type": "pointerMove", "duration": 0, "x": w // 2, "y": from_y}, {"type": "pointerDown"},
            {"type": "pause", "duration": 100}, {"type": "pointerMove", "duration": 400, "x": w // 2, "y": to_y},
            {"type": "pointerUp"}]}]})
    return to_y


def logged(x, pattern, m) -> list:
    """Lines matching pattern since the mark. iOS: the console can glue two records into one line (an unterminated
    line from another writer), so each record starts at its own timestamp and process header; the capture itself
    stays as it is."""
    header = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+[+-]\d{4} %s\[\d+:\d+\]" % (
        re.escape(cfg("ios.process")) if cfg("ios.process") else r"[^\[\]]+?"))
    records = []
    for line in read_since(x.dev.log, m.offset):
        starts = list(header.finditer(line)) if x.platform == "ios" else []
        if starts:
            records.extend(line[s.start():starts[i + 1].start() if i + 1 < len(starts) else len(line)]
                           for i, s in enumerate(starts))
        else:
            records.append(line)
    return [line for line in records if re.search(pattern, line)]


def wait_line(x, pattern, m, timeout, poll=0.05, lines=None):
    """The first line matching since m, polled tightly (a kill or a tap timed on a line must not lose a second);
    None after timeout. Not an expectation: the caller decides what a missing line means. `lines`: the pack's own
    reader of matching lines, in place of logged()."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = (lines or logged)(x, pattern, m)
        if hits:
            return hits[0]
        time.sleep(poll)
    return None
