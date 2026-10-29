"""Arca - native macOS shell (NSWindow + WKWebView) around the local API.

Only true Cocoa protocols live on the Objective-C classes below; everything
else is a plain module function so pyobjc never has to guess a selector.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time

import objc
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyRegular, NSAppearance,
    NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSBackingStoreBuffered,
    NSColor, NSDragOperationCopy, NSDragOperationNone, NSDragPboard, NSImage,
    NSMakePoint,
    NSMakeRect, NSMenu, NSMenuItem, NSOpenPanel, NSPasteboard,
    NSPasteboardTypeFileURL, NSPasteboardTypeString, NSView,
    NSEvent, NSEventMaskLeftMouseDragged, NSEventMaskLeftMouseUp,
    NSEventModifierFlagControl, NSEventTypeLeftMouseDown,
    NSEventTypeLeftMouseDragged, NSEventTypeLeftMouseUp,
    NSScreen, NSTerminateNow,
    NSViewMinYMargin, NSViewWidthSizable,
    NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSMakeSize, NSObject, NSString, NSURL, NSURLRequest
from WebKit import (
    WKUserContentController, WKUserScript, WKUserScriptInjectionTimeAtDocumentStart,
    WKWebView, WKWebViewConfiguration,
)

from . import engine as eng
from . import integration
from . import server as srv
from . import stats
from . import util
from .settings import Settings

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _web_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cand = os.path.join(base, "arca", "web")
        if os.path.isdir(cand):
            return cand
    return WEB_DIR


# --------------------------------------------------------------------- bridge
def eval_js(ctx, js: str) -> None:
    view = getattr(ctx, "web", None)
    if view is None:
        win = getattr(ctx, "window", None)
        if not win:
            return
        view = win.contentView()
    if hasattr(view, "evaluateJavaScript_completionHandler_"):
        view.evaluateJavaScript_completionHandler_(js, None)


def reply(ctx, mid, result) -> None:
    if mid is None:
        return
    payload = json.dumps(result, ensure_ascii=False)
    eval_js(ctx, "window.__arcaReply && window.__arcaReply(%s, %s);"
            % (json.dumps(str(mid)), payload))


def set_appearance(ctx, mode: str) -> None:
    name = {"light": NSAppearanceNameAqua, "dark": NSAppearanceNameDarkAqua}.get(mode)
    appearance = NSAppearance.appearanceNamed_(name) if name else None
    try:
        NSApp.setAppearance_(appearance)
    except Exception:
        pass
    win = getattr(ctx, "window", None)
    if win:
        try:
            win.setAppearance_(appearance)
        except Exception:
            pass


def open_panel(ctx, params: dict, mid=None) -> None:
    kind = params.get("kind") or "files"
    panel = NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(kind in ("files", "archive"))
    panel.setCanChooseDirectories_(kind in ("files", "folder", "dest"))
    panel.setAllowsMultipleSelection_(bool(params.get("multiple")))
    panel.setCanCreateDirectories_(True)
    panel.setTreatsFilePackagesAsDirectories_(True)
    if params.get("title"):
        panel.setMessage_(params["title"])
    if params.get("prompt"):
        panel.setPrompt_(params["prompt"])
    target = params.get("dir") or ""
    if kind == "dest" and target and os.path.isdir(target):
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(target))

    def done(result):
        paths = []
        if result == 1:  # NSModalResponseOK
            for url in panel.URLs():
                paths.append(url.path())
        reply(ctx, mid, {"paths": paths})
        try:
            ctx.panels.remove(panel)
        except (ValueError, AttributeError):
            pass

    if not hasattr(ctx, "panels"):
        ctx.panels = []
    ctx.panels.append(panel)
    win = getattr(ctx, "window", None)
    if win is not None:
        panel.beginSheetModalForWindow_completionHandler_(win, done)
    else:
        done(panel.runModal())


def handle_message(ctx, payload: dict) -> None:
    mid = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    try:
        if method == "pick":
            open_panel(ctx, params, mid)
            return
        elif method == "dragPaths":
            result = {"paths": _dragging_paths()}
        elif method == "clipPaths":
            # Files copied in Finder, so "paste" inside the app is a real paste.
            result = {"paths": _board_paths(NSPasteboard.generalPasteboard())}
        elif method == "copy":
            text = params.get("text")
            if text is None:
                result = {"ok": False, "error": "nothing to copy"}
            else:
                board = NSPasteboard.generalPasteboard()
                board.clearContents()
                ok = bool(board.setString_forType_(str(text), NSPasteboardTypeString))
                result = {"ok": ok}
        elif method == "dragRegion":
            bar = getattr(ctx, "drag_bar", None)
            if bar is None:
                result = {"ok": False}
            else:
                rect = bar._apply_region(params.get("rect") or [0, 0, 1120, 94],
                                         params.get("holes") or [])
                result = {"ok": True, "rect": list(rect),
                          "holes": len(params.get("holes") or [])}
        elif method == "reveal":
            util.reveal_in_finder(params.get("path") or "")
            result = {"ok": True}
        elif method == "open":
            util.open_with_default(params.get("path") or "")
            result = {"ok": True}
        elif method == "openUrl":
            # Release notes live on the web; the page never navigates itself
            # away from the app.
            result = {"ok": util.open_url(params.get("url") or "")}
        elif method == "appearance":
            set_appearance(ctx, params.get("mode") or "system")
            result = {"ok": True}
        elif method == "attention":
            try:
                NSApp.requestUserAttention_(0)
            except Exception:
                pass
            result = {"ok": True}
        elif method == "activate":
            NSApp.activateIgnoringOtherApps_(True)
            result = {"ok": True}
        elif method == "quit":
            NSApp.terminate_(None)
            result = {"ok": True}
        else:
            result = {"error": "unknown method"}
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{exc.__class__.__name__}: {exc}"}
    reply(ctx, mid, result)


class ScriptBridge(NSObject):
    """WKScriptMessageHandler that forwards JSON to handle_message()."""

    def initWithContext_(self, ctx):
        self = objc.super(ScriptBridge, self).init()
        if self is None:
            return None
        self.ctx = ctx
        self.panels = []
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            payload = json.loads(message.body())
        except Exception:
            return
        try:
            handle_message(self.ctx, payload)
        except Exception as exc:  # noqa: BLE001
            reply(self.ctx, payload.get("id"), {"error": str(exc)})


# ----------------------------------------------------------------------- menu
def _item(title, action, key="", mask=None):
    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    if mask is not None:
        it.setKeyEquivalentModifierMask_(mask)
    return it


def build_menu(app_name: str) -> None:
    CMD = 1 << 20
    OPT = 1 << 19
    SHIFT = 1 << 17
    main = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    main.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItem_(_item(f"关于 {app_name}", "orderFrontStandardAboutPanel:"))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(_item(f"隐藏 {app_name}", "hide:", "h"))
    app_menu.addItem_(_item("隐藏其他", "hideOtherApplications:", "h", CMD | OPT))
    app_menu.addItem_(_item("显示全部", "unhideAllApplications:"))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(_item(f"退出 {app_name}", "terminate:", "q"))
    app_item.setSubmenu_(app_menu)

    edit_item = NSMenuItem.alloc().init()
    main.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("编辑")
    edit.addItem_(_item("撤销", "undo:", "z"))
    edit.addItem_(_item("重做", "redo:", "Z", CMD | SHIFT))
    edit.addItem_(NSMenuItem.separatorItem())
    edit.addItem_(_item("剪切", "cut:", "x"))
    edit.addItem_(_item("拷贝", "copy:", "c"))
    edit.addItem_(_item("粘贴", "paste:", "v"))
    edit.addItem_(_item("全选", "selectAll:", "a"))
    edit_item.setSubmenu_(edit)

    win_item = NSMenuItem.alloc().init()
    main.addItem_(win_item)
    win_menu = NSMenu.alloc().initWithTitle_("窗口")
    win_menu.addItem_(_item("最小化", "performMiniaturize:", "m"))
    win_menu.addItem_(_item("缩放", "performZoom:"))
    win_menu.addItem_(NSMenuItem.separatorItem())
    win_menu.addItem_(_item("关闭窗口", "performClose:", "w"))
    NSApp.setWindowsMenu_(win_menu)
    win_item.setSubmenu_(win_menu)
    NSApp.setMainMenu_(main)


# --------------------------------------------------------------------- window
# ------------------------------------------------------------------ drop layer
def _board_paths(board):
    """File paths currently on a pasteboard."""
    if board is None:
        return []
    urls = []
    try:
        urls = board.readObjectsForClasses_options_(
            [NSURL], ["NSPasteboardURLReadingFileURLsOnlyKey"]) or []
    except Exception:
        urls = []
    out = []
    for url in urls:
        try:
            path = url.path()
        except Exception:
            continue
        if path and path not in out:
            out.append(path)
    if not out:
        try:
            names = board.propertyListForType_("NSFilenamesPboardType") or []
        except Exception:
            names = []
        out = [n for n in names if isinstance(n, str)]
    return out


def _pasteboard_paths(sender):
    """File paths carried by a drag, via the URL list or the legacy fallback."""
    try:
        return _board_paths(sender.draggingPasteboard())
    except Exception:
        return []


def _dragging_paths():
    """Paths of the drag in flight, read straight off the drag pasteboard.

    Used when WebKit owns the drag session (it always does for a WKWebView):
    the DOM event tells us *when* a drag is happening, this tells us *what*.
    """
    try:
        return _board_paths(NSPasteboard.pasteboardWithName_(NSDragPboard))
    except Exception:
        return []


class DropShim(NSView):
    """Transparent view over the web view that receives Finder drags.

    WKWebView never exposes local file paths to an http:// page, so the DOM
    drop event arrives empty. AppKit still sees the real pasteboard, so drags
    are accepted here and forwarded to the page over the JS bridge.
    """

    def initWithContext_(self, ctx):
        self = objc.super(DropShim, self).init()
        if self is None:
            return None
        self._ctx = ctx
        self._active = False
        self.registerForDraggedTypes_([
            NSPasteboardTypeFileURL,
            "NSFilenamesPboardType",
            "public.file-url",
        ])
        return self

    # Every mouse event falls through to the web view underneath; AppKit still
    # considers this view as a drag destination because that lookup uses the
    # view tree and registered pasteboard types, not -hitTest:.
    def hitTest_(self, point):
        return None

    def wantsPeriodicDraggingUpdates(self):
        return False

    def draggingEntered_(self, sender):
        paths = _pasteboard_paths(sender)
        if not paths:
            return NSDragOperationNone
        self._active = True
        eval_js(self._ctx, "window.__arcaDragEnter && window.__arcaDragEnter(%s)"
                % json.dumps(len(paths)))
        return NSDragOperationCopy

    def draggingUpdated_(self, sender):
        if not self._active:
            return NSDragOperationNone
        return NSDragOperationCopy

    def draggingExited_(self, sender):
        if self._active:
            self._active = False
            eval_js(self._ctx, "window.__arcaDragLeave && window.__arcaDragLeave()")

    # Once we accepted the drag, accept the drop too: a rejected drop would
    # leave the user with nothing at all, since WebKit never saw the session.
    def prepareForDragOperation_(self, sender):
        return True

    def performDragOperation_(self, sender):
        self._active = False
        paths = _pasteboard_paths(sender) or _dragging_paths()
        eval_js(self._ctx, "window.__arcaDragLeave && window.__arcaDragLeave()")
        if not paths:
            return False
        eval_js(self._ctx, "window.__arcaDrop && window.__arcaDrop(%s)"
                % json.dumps(paths, ensure_ascii=False))
        return True


def _titlebar_double_click(window):
    """What double-clicking a title bar does, per System Settings."""
    action = "Zoom"
    try:
        from Foundation import NSUserDefaults
        action = (NSUserDefaults.standardUserDefaults()
                  .stringForKey_("AppleActionOnDoubleClick") or "Zoom")
    except Exception:  # noqa: BLE001
        pass
    try:
        if action == "Minimize":
            window.miniaturize_(None)
        elif action != "None":
            window.zoom_(None)
    except Exception:  # noqa: BLE001
        pass


class DragBar(NSView):
    """Invisible strip that hands window moving back to AppKit.

    The window uses NSWindowStyleMaskFullSizeContentView, so the web view covers
    the title bar and WebKit eats every mouse-down up there: nothing could be
    dragged. This view sits over the page header, takes the mouse-down and asks
    the window for its own drag loop, which keeps edge snapping, the drag
    cursor and the system's double-click preference behaving natively.

    The page reports the header rect and the buttons inside it in CSS pixels,
    which map 1:1 to view points here. Anything reported as a hole falls
    through to the web view, so the header controls keep working.
    """

    def initWithContext_(self, ctx):
        self = objc.super(DragBar, self).init()
        if self is None:
            return None
        self._ctx = ctx
        self._rect = (0.0, 0.0, 1120.0, 32.0)   # window coords, y down
        self._holes = []
        self._probe_calls = []
        # Sitting above the drop shim means this view is the hit target for a
        # drag over the header; without the same types a drop there would be
        # refused, so the session is handed to the shim unchanged.
        self.registerForDraggedTypes_([
            NSPasteboardTypeFileURL,
            "NSFilenamesPboardType",
            "public.file-url",
        ])
        return self

    @objc.python_method
    def _drop(self):
        return getattr(self._ctx, "drop_shim", None)

    def draggingEntered_(self, sender):
        shim = self._drop()
        return shim.draggingEntered_(sender) if shim else NSDragOperationNone

    def draggingUpdated_(self, sender):
        shim = self._drop()
        return shim.draggingUpdated_(sender) if shim else NSDragOperationNone

    def draggingExited_(self, sender):
        shim = self._drop()
        if shim:
            shim.draggingExited_(sender)

    def prepareForDragOperation_(self, sender):
        shim = self._drop()
        return bool(shim.prepareForDragOperation_(sender)) if shim else False

    def performDragOperation_(self, sender):
        shim = self._drop()
        return bool(shim.performDragOperation_(sender)) if shim else False

    # Top-left origin: the numbers the page reports can be compared as-is, and
    # it matches how the page lays the header out.
    def isFlipped(self):
        return True

    # ---------------------------------------------------------------- geometry

    @objc.python_method
    def _size(self):
        sup = self.superview()
        return sup.bounds().size if sup is not None else (0.0, 0.0)

    @objc.python_method
    def _in_frame(self, point):
        f = self.frame()
        return (f.origin.x <= point.x <= f.origin.x + f.size.width and
                f.origin.y <= point.y <= f.origin.y + f.size.height)

    @objc.python_method
    def _from_top(self, point):
        """Superview point -> (x, y) measured from the window's top-left."""
        return float(point.x), float(self._size().height - point.y)

    @objc.python_method
    def _apply_region(self, rect, holes):
        """Place the strip; rect/holes come from the page, y measured from top."""
        w_size = self._size()
        left, top, width, height = [float(v) for v in list(rect)[:4]]
        width = max(0.0, min(width, float(w_size.width) - max(0.0, left)))
        # A zero height disables the strip: the page uses that while a menu or
        # the settings sheet is up, so those stay clickable.
        height = max(0.0, min(height, float(w_size.height)))
        self._rect = (left, top, width, height)
        self._holes = [tuple(float(v) for v in list(h)[:4]) for h in (holes or [])]
        self.setFrame_(NSMakeRect(left, float(w_size.height) - top - height, width, height))
        return self._rect

    def hitTest_(self, point):
        # AppKit hands over superview coordinates and expects us to reject them.
        if not self._in_frame(point):
            return None
        x, y = self._from_top(point)
        for hx, hy, hw, hh in self._holes:
            # A little slack: a 30pt button is an awkward target otherwise.
            if hx - 1 <= x <= hx + hw + 1 and hy - 1 <= y <= hy + hh + 1:
                return None          # a control lives here; the page gets it
        return self

    # ------------------------------------------------------------------ mouse

    def acceptsFirstMouse_(self, event):
        # Same as a real title bar: the first click both activates and drags.
        return True

    def mouseDown_(self, event):
        window = self.window()
        if window is None:
            return
        if not window.isKeyWindow():
            # A click on a background window's title bar brings it forward and
            # starts moving at the same time.
            NSApp.activateIgnoringOtherApps_(True)
        # --probe-window exercises the branches without starting a real grab.
        probe = getattr(self, "_probe_only", False)
        if event.modifierFlags() & NSEventModifierFlagControl:
            self._probe("context")
            if not probe:
                self._context_at(event)      # control-click opens the menu
            return
        if event.clickCount() == 2:
            self._probe("double-click")
            if not probe:
                _titlebar_double_click(window)
            return
        if hasattr(window, "performWindowDragWithEvent_"):
            # Hands the whole gesture to AppKit: it keeps tracking the mouse
            # even when the pointer leaves the window.
            self._probe("performWindowDragWithEvent:")
            if not probe:
                origin = window.frame().origin
                window.performWindowDragWithEvent_(event)
                # If AppKit declined (it only trusts hardware events) the
                # button may still be held down; finish the move by hand.
                if (window.frame().origin == origin
                        and NSEvent.pressedMouseButtons() & 1):
                    self._probe("manual-fallback")
                    self._drag_until_release()
            return
        self._probe("manual")
        if probe:
            return
        self._drag_until_release()

    def rightMouseDown_(self, event):
        self._context_at(event)

    # -------------------------------------------------------------- internals

    @objc.python_method
    def _drag_until_release(self):
        """Move the window by hand until the button comes up.

        Only runs when AppKit's grab loop did not take the gesture, so the
        common path never reaches this. Screen coordinates are used because
        window coordinates shift as the window itself moves.
        """
        window = self.window()
        if window is None:
            return
        origin = window.frame().origin
        base = NSEvent.mouseLocation()
        mask = NSEventMaskLeftMouseDragged | NSEventMaskLeftMouseUp
        while True:
            ev = window.nextEventMatchingMask_(mask)
            if ev is None or ev.type() == NSEventTypeLeftMouseUp:
                return
            loc = NSEvent.mouseLocation()
            window.setFrameOrigin_((origin.x + loc.x - base.x,
                                    origin.y + loc.y - base.y))

    @objc.python_method
    def _probe(self, label):
        self._probe_calls.append(label)

    @objc.python_method
    def _context_at(self, event):
        """Replay the right-click inside the page, where the menu lives."""
        loc = event.locationInWindow()
        x, y = round(loc.x), round(self._size().height - loc.y)
        eval_js(self._ctx, """(function(){
  var el = document.elementFromPoint(%d, %d) || document.body;
  el.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
    clientX: %d, clientY: %d, button: 2, buttons: 2}));
})()""" % (x, y, x, y))


def _drag_regions(ctx, web) -> None:
    """Ask the page where its header is, so the drag strip matches the layout."""
    raw = _js_value(web, """(function(){
      var header = document.querySelector('.topbar');
      if (!header) return '';
      var band = document.querySelector('.menu, .sheet-back')
        ? 0 : Math.round(header.getBoundingClientRect().bottom);
      var marks = document.querySelectorAll(
        '.brand, .top-actions, button, input, a, [data-no-drag]');
      var holes = [];
      for (var i = 0; i < marks.length; i++) {
        var b = marks[i].getBoundingClientRect();
        if (b.width > 0 && b.height > 0 && b.top < band && b.bottom > 0) {
          holes.push([Math.round(b.left), Math.round(b.top),
                      Math.round(b.width), Math.round(b.height)]);
        }
      }
      return JSON.stringify({
        rect: [0, 0, Math.round(window.innerWidth), band],
        holes: holes,
      });
    })()""", 1.5)
    bar = getattr(ctx, "drag_bar", None)
    if not raw or bar is None:
        return
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    rect = data.get("rect") or [0, 0, 1120, 94]
    bar._apply_region(rect, data.get("holes") or [])


def _js_value(view, js: str, timeout: float = 3.0):
    """Synchronous evaluateJavaScript for diagnostics."""
    box = {}
    done = threading.Event()

    def callback(result, error):
        box["result"] = result
        box["error"] = error
        done.set()

    view.evaluateJavaScript_completionHandler_(js, callback)
    if not done.wait(timeout):
        return None
    return box.get("result")


class FakeDraggingInfo(NSObject):
    """Minimal NSDraggingInfo stand-in, so the drop path can be exercised."""

    def initWithPasteboard_(self, board):
        self = objc.super(FakeDraggingInfo, self).init()
        if self is None:
            return None
        self._board = board
        return self

    def draggingPasteboard(self):
        return self._board

    def draggingLocation(self):
        return NSMakePoint(560, 380)

    def draggingSourceOperationMask(self):
        return NSDragOperationCopy


def _drop_selftest(ctx, web) -> dict:
    """Feed a real pasteboard through the shim and see what the page shows."""
    import tempfile

    shim = getattr(ctx, "drop_shim", None)
    if shim is None:
        return {"ok": False, "error": "no drop shim on window"}

    root = tempfile.mkdtemp(prefix="arca-drop-")
    a = os.path.join(root, "拖放测试-A.txt")
    b = os.path.join(root, "拖放测试-B.bin")
    with open(a, "w") as fh:
        fh.write("alpha")
    with open(b, "wb") as fh:
        fh.write(b"\x00\x01\x02" * 512)

    board = NSPasteboard.pasteboardWithUniqueName()
    board.clearContents()
    wrote = board.writeObjects_([NSURL.fileURLWithPath_(a), NSURL.fileURLWithPath_(b)])
    fake = FakeDraggingInfo.alloc().initWithPasteboard_(board)

    entered = int(shim.draggingEntered_(fake))
    overlay_on = _js_value(web, "document.getElementById('drag-overlay')"
                                 ".classList.contains('on')")
    accepted = bool(shim.prepareForDragOperation_(fake))
    dropped = bool(shim.performDragOperation_(fake))
    overlay_off = _js_value(web, "document.getElementById('drag-overlay')"
                                  ".classList.contains('on')")

    rows = 0
    for _ in range(20):
        time.sleep(0.15)
        rows = _js_value(web, "document.querySelectorAll('#c-list .file-row').length") or 0
        if rows >= 2:
            break
    names = _js_value(web, "Array.from(document.querySelectorAll('#c-list .file-row b'))"
                           ".map(n => n.textContent).join('|')")

    return {
        "pasteboard": os.path.basename(board.name()),
        "wrote": bool(wrote),
        "dragOperation": entered,
        "accepted": accepted,
        "dropped": dropped,
        "overlayOnDuringDrag": bool(overlay_on),
        "overlayOffAfterDrop": not bool(overlay_off),
        "rowsInPage": rows,
        "names": names,
        "dispatch": _drag_dispatch_probe(ctx),
        "bridge": _drag_bridge_probe(web, [a, b]),
    }


def _drag_bridge_probe(web, paths) -> dict:
    """Put files on the drag pasteboard and read them back through the bridge."""
    board = NSPasteboard.pasteboardWithName_(NSDragPboard)
    board.clearContents()
    board.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths])
    direct = _dragging_paths()

    _js_value(web, """
    window.__probeResult = null;
    (function () {
      const id = 'probe-drag';
      const prev = window.__arcaReply;
      window.__arcaReply = function (mid, result) {
        if (String(mid) === id) {
          window.__arcaReply = prev;
          window.__probeResult = JSON.stringify(result || {});
        } else if (prev) { prev(mid, result); }
      };
      window.webkit.messageHandlers.arca.postMessage(
        JSON.stringify({ id: id, method: 'dragPaths', params: {} }));
    })();
    """)

    bridged = None
    for _ in range(30):
        time.sleep(0.1)
        raw = _js_value(web, "window.__probeResult")
        if raw:
            try:
                bridged = json.loads(raw).get("paths")
            except Exception as exc:
                bridged = "bad payload: %r" % (exc,)
            break
    return {"pasteboardRead": direct, "bridge": bridged}


def _drag_dispatch_probe(ctx) -> dict:
    """Ask AppKit which view would receive a drag at the window centre.

    -_hitTest_dragTypes: is what the drag machinery itself calls, so this tells
    us whether the shim is really in the chain rather than merely registered.
    """
    out = {}
    point = NSMakePoint(560, 300)
    try:
        content = ctx.window.contentView()
    except Exception as exc:  # pragma: no cover - diagnostics only
        return {"error": repr(exc)}

    try:
        out["hitTest"] = type(content.hitTest_(point)).__name__
    except Exception as exc:
        out["hitTest"] = "%s: %s" % (type(exc).__name__, exc)

    fn = getattr(content, "_hitTest_dragTypes_", None)
    if fn is None:
        out["dragTypes"] = "selector missing"
        return out
    attempts = []
    for args in ((point, [NSPasteboardTypeFileURL]), (point,)):
        try:
            view = fn(*args)
        except Exception as exc:
            attempts.append({"arity": len(args), "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        attempts.append({"arity": len(args),
                         "view": type(view).__name__ if view is not None else None})
        break
    out["dragTypes"] = attempts
    return out


def _menu_probe(web) -> dict:
    """Render the context menu inside WKWebView and read the result back.

    Chrome proves the logic; this proves the same code survives WebKit's
    engine, its font metrics and its <use> symbol resolution.
    """
    _js_value(web, """
    (function () {
      if (window.ArcaMenu) window.ArcaMenu.close();
      window.__menuRead = function () {
        const el = document.querySelector('.menu');
        if (!el) return null;
        const rr = el.getBoundingClientRect();
        const svg = el.querySelector('.menu-ic svg');
        let iconBox = null;
        try { const b = svg.getBBox(); iconBox = [Math.round(b.width), Math.round(b.height)]; }
        catch (e) { iconBox = 'error: ' + e.message; }
        const item = el.querySelector('.menu-item');
        return JSON.stringify({
          open: true,
          rect: [Math.round(rr.x), Math.round(rr.y), Math.round(rr.width), Math.round(rr.height)],
          items: el.querySelectorAll('.menu-item').length,
          labels: Array.prototype.map.call(
            el.querySelectorAll('.menu-label'), function (n) { return n.textContent; }),
          theme: document.documentElement.dataset.theme,
          bg: getComputedStyle(el).backgroundColor,
          fg: getComputedStyle(item).color,
          iconBox: iconBox,
          font: getComputedStyle(item).fontFamily,
          radius: getComputedStyle(el).borderTopLeftRadius,
        });
      };
      const host = document.querySelector('#c-files:not(.hidden)')
                || document.querySelector('#drop-c')
                || document.querySelector('#view-compress');
      const r = host.getBoundingClientRect();
      window.__menuAt = [Math.round(r.x + r.width * 0.5),
                         Math.round(r.y + Math.min(40, r.height * 0.3))];
      const hit = document.elementFromPoint(window.__menuAt[0], window.__menuAt[1]) || host;
      const ev = new MouseEvent('contextmenu', {
        bubbles: true, cancelable: true,
        clientX: window.__menuAt[0], clientY: window.__menuAt[1], button: 2 });
      hit.dispatchEvent(ev);
      window.__menuHit = hit.id || hit.className || hit.tagName;
      window.__menuPrevented = ev.defaultPrevented;
      window.__menuReadAt = window.__menuAt;
    })();
    """)

    raw = None
    for _ in range(6):
        time.sleep(0.06)
        raw = _js_value(web, "window.__menuRead && window.__menuRead()", 1.0)
        if raw:
            break
    # Round-trip through a JSON string: PyObjC hands back NSArray/NSString,
    # which json.dumps would choke on when the report is printed.
    meta_raw = _js_value(web, """JSON.stringify({
        prevented: !!window.__menuPrevented,
        hit: window.__menuHit || '',
        at: window.__menuReadAt || null,
      })""", 1.5)
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:  # noqa: BLE001
        meta = {"raw": str(meta_raw)}
    _js_value(web, "window.ArcaMenu && window.ArcaMenu.close()")
    try:
        out = json.loads(raw) if raw else {"open": False}
    except Exception as exc:  # noqa: BLE001
        out = {"open": False, "error": "%s: %r" % (exc.__class__.__name__, raw)}
    out.update(meta)
    return out


def _drag_probe(ctx, web=None) -> dict:
    """Check the drag strip the way AppKit would: hit-test real points."""
    bar = getattr(ctx, "drag_bar", None)
    window = getattr(ctx, "window", None)
    if bar is None or window is None:
        return {"present": False}
    if web is not None:
        # Pull the geometry instead of relying on the last page message: the
        # menu probe above just opened and closed an overlay.
        _drag_regions(ctx, web)
    cont = window.contentView()
    height = cont.bounds().size.height
    left, top, width, bar_h = bar._rect
    frame = bar.frame()

    def hit(x, y_from_top):
        # Same call AppKit makes: superview coordinates, origin bottom-left.
        return bar.hitTest_(NSMakePoint(x, height - y_from_top))

    mid_x = left + width * 0.5
    out = {
        "present": True,
        "frame": [round(frame.origin.x), round(frame.origin.y),
                  round(frame.size.width), round(frame.size.height)],
        "rect": [round(left), round(top), round(width), round(bar_h)],
        "holes": [[round(v) for v in hole] for hole in bar._holes],
        "headerCenterHits": hit(mid_x, top + bar_h * 0.5) is bar,
        "belowHits": hit(mid_x, top + bar_h + 24) is None,
        "belowLeftHits": hit(60.0, top + bar_h + 24) is None,
        "bandHits": hit(60.0, top + 4.0) is bar,
        "topmost": bool(cont.subviews()) and cont.subviews()[-1].isEqual_(bar),
        "api": ("performWindowDragWithEvent:"
                if hasattr(window, "performWindowDragWithEvent_") else "manual"),
    }
    # A hole that does not let the mouse through is a dead button.
    blocked = [[round(hx), round(hy)] for hx, hy, hw, hh in bar._holes
               if hit(hx + hw / 2.0, hy + hh / 2.0) is not None]
    out["holesPassThrough"] = not blocked
    out["blockedHoles"] = blocked

    # Walk the mouse-down branches with synthetic events; nothing is dragged.
    bar._probe_only = True
    bar._probe_calls = []
    try:
        for clicks, label in ((1, "single"), (1, "control"), (2, "double")):
            flags = NSEventModifierFlagControl if label == "control" else 0
            ev = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                NSEventTypeLeftMouseDown, NSMakePoint(mid_x, height - top - 8),
                flags, 0.0, window.windowNumber(), None, 1, clicks, 1.0)
            if ev is None:
                continue
            bar.mouseDown_(ev)
    finally:
        bar._probe_only = False
    out["branches"] = bar._probe_calls

    # With a menu or the settings sheet up, the strip must step aside or it
    # would eat the clicks meant for them.
    if web is not None:
        band = bar._rect[3]
        _js_value(web, "window.ArcaMenu && window.ArcaMenu.open("
                       "{x: 320, y: 260, items: [{label: 'probe'}]})")

        def settle(want_zero, seconds=2.0):
            """The page reports over the bridge, so give the round-trip time.

            A fixed sleep passed on a warm start and failed on the first launch
            from a freshly mounted image, which is exactly how a user meets it.
            """
            deadline = time.time() + seconds
            while time.time() < deadline:
                if (bar._rect[3] == 0) == want_zero:
                    break
                time.sleep(0.05)
            return bar._rect[3]

        with_menu = settle(True)
        _js_value(web, "window.ArcaMenu && window.ArcaMenu.close()")
        after = settle(False)
        out["overlayCollapse"] = {
            "band": round(band), "withMenu": round(with_menu), "after": round(after),
            "ok": band > 0 and with_menu == 0 and after == band,
        }
    return out


def _synthetic_drag(ctx) -> dict:
    """Show where a click on the header actually lands.

    Posting events cannot move a window: AppKit's grab loop only trusts a
    hardware mouse-down, so what this proves is the part we own — the click
    reaches the strip and takes the drag branch. The move itself needs a hand
    on the trackpad.
    """
    window = ctx.window
    bar = ctx.drag_bar
    left, top, width, height = bar._rect
    inside = NSMakePoint(left + width * 0.55, window.frame().size.height - (top + 40))
    number = window.windowNumber()
    step, before = 46.0, window.frame().origin
    seen = len(bar._probe_calls)

    def make(kind, point, clicks=1):
        return NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            kind, point, 0, 0.0, number, None, 1, clicks, 1.0)

    NSApp.postEvent_atStart_(make(NSEventTypeLeftMouseDown, inside), False)
    for i in (1, 2):
        NSApp.postEvent_atStart_(make(
            NSEventTypeLeftMouseDragged,
            NSMakePoint(inside.x + step * i, inside.y - step * i)), False)
    NSApp.postEvent_atStart_(make(
        NSEventTypeLeftMouseUp,
        NSMakePoint(inside.x + step * 2, inside.y - step * 2)), False)

    time.sleep(1.0)
    after = window.frame().origin
    return {
        "clickAt": [round(inside.x), round(inside.y)],
        "height": round(height),
        "shimEvents": bar._probe_calls[seen:],
        "moved": [round(after.x - before.x), round(after.y - before.y)],
    }


def _drag_give_up():
    print(json.dumps({"dragMove": {"status": "timeout"}}), flush=True)
    os._exit(0)


def make_window(ctx, url: str):
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
             NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable |
             NSWindowStyleMaskFullSizeContentView)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 1120, 764), style, NSBackingStoreBuffered, False)
    window.setTitle_("Arca")
    window.setTitlebarAppearsTransparent_(True)
    window.setTitleVisibility_(NSWindowTitleHidden)
    window.setMinSize_(NSMakeSize(940, 640))
    window.setReleasedWhenClosed_(False)
    window.setMovableByWindowBackground_(False)
    window.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(0.96, 1.0))

    config = WKWebViewConfiguration.alloc().init()
    controller = WKUserContentController.alloc().init()
    bridge = ScriptBridge.alloc().initWithContext_(ctx)
    controller.addScriptMessageHandler_name_(bridge, "arca")
    boot = {
        "token": ctx.token,
        "argv": getattr(ctx, "argv_files", []),
        "native": True,
        "version": util.APP_VERSION,
    }
    script = WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
        "window.__ARCA_BOOT = %s;" % json.dumps(boot, ensure_ascii=False),
        WKUserScriptInjectionTimeAtDocumentStart, True)
    controller.addUserScript_(script)
    config.setUserContentController_(controller)
    ctx.bridge = bridge

    web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 1120, 764), config)
    web.setAutoresizingMask_(18)

    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1120, 764))
    container.setAutoresizingMask_(18)
    container.addSubview_(web)

    shim = DropShim.alloc().initWithContext_(ctx)
    shim.setFrame_(container.bounds())
    shim.setAutoresizingMask_(18)
    container.addSubview_(shim)

    # Above the web view: the first strip the mouse can actually move the
    # window by. The page refines its size once it knows where its header is.
    drag_bar = DragBar.alloc().initWithContext_(ctx)
    drag_bar.setFrame_(NSMakeRect(0, 764 - 32, 1120, 32))
    drag_bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    container.addSubview_(drag_bar)

    window.setContentView_(container)
    window.center()
    window.makeKeyAndOrderFront_(None)
    NSApp.activateIgnoringOtherApps_(True)
    ctx.window = window
    ctx.web = web
    ctx.drop_shim = shim
    ctx.drag_bar = drag_bar
    web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    def fit_drag_bar():
        # The header rect is only knowable once layout has run; doing it here
        # means dragging works even before the page talks to the bridge.
        # _drag_regions evaluates JS, and WebKit kills the process outright
        # (EXC_BREAKPOINT / SIGKILL) when its main-thread-only API is called
        # off the main thread -- so hop over for every probe.
        for delay in (0.6, 1.6, 3.0):
            time.sleep(delay)
            try:
                _run_on_main(lambda: _drag_regions(ctx, web))
            except Exception:  # noqa: BLE001
                pass
            if drag_bar._holes:
                break

    threading.Thread(target=fit_drag_bar, daemon=True).start()
    return window


def deliver(ctx, action: str, paths) -> dict | None:
    """Queue a Finder request and pull the window forward.

    The queue is the only safe hand-off: a cold start reaches this code before
    the web view has finished loading, so evaluating JS here would be a no-op.
    """
    item = srv.push_inbox(ctx, action, paths)
    if item is None:
        return None
    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass
    eval_js(ctx, "window.arcaInbox && window.arcaInbox();")
    return item


class _MainCall(NSObject):
    """One zero-arg callable executed on the main runloop.

    ``NSPerformService`` (used by the service probe) must be called from the
    main thread: it spins an inner runloop while the request travels through
    the services daemon back into this very app, and the handler is
    dispatched by the main runloop -- from a worker thread the delivery can
    never arrive and the call never returns.

    Note the helper method is deliberately *not* named ``run``: pyobjc
    validates selectors against known metadata and NSObject subclasses with
    a ``run`` method trip ``BadPrototypeError`` (``run`` expects 0 args).
    """

    def init(self):
        # objc.super, not builtin super(): pyobjc rejects the latter with
        # ObjCSuperWarning (NSObject's ivar layout is not a plain Python MRO).
        self = objc.super(_MainCall, self).init()
        self.fn = None
        self.done = threading.Event()
        self.result = None
        self.error = None
        return self

    def call_(self, _arg):
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        finally:
            self.done.set()


def _run_on_main(fn, timeout=60.0):
    """Execute fn() on the main thread and return its result."""
    if threading.current_thread() is threading.main_thread():
        return fn()
    helper = _MainCall.alloc().init()
    helper.fn = fn
    helper.performSelectorOnMainThread_withObject_waitUntilDone_(
        "call:", None, False)
    if not helper.done.wait(timeout):
        raise TimeoutError("main thread did not run the probe step")
    if helper.error is not None:
        raise helper.error
    return helper.result


def _probe_services(ctx) -> dict:
    """Drive both Finder services through the real Services machinery.

    NSPerformService() resolves a service by its menu title in the system
    database and delivers the same Apple event Finder would, so a missing
    NSServices entry or a wrong selector signature fails here instead of only on
    a user's machine.  The page also polls /api/inbox, so the probe watches
    ctx.delivered rather than draining the queue.
    """
    import ctypes
    import zipfile

    appkit = ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    appkit.NSPerformService.restype = ctypes.c_bool
    appkit.NSPerformService.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def _ptr(obj):
        """Raw id of a bridged object.

        ``objc.pyobjc_id`` rejects the ``pyobjc_unicode`` returned by
        ``NSString.stringWithString_`` ("not an Objective-C object"), while
        ``__c_void_p__()`` works for every bridged type.
        """
        return obj.__c_void_p__().value

    tmp = tempfile.mkdtemp(prefix="arca-service-probe-")
    txt = os.path.join(tmp, "探测.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("arca probe")
    zip_path = os.path.join(tmp, "探测.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.txt", "hello")

    time.sleep(2.0)          # let AppKit finish launching before talking to pbs
    results = []
    for title, selection, expect in (("用 Arca 压缩", [txt], "compress"),
                                     ("用 Arca 解压", [zip_path], "extract")):
        before = len(ctx.delivered)

        def _deliver(title=title, selection=selection):
            pb = NSPasteboard.pasteboardWithName_(
                "arca.probe.%d" % int(time.time() * 1000))
            pb.clearContents()
            pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in selection])
            return bool(appkit.NSPerformService(
                _ptr(NSString.stringWithString_(title)), _ptr(pb)))

        posted = _run_on_main(_deliver)
        got = None
        for _ in range(60):
            if len(ctx.delivered) > before:
                got = ctx.delivered[-1]
                break
            time.sleep(0.1)
        results.append({
            "service": title,
            "posted": posted,
            "action": (got or {}).get("action"),
            "expect": expect,
            "paths": [os.path.basename(p) for p in ((got or {}).get("paths") or [])],
        })
    return {"tmp": tmp, "results": results}


class ArcaDelegate(NSObject):
    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationShouldTerminate_(self, app):
        """Closing the last window / Cmd-Q: stop active work before quitting.

        Children are terminated and reaped (bounded), queued jobs are marked
        cancelled, and the pending journal is cleaned by each worker's own
        finally block.  Whatever cannot be stopped in 8s is reported instead
        of being claimed as finished.
        """
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            try:
                result = ctx.manager.shutdown(timeout=8.0)
                if not result["ok"]:
                    print("[arca] 退出时仍有未收尾任务: "
                          + json.dumps(result["unfinished"], ensure_ascii=False),
                          file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - never block quitting
                print(f"[arca] 退出收尾失败: {exc!r}", file=sys.stderr)
        return NSTerminateNow

    # ------------------------------------------------- Finder 右键 → 服务
    # The Services machinery sends `selector:userData:error:` with an
    # `NSString **` out parameter.  pyobjc infers `@@:@@@` for a plain method,
    # which would hand us the pointer-to-pointer as if it were an object, so the
    # signature is written out.  Returning None means "no error to report".
    @objc.typedSelector(b"v@:@@^@")
    def compressSelection_userData_error_(self, pboard, userData, error):
        return (None, self._service("compress", pboard))

    @objc.typedSelector(b"v@:@@^@")
    def extractSelection_userData_error_(self, pboard, userData, error):
        return (None, self._service("extract", pboard))

    @objc.python_method
    def _service(self, action, pboard):
        try:
            paths = integration.pasteboard_paths(pboard)
        except Exception as exc:  # noqa: BLE001
            return f"无法读取 Finder 的选择：{exc}"
        if not paths:
            return "Finder 里没有选中任何文件"
        if action == "extract":
            # The menu item is always offered; only complain when it cannot work.
            arch = [p for p in paths if integration.is_archive(p)]
            if not arch:
                return "选中的项目里没有压缩包"
            paths = arch
        deliver(self.ctx, action, paths)
        return None

    # ------------------------------------------ 打开方式 / 双击 / 拖到图标
    def _pending_action(self) -> str:
        """Action hint left by a Quick Action workflow, or "auto".

        The workflow writes "compress"/"extract" right before `open -a Arca`,
        because double-click and drag cannot express intent.  The flag expires
        after 10 s so a stale hint never rewrites an unrelated launch.
        """
        path = os.path.join(util.app_support_dir(), "next-action")
        try:
            with open(path, encoding="utf-8") as fh:
                action, ts = (fh.read().split() + ["", "0"])[:2]
            os.remove(path)
            if action in ("compress", "extract") and time.time() - float(ts) < 10:
                return action
        except (OSError, ValueError):
            pass
        return "auto"

    def application_openFiles_(self, app, files):
        paths = []
        for item in (files or []):
            try:
                paths.append(str(item.path()))
            except Exception:  # noqa: BLE001
                paths.append(str(item))
        deliver(self.ctx, self._pending_action(), paths)

    def application_openFile_(self, app, filename):
        deliver(self.ctx, self._pending_action(), [str(filename)])

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
        # Clicking the dock icon should bring the window back, nothing else.
        win = getattr(self.ctx, "window", None)
        if win is not None and not win.isVisible():
            win.makeKeyAndOrderFront_(None)
        return True


# ----------------------------------------------------------------------- main
def run(argv):
    argv = list(argv)
    probe_mode = "--probe-service" in argv or os.path.isfile(
        os.path.join(util.app_support_dir(), "svc-probe.json"))
    if probe_mode:
        # `open --args` argument forwarding is flaky to debug blind: log the
        # launch argv where the driving shell can read it, so probe failures
        # can be told apart from "args never arrived".
        try:
            with open(os.path.join(util.app_support_dir(), "launch.log"), "a",
                      encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ")
                         + json.dumps(argv, ensure_ascii=False) + "\n")
        except OSError:
            pass
    headless = "--headless" in argv
    port_file = None
    if "--port-file" in argv:
        try:
            port_file = argv[argv.index("--port-file") + 1]
        except IndexError:
            port_file = None

    # 单实例：共享状态（history/settings/components/journal/components 缓存）
    # 只有一个写入者。锁必须在 Settings/JobManager 初始化之前拿到。
    locked, holder = util.acquire_instance_lock()
    if not locked:
        detail = ("当前实例：" + holder) if holder else "无法读取现有实例信息"
        if headless:
            print("另一个 Arca 实例正在使用该用户目录，headless 启动已拒绝（"
                  + detail + "）；如需并行测试请用 ARCA_HOME/HOME 隔离。",
                  file=sys.stderr)
            raise SystemExit(3)
        message = ("Arca 已在运行。同一用户目录只允许一个实例，避免任务历史与"
                   "设置互相覆盖。\n\n" + detail
                   + "\n\n请切换到已打开的窗口；如果要使用独立的数据目录，"
                     "请设置环境变量 ARCA_HOME。")
        try:
            import AppKit
            AppKit.NSApplication.sharedApplication()
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Arca 已在运行")
            alert.setInformativeText_(message)
            alert.runModal()
        except Exception:
            print(message, file=sys.stderr)
        raise SystemExit(3)

    settings = Settings()
    try:
        engine = eng.Engine(settings.get("rarPath") or None)
    except eng.EngineError as exc:
        print("引擎初始化失败:", exc, file=sys.stderr)
        raise SystemExit(2)

    files = [a for a in argv[1:] if os.path.exists(a) and not a.startswith("-")]
    ctx = srv.AppContext(engine, settings, _web_dir())
    ctx.argv_files = files
    ctx.window = None
    ctx.headless = headless
    server = srv.Server(ctx)
    server.start()
    stats.maybe_ping(settings, headless)
    url = f"http://127.0.0.1:{server.port}/index.html?t={ctx.token}"

    info = {"port": server.port, "token": ctx.token}
    if port_file:
        try:
            with open(port_file, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(info))
        except OSError:
            pass

    if headless:
        print(json.dumps(info), flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            time.sleep(0.3)
        # Headless has no window/terminate notification: SIGTERM/SIGINT must
        # run the same shutdown as the UI quit path.
        try:
            result = ctx.manager.shutdown(timeout=10.0)
            if not result["ok"]:
                print("[arca] 退出时仍有未收尾任务: "
                      + json.dumps(result["unfinished"], ensure_ascii=False),
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[arca] 退出收尾失败: {exc!r}", file=sys.stderr)
        return

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = ArcaDelegate.alloc().init()
    # Set before run(): Finder can hand us a file (double click, service, drag on
    # the dock icon) while the window is still being built.
    delegate.ctx = ctx
    ctx.delegate = delegate
    app.setDelegate_(delegate)
    # Without an explicit provider AppKit never opens the Services port, and
    # every request dies with "Application … never opened its Services port
    # before the timeout" even though Info.plist declares NSServices.
    app.setServicesProvider_(delegate)
    app.setApplicationIconImage_(_load_icon())

    build_menu("Arca")
    make_window(ctx, url)
    appearance = settings.get("appearance") or "system"
    set_appearance(ctx, appearance)

    # Service probe trigger.  `open --args` never forwarded argv to us
    # (launch.log proved it), and the Services port only opens when the app is
    # launched through LaunchServices -- so the canonical trigger is a marker
    # file the test writes before `open -n -a Arca.app`; the command-line form
    # stays as a fallback for direct runs.
    report_path = ""
    marker = os.path.join(util.app_support_dir(), "svc-probe.json")
    if "--probe-service" in argv:
        idx = argv.index("--probe-service")
        nxt = argv[idx + 1] if len(argv) > idx + 1 else ""
        report_path = "" if nxt.startswith("-") else nxt
    elif os.path.isfile(marker):
        try:
            report_path = json.load(open(marker, encoding="utf-8")).get("report", "")
        except (OSError, ValueError):
            report_path = ""
        os.remove(marker)

    if report_path or "--probe-service" in argv:
        def service_waiter():
            try:
                report = _probe_services(ctx)
            except Exception as exc:  # noqa: BLE001
                report = {"error": repr(exc)}
            report["argv"] = argv
            blob = json.dumps(report, ensure_ascii=False)
            # stdout of a LaunchServices launch is not ours -- always drop a
            # copy at a fixed path so the driving shell can find the report.
            for path in (report_path, "/tmp/arca-svc.json"):
                if not path:
                    continue
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(blob)
                except OSError:
                    pass
            print(blob, flush=True)
            os._exit(0)

        threading.Thread(target=service_waiter, daemon=True).start()

    if "--probe-window" in argv:
        # Machine-checkable proof that the native shell really came up: report
        # window geometry + what the WKWebView loaded, then exit.
        def waiter():
            web = getattr(ctx, "web", None)
            # Wait until the page is actually interactive: a fixed sleep makes
            # the probe report an empty title and zero rows on a cold start.
            for _ in range(40):
                time.sleep(0.2)
                if web is not None and _run_on_main(lambda: _js_value(
                        web, "!!(window.ArcaMenu && document.getElementById('c-list'))", 0.6)):
                    break
            time.sleep(0.4)

            def _build():
                frame = ctx.window.frame()
                report = {
                    "visible": bool(ctx.window.isVisible()),
                    "key": bool(ctx.window.isKeyWindow()),
                    "width": round(frame.size.width),
                    "height": round(frame.size.height),
                    "title": str(web.title() if web is not None else ""),
                    "url": str(web.URL().absoluteString() if web is not None else ""),
                    "appearance": str(ctx.window.effectiveAppearance().name()),
                    "dropShim": type(ctx.drop_shim).__name__ if getattr(ctx, "drop_shim", None) else None,
                    "dragShim": type(ctx.drag_bar).__name__ if getattr(ctx, "drag_bar", None) else None,
                }
                if web is not None:
                    report["drop"] = _drop_selftest(ctx, web)
                    report["menu"] = _menu_probe(web)
                    report["drag"] = _drag_probe(ctx, web)
                    report["clipboard"] = {
                        "files": len(_board_paths(NSPasteboard.generalPasteboard())),
                    }
                return report

            try:
                # Every WebKit call below is main-thread-only; WebKit kills
                # the process on a background-thread violation.
                report = _run_on_main(_build)
            except Exception as exc:  # pragma: no cover - diagnostics only
                report = {"error": repr(exc)}
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if "--probe-drag" in argv:
                # Second line: proof the strip really moves the window. The
                # watchdog keeps a stuck grab loop from hanging the run.
                threading.Timer(9.0, _drag_give_up).start()
                try:
                    moved = _synthetic_drag(ctx)
                except Exception as exc:  # noqa: BLE001
                    moved = {"error": repr(exc)}
                print(json.dumps({"dragMove": moved}, ensure_ascii=False), flush=True)
            os._exit(0)

        threading.Thread(target=waiter, daemon=True).start()

    app.run()


def _load_icon():
    web = _web_dir()
    for cand in (os.path.join(web, "..", "..", "..", "Arca.icns"),   # bundle Resources
                 os.path.join(web, "..", "..", "assets", "Arca.icns"),  # dev
                 os.path.join(getattr(sys, "_MEIPASS", "") or "", "assets", "Arca.icns")):
        try:
            if cand and os.path.isfile(cand):
                img = NSImage.alloc().initWithContentsOfFile_(cand)
                if img:
                    return img
        except Exception:
            continue
    return None
