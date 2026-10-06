"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from browser_harness.admin import daemon_browser_ready, ensure_daemon
from browser_harness.helpers import cdp

from . import config, console

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


# A warm tab gets this long to prove it is alive before it is replaced.
HEALTH_TIMEOUT = 2.0


def connect_chrome():
    """Reach Chrome through the browser-harness daemon. A new connection makes Chrome ask "Allow remote debugging?";
    approval stays manual, so say so, and give up after approval_wait_seconds instead of holding the queue."""
    if not daemon_browser_ready():
        wait = config.get("approval_wait_seconds")
        console.say(
            f"  connecting to Chrome; if it asks “Allow remote debugging?”, click Allow (waiting up to {wait} s)"
        )
        ensure_daemon(wait=wait)
    else:
        ensure_daemon()


class Browser:
    def __init__(self, url, background=False, target=None, keep_page=False):
        """A tab at `url`. With `target`, a warm tab to reuse: it is checked first, replaced if it fails, and left
        on its current page only when `keep_page` and that page is already under `url`. `self.tab` records which."""
        self.target = self.session = None
        self.broken = False  # set when Chrome stops answering, so the pool drops this tab instead of keeping it
        connect_chrome()
        try:
            if target and self.adopt(target):
                if keep_page and (self.evaluate("location.href") or "").startswith(url):
                    self.tab = "reused"
                    return
                self.tab = "navigated"
            else:
                # A foreground tab is how you watch a run; a background one stays out of your way in the same
                # Chrome, with the same profile and logins. Either way the tab is never activated after this.
                self.target = cdp("Target.createTarget", url="about:blank", background=background)["targetId"]
                self.attach()
                self.tab = "new"
            self.call("Page.navigate", url=url)
        except BaseException:
            # A half-built background tab would be invisible, so never leave one behind.
            self.close()
            raise
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except StalePage:  # mid-navigation; a slow page gets the rest of the 15 s, then the model decides
                pass
            time.sleep(0.02)

    def attach(self):
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        # Both overrides belong to the session, so a re-attached warm tab needs them again.
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Focus emulation keeps rAF, timers and menus running in a tab you are not looking at.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def adopt(self, target):
        """Take over a warm tab if it is still there and answering; otherwise close what is left of it."""
        try:
            alive = any(t["targetId"] == target for t in cdp("Target.getTargets")["targetInfos"])
            if not alive:  # you closed it, Chrome discarded it, or Chrome restarted
                return False
            self.target = target
            self.attach()
            try:  # a background tab Chrome froze must wake before it can answer
                self.call("Page.setWebLifecycleState", state="active")
            except RuntimeError:
                pass
            if (
                cdp(
                    "Runtime.evaluate",
                    session_id=self.session,
                    expression="1",
                    returnByValue=True,
                    _response_timeout=HEALTH_TIMEOUT,
                )
                .get("result", {})
                .get("value")
                == 1
            ):
                return True
        except (RuntimeError, TimeoutError):
            pass
        self.close()
        self.target = self.session = None
        return False

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        """Read-only, so a Chrome that does not answer in time is just a stale page: observe and decide again."""
        try:
            response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        except TimeoutError:
            raise StalePage("Chrome did not answer in time") from None
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except (RuntimeError, TimeoutError):
                pass
        timeouts = 0
        for attempt in range(10):
            try:
                return browser_operation({"operation": "observe", "session": self.session})
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
            except TimeoutError:
                # Reading changes nothing, so try again; three silent reads in a row mean Chrome is stuck.
                timeouts += 1
                if timeouts == 3:
                    self.broken = True
                    raise RuntimeError("Chrome stopped responding; nothing more was done") from None
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            try:
                cdp("Target.closeTarget", targetId=self.target)
            except (RuntimeError, TimeoutError):
                pass  # already gone
            self.target = None

    def release(self):
        """Hand the tab back to the pool: drop the session, keep the tab."""
        if self.session:
            try:
                cdp("Target.detachFromTarget", sessionId=self.session)
            except (RuntimeError, TimeoutError):
                pass
            self.session = None


class Tabs:
    """Warm background tabs, one per site, for the life of `jev serve`. Only background runs use them: a tab you watch
    is yours once its run ends. A tab is reused in place only after a run that ended done; anything else, a blocked,
    timed-out or cancelled run, a half-typed field, means the next run starts from the skill's URL."""

    def __init__(self):
        self.warm = {}  # site → {"target": id, "status": how the last run on it ended}

    @staticmethod
    def site(url):
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def open(self, url):
        warm = self.warm.pop(self.site(url), None) or {}
        return Browser(url, background=True, target=warm.get("target"), keep_page=warm.get("status") == "done")

    def keep(self, browser, url, status):
        if browser.broken:
            browser.close()
            return
        browser.release()
        if browser.target:
            self.warm[self.site(url)] = {"target": browser.target, "status": status}

    def close_all(self):
        for warm in self.warm.values():
            try:
                cdp("Target.closeTarget", targetId=warm["target"])
            except (RuntimeError, TimeoutError, OSError):
                pass
        self.warm.clear()


# The server's warm tabs. None outside `jev serve`: a one-off run has nothing to keep a tab for.
TABS = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    return info
