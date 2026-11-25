"""Text input and exact native values on Android/iOS, including fields whose cap is under test. For apps whose
text fields are a tagged wrapper holding a label and the editable child: the pack's flows/tags.py names the two
inner tags as TEXT_FIELD_LABEL and TEXT_INPUT."""
import json
import re
import xml.etree.ElementTree as ET

from devtest_common import Blocked

from . import phone


class _Tags:
    def __getattr__(self, name):
        try:
            from . import tags
            return getattr(tags, name)
        except (ImportError, AttributeError):
            raise Blocked("flows.fields needs %s in the pack's flows/tags.py" % name) from None


T = _Tags()


def blur(x, tag: str) -> None:
    """Leave a text field by tapping its non-editable label, on Android/iOS. Tapping an adjacent field instead is
    unreliable: a validation on blur can scroll the form under the tap."""
    if x.platform == "android":
        wrapper = x.dev.find(tag)
        label = next((e for e in wrapper.el.iter() if e.get("resource-id") == T.TEXT_FIELD_LABEL), None) if wrapper else None
        bounds = [int(v) for v in re.findall(r"\d+", label.get("bounds", ""))] if label is not None else []
        if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise Blocked("visible field label missing: " + tag)
        x.dev.sh("input tap %d %d" % ((bounds[0]+bounds[2])//2, (bounds[1]+bounds[3])//2))
    else:
        labels = x.dev.elements("xpath", "//*[@name='%s']//*[@name='%s']" % (tag, T.TEXT_FIELD_LABEL))
        if not labels:
            raise Blocked("field label missing: " + tag)
        x.dev.http("POST", "/session/%s/element/%s/click" % (x.dev.sid(), labels[0]), {})
    phone.settle(x)
    phone.dismiss_keyboard(x)

def _native_input(x, tag):
    # UIKit's input can extend a little beyond its Compose wrapper: ancestry first, geometry only as a fallback.
    nested = x.dev.elements("xpath", "//*[@name='%s']//*[@name='%s']" % (tag, T.TEXT_INPUT))
    if len(nested) == 1:
        return nested[0]
    wrappers = x.dev.elements("accessibility id", tag)
    if len(wrappers) != 1:
        raise Blocked("expected one wrapper %s, found %s" % (tag, len(wrappers)))
    box = x.dev.rect(wrappers[0])
    def inside(r):
        return r["x"] >= box["x"]-1 and r["y"] >= box["y"]-1 and r["x"]+r["width"] <= box["x"]+box["width"]+1 and r["y"]+r["height"] <= box["y"]+box["height"]+1
    inputs = [e for e in x.dev.elements("accessibility id", T.TEXT_INPUT) if inside(x.dev.rect(e))]
    if len(inputs) != 1:
        raise Blocked("expected one input in %s, found %d" % (tag, len(inputs)))
    return inputs[0]

def replace(x, tag: str, value: str) -> None:
    """Real input without assuming the accepted value equals the input (caps are asserted separately).
    Android clipboard and iOS native field; the wrapper is tagged, its editable child is not.
    """
    if x.platform == "android":
        x.dev.hide_keyboard()
        x.tap(tag)
        x.wait(lambda: x.dev.keyboard_shown() and x.dev.focused(tag), 10, "input focused: " + tag)
        x.dev.sh("input keycombination KEYCODE_CTRL_LEFT KEYCODE_A")
        x.dev.sh("input keyevent KEYCODE_DEL")
        x.dev.paste_text(value)
    else:
        element = _native_input(x, tag)
        base = "/session/%s/element/%s" % (x.dev.sid(), element)
        x.dev.http("POST", base + "/click")
        x.dev.http("POST", base + "/clear")
        if value:
            x.dev.http("POST", base + "/value", {"value": list(value)}, timeout=180)

def read(x, tag: str, evidence: str = None) -> str:
    """Exact editable text, plus optional UI tree and JSON evidence; no screenshot. Android/iOS."""
    if x.platform == "android":
        nodes = x.dev.dump()
        wrapper = x.dev.find(tag, nodes)
        if wrapper is None:
            raise Blocked("field missing: " + tag)
        value = x.dev.field_value(wrapper)
        raw = ET.tostring(nodes[0].el, encoding="unicode")
    else:
        raw = x.dev.xml_source(full_text=True)
        wrapper = next((e for e in ET.fromstring(raw).iter() if e.get("name") == tag), None)
        if wrapper is None:
            raise Blocked("field wrapper missing: " + tag)
        inputs = [e for e in wrapper.iter() if e.get("type") in ("XCUIElementTypeTextField", "XCUIElementTypeTextView") or e.get("name") == T.TEXT_INPUT]
        if len(inputs) != 1:
            value = x.dev.attrs(_native_input(x, tag)).get("value") or ""
        else:
            value = inputs[0].get("value", "")
    if evidence:
        x.evidence.mkdir(parents=True, exist_ok=True)
        (x.evidence / (evidence + ".ui.xml")).write_text(raw)
        (x.evidence / (evidence + ".json")).write_text(json.dumps(dict(tag=tag, value=value, length=len(value)), indent=2))
    x.note("field %s value=%r length=%d" % (tag, value, len(value)))
    return value
