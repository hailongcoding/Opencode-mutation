#!/usr/bin/env python3
"""
Computer-use MCP server for the OpenCode launcher in this folder.

This process is only ever started by OpenCode when
`scripts/Start-ComputerUse.ps1` injected it into a single, private session.
It refuses to run without a session token, so it cannot be used as a
permanent, global capability.

Transport: MCP over stdio (never print to stdout; logs go to stderr).
"""

from __future__ import annotations

import atexit
import ctypes
import functools
import io
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime

SESSION_ENV = "COMPUTER_USE_SESSION_TOKEN"
PARENT_ENV = "COMPUTER_USE_PARENT_PID"
READ_ONLY_ENV = "COMPUTER_USE_READ_ONLY"

if not os.environ.get(SESSION_ENV):
    sys.stderr.write(
        "[computer-use] Refusing to start: no active launcher session.\n"
        "[computer-use] Start 'Computer-Use.cmd' in the launcher folder instead.\n"
    )
    raise SystemExit(2)

IS_WINDOWS = platform.system() == "Windows"


def _process_alive(pid: int) -> bool:
    """Whether a process is still running, without needing psutil."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parent_alive() -> bool:
    """A launcher-owned session dies with the launcher, even if a service outlives it."""
    raw = os.environ.get(PARENT_ENV)
    if not raw:
        return True
    try:
        return _process_alive(int(raw))
    except ValueError:
        return True


if not _parent_alive():
    sys.stderr.write(
        "[computer-use] Refusing to start: the launcher process is no longer running.\n"
        "[computer-use] Start 'Computer-Use.cmd' again for a new session.\n"
    )
    raise SystemExit(2)

# --------------------------------------------------------------------------
# Platform setup
# --------------------------------------------------------------------------

if IS_WINDOWS:
    try:
        # Physical pixels everywhere: screenshots, clicks and UIA rectangles match.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import pyautogui  # noqa: E402  (must come after DPI awareness)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

from PIL import Image as PILImage  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp import Image as MCPImage  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Last screenshot mapping, used to translate model coordinates -> screen pixels.
_shot: dict | None = None

_AUDIT_PATH = os.path.join(ROOT, ".runtime", "logs", "actions.log")
_AUDIT_LOCK = threading.Lock()
_PAUSED = False


def _read_only() -> bool:
    """Whether this process was launched as inspection-only."""
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# These tools cannot change the desktop (clipboard still checks its action).
_READ_ONLY_TOOLS = frozenset(
    {
        "screenshot", "screen_info", "ui_tree", "snapshot", "find_elements",
        "read_element", "list_windows", "wait_for_window", "wait_for_element",
        "wait", "clipboard", "runtime_status", "get_element_state", "read_table",
        "find_text", "wait_for_idle", "wait_for_change", "highlight_element",
        "release_all", "pause",
    }
)


def _read_only_block() -> str:
    return (
        "BLOCKED: this Computer-Use session is read-only. Inspection tools "
        "(snapshot, ui_tree, screenshot, find_elements, read_element, "
        "get_element_state, read_table, find_text, list_windows, waits, "
        "runtime_status, clipboard get) are available, but every mouse, "
        "keyboard, window and launch action is disabled."
    )


_PAUSED_ALLOWED = _READ_ONLY_TOOLS | {"pause", "release_all"}


def _paused_block() -> str:
    return (
        "PAUSED: automation is paused. Press Ctrl+Alt+F12 or call "
        "pause(action=\"off\") to resume."
    )


def _audit_args(kwargs: dict) -> dict:
    """Keep audit records useful without writing typed secrets to disk."""
    clean: dict = {}
    for key, value in kwargs.items():
        if key in {"text", "password", "value"}:
            clean[key] = f"<{len(str(value))} chars>"
        elif value is None or isinstance(value, (str, int, float, bool)):
            text = str(value)
            clean[key] = text[:160] + ("..." if len(text) > 160 else "")
        else:
            clean[key] = repr(value)[:160]
    return clean


def _audit(tool: str, args: dict, outcome: str, elapsed_ms: int) -> None:
    """Append one JSON line to .runtime/logs/actions.log (best effort)."""
    try:
        os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
        with _AUDIT_LOCK:
            try:
                if os.path.getsize(_AUDIT_PATH) > 5 * 1024 * 1024:
                    backup = _AUDIT_PATH + ".1"
                    if os.path.exists(backup):
                        os.remove(backup)
                    os.replace(_AUDIT_PATH, backup)
            except OSError:
                pass
            record = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "tool": tool,
                "args": args,
                "outcome": outcome,
                "ms": elapsed_ms,
                "read_only": _read_only(),
            }
            with open(_AUDIT_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        tool = fn.__name__
        started = time.monotonic()
        if not _parent_alive():
            _release_held()
            result = (
                "DETACHED: the Computer-Use launcher that created this session is no longer running. "
                "Start Computer-Use.cmd again for a new session."
            )
            _audit(tool, _audit_args(kwargs), "detached", int((time.monotonic() - started) * 1000))
            return result
        if _PAUSED and tool not in _PAUSED_ALLOWED:
            result = _paused_block()
            _audit(tool, _audit_args(kwargs), "paused", int((time.monotonic() - started) * 1000))
            return result
        if _read_only() and tool not in _READ_ONLY_TOOLS:
            result = _read_only_block()
            _audit(tool, _audit_args(kwargs), "blocked", int((time.monotonic() - started) * 1000))
            return result
        try:
            result = fn(*args, **kwargs)
            outcome = "ok"
            if isinstance(result, str):
                head = result.split(":", 1)[0].strip().lower()
                if head in {
                    "failed", "error", "blocked", "detached", "paused", "aborted",
                    "timed out", "no", "already", "nothing", "unknown",
                }:
                    outcome = head
        except pyautogui.FailSafeException:
            result = (
                "ABORTED: pyautogui failsafe triggered (mouse at a screen corner). "
                "The user may have interrupted automation. Nothing else was done."
            )
            outcome = "aborted"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            result = f"ERROR in {tool}: {type(exc).__name__}: {exc}"
            outcome = "error"
        _audit(tool, _audit_args(kwargs), outcome, int((time.monotonic() - started) * 1000))
        return result

    return wrapper


def _to_screen(x: float, y: float) -> tuple[int, int]:
    """Map coordinates from the last screenshot's pixel space to screen pixels."""
    if _shot:
        img_w, img_h = int(_shot["img_w"]), int(_shot["img_h"])
        if not (0 <= x < img_w and 0 <= y < img_h):
            raise ValueError(
                f"image coordinate ({x},{y}) is outside the last screenshot "
                f"{img_w}x{img_h}; take a fresh screenshot/snapshot and use its "
                "coordinates"
            )
        return (
            _shot["origin_x"] + int(round(x * _shot["screen_w"] / img_w)),
            _shot["origin_y"] + int(round(y * _shot["screen_h"] / img_h)),
        )
    return int(round(x)), int(round(y))


def _monitor_info() -> dict:
    """Virtual-desktop bounds and monitor count."""
    if not IS_WINDOWS:
        return {"count": 1, "virtual": [0, 0] + list(pyautogui.size())}
    metrics = ctypes.windll.user32.GetSystemMetrics
    return {
        "count": metrics(80),  # SM_CMONITORS
        "virtual": [metrics(76), metrics(77), metrics(78), metrics(79)],
    }


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _monitors() -> list[dict]:
    """Physical monitors, primary first, each with its screen-space bounds."""
    if not IS_WINDOWS:
        width, height = pyautogui.size()
        return [{"index": 0, "bounds": (0, 0, width, height), "primary": True}]

    user32 = ctypes.windll.user32
    found: list[dict] = []
    PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def callback(hmonitor, _hdc, _rect, _param):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(
                {
                    "bounds": (r.left, r.top, r.right - r.left, r.bottom - r.top),
                    "primary": bool(info.dwFlags & 1),
                }
            )
        return True

    user32.EnumDisplayMonitors(None, None, PROC(callback), 0)
    found.sort(key=lambda m: (not m["primary"], m["bounds"][0], m["bounds"][1]))
    for index, monitor in enumerate(found):
        monitor["index"] = index
    return found or [{"index": 0, "bounds": (0, 0) + pyautogui.size(), "primary": True}]


def _grab(region: tuple[int, int, int, int] | None = None):
    """Screenshot of `region` (screen coords, may be negative) or the whole desktop."""
    from PIL import ImageGrab

    if region is None:
        return ImageGrab.grab(all_screens=True)
    left, top, width, height = region
    return ImageGrab.grab(
        bbox=(left, top, left + width, top + height), all_screens=True
    )


def _active_window_title() -> str:
    if not IS_WINDOWS:
        return ""
    hwnd = _user32().GetForegroundWindow()
    if not hwnd:
        return ""
    length = ctypes.windll.user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
    return buf.value


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_u32.SendInput.restype = wintypes.UINT


def _send_unicode(text: str, chunk: int = 48, delay: float = 0.004) -> int:
    """Type arbitrary Unicode text through SendInput (handles emoji, accents).

    The union must hold all three input kinds: on 64-bit Windows `sizeof(INPUT)`
    is 40 bytes, and SendInput rejects the whole call with ERROR_INVALID_PARAMETER
    when cbSize is wrong. A KEYBDINPUT-only struct is 32 bytes, so it silently
    types nothing - which is exactly how this used to fail in browsers.
    """
    units = text.encode("utf-16-le")
    total = len(units) // 2
    if total == 0:
        return 0
    step = max(1, int(chunk))
    sent = 0
    for start in range(0, total, step):
        events = []
        for index in range(start, min(start + step, total)):
            scan = int.from_bytes(units[index * 2 : index * 2 + 2], "little")
            events.append(
                _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(0, scan, KEYEVENTF_UNICODE, 0, None)))
            )
            events.append(
                _INPUT(
                    type=1,
                    u=_INPUTUNION(
                        ki=_KEYBDINPUT(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
                    ),
                )
            )
        arr = (_INPUT * len(events))(*events)
        ctypes.set_last_error(0)
        done = _u32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
        if done != len(events):
            err = ctypes.get_last_error()
            raise OSError(
                err or 87,
                f"SendInput delivered {done}/{len(events)} key events "
                f"({ctypes.FormatError(err) if err else 'ERROR_INVALID_PARAMETER'})",
            )
        sent += len(events) // 2
        if delay:
            time.sleep(delay)
    return sent


# --------------------------------------------------------------------------
# Keyboard parsing + held-key bookkeeping
# --------------------------------------------------------------------------

_KEY_NAMES = {
    "page down": "pagedown", "page up": "pageup", "caps lock": "capslock",
    "num lock": "numlock", "scroll lock": "scrolllock", "print screen": "printscreen",
    "back space": "backspace",
}

_KEY_ALIASES = {
    "win": "winleft",
    "windows": "winleft",
    "cmd": "winleft",
    "super": "winleft",
    "meta": "winleft",
    "control": "ctrl",
    "option": "alt",
    "esc": "escape",
    "return": "enter",
    "del": "delete",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "space": "space",
}

_MODIFIER_KEYS = {"ctrl", "shift", "alt", "winleft", "winright"}

_HELD_KEYS: list[str] = []
_HELD_MOUSE: set[str] = set()


def _parse_keys(keys: str) -> list[str]:
    cleaned = (keys or "").strip().lower()
    for phrase, single in _KEY_NAMES.items():
        cleaned = cleaned.replace(phrase, single)
    parts = [p.strip() for p in re.split(r"[+\s]+", cleaned) if p.strip()]
    return [_KEY_ALIASES.get(part, part) for part in parts]


def _parse_modifiers(modifiers: str) -> tuple[list[str], str]:
    """Parse click modifiers, rejecting ordinary keys that could get stuck down."""
    parts = _parse_keys(modifiers)
    parsed: list[str] = []
    for part in parts:
        if part not in _MODIFIER_KEYS:
            return [], (
                f"unsupported click modifier {part!r}; use ctrl, shift, alt "
                "or win (for example \"ctrl+shift\")"
            )
        if part not in parsed:
            parsed.append(part)
    return parsed, ""


def _release_keys(parts: list[str]) -> None:
    for part in reversed(parts):
        try:
            pyautogui.keyUp(part)
        except Exception:
            pass


def _release_held() -> None:
    _release_keys(_HELD_KEYS)
    _HELD_KEYS.clear()
    for button in list(_HELD_MOUSE):
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            pass
    _HELD_MOUSE.clear()


atexit.register(_release_held)


def _log_event(message: str) -> None:
    """Append a diagnostic line to .runtime/logs/sessions.log."""
    try:
        log_dir = os.path.join(ROOT, ".runtime", "logs")
        os.makedirs(log_dir, exist_ok=True)
        token = os.environ.get(SESSION_ENV, "")[:8]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(log_dir, "sessions.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {message}  session={token}  pid={os.getpid()}\n")
    except Exception:
        pass


def _start_parent_watchdog() -> None:
    """Exit if the launcher disappears, even when the service outlives it."""
    lease = os.environ.get(PARENT_ENV)
    if not lease:
        return
    _log_event(f"lease armed  launcher={lease}")

    def watch() -> None:
        while True:
            time.sleep(2.0)
            if _parent_alive():
                continue
            try:
                _log_event(f"lease expired  launcher={lease}")
                _release_held()
            finally:
                os._exit(0)

    threading.Thread(target=watch, name="computer-use-parent-watch", daemon=True).start()


_start_parent_watchdog()


# --------------------------------------------------------------------------
# Windows clipboard helpers (no focus stealing, no extra packages)
# --------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
_winapi = None


def _clipboard_api():
    """Declare Win32 prototypes so 64-bit HANDLEs are never truncated."""
    global _winapi
    if _winapi is not None:
        return _winapi
    if not IS_WINDOWS:
        raise RuntimeError("clipboard is only implemented on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    _winapi = (user32, kernel32)
    return _winapi


def _open_clipboard(user32, attempts: int = 8) -> None:
    """OpenClipboard fails while another app owns it; retry briefly instead of dying."""
    for attempt in range(attempts):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05 * (attempt + 1))
    raise OSError(f"could not open the clipboard (another app is holding it, error {ctypes.get_last_error()})")


def _clipboard_get() -> str:
    user32, kernel32 = _clipboard_api()
    _open_clipboard(user32)
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_set(text: str) -> None:
    user32, kernel32 = _clipboard_api()
    data = (text + "\0").encode("utf-16-le")
    _open_clipboard(user32)
    handle = None
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError(f"GlobalAlloc failed (error {ctypes.get_last_error()})")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError(f"GlobalLock failed (error {ctypes.get_last_error()})")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError(f"SetClipboardData failed (error {ctypes.get_last_error()})")
        handle = None  # the clipboard now owns the memory
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def _clipboard_fallback(action: str, text: str = "") -> str | None:
    """Last resort via PowerShell, used when the Win32 clipboard is unavailable."""
    if not IS_WINDOWS:
        return None
    try:
        if action == "get":
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
            return done.stdout.rstrip("\r\n") if done.returncode == 0 else None
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return "" if done.returncode == 0 else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Windows window helpers
# --------------------------------------------------------------------------

def _user32():
    """user32 with 64-bit-safe prototypes (GetForegroundWindow defaults to int32!)."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetForegroundWindow.argtypes = []
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.BringWindowToTop.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    u.IsZoomed.argtypes = [wintypes.HWND]
    u.IsZoomed.restype = wintypes.BOOL
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    u.SetWindowPos.restype = wintypes.BOOL
    return u


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return (0, 0, 0, 0)


def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Client-area rectangle in screen coordinates (excludes title bar/borders)."""
    rect = wintypes.RECT()
    u = _user32()
    if not u.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return (0, 0, 0, 0)
    point = wintypes.POINT(0, 0)
    if not u.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point)):
        return (0, 0, 0, 0)
    return point.x, point.y, rect.right, rect.bottom


def _on_virtual_desktop(rect: tuple[int, int, int, int]) -> bool:
    vx, vy, vw, vh = _monitor_info()["virtual"]
    x, y, w, h = rect
    return not (x + w <= vx or y + h <= vy or x >= vx + vw or y >= vy + vh)


def _list_windows() -> list[dict]:
    if not IS_WINDOWS:
        return []
    user32 = ctypes.windll.user32
    found: list[dict] = []
    foreground = _user32().GetForegroundWindow()

    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True
        bounds = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        found.append(
            {
                "hwnd": int(hwnd),
                "title": buf.value,
                "rect": list(bounds),
                "active": hwnd == foreground,
                "minimized": bool(user32.IsIconic(hwnd)),
                "offscreen": not _on_virtual_desktop(bounds),
            }
        )
        return True

    user32.EnumWindows(PROC(callback), 0)
    return found


def _rank_title_matches(matches: list[dict], needle: str) -> list[dict]:
    """Best title match first: exact, prefix, substring; then active, then shortest."""
    def rank(window: dict) -> tuple:
        title = window["title"].lstrip("* ").lower()
        if title == needle:
            tier = 0
        elif title.startswith(needle):
            tier = 1
        else:
            tier = 2
        return (tier, not window["active"], len(window["title"]))

    return sorted(matches, key=rank)


def _force_foreground(hwnd: int) -> bool:
    """SetForegroundWindow refuses in many cases; use the AttachThreadInput workaround."""
    u = _user32()
    kernel32 = ctypes.windll.kernel32
    target = wintypes.HWND(hwnd)
    current = kernel32.GetCurrentThreadId()
    foreground = u.GetForegroundWindow()
    fg_thread = u.GetWindowThreadProcessId(foreground, None)
    target_thread = u.GetWindowThreadProcessId(target, None)

    attached_fg = attached_tg = False
    if fg_thread and fg_thread != current:
        attached_fg = bool(u.AttachThreadInput(current, fg_thread, True))
    if target_thread and target_thread not in (current, fg_thread):
        attached_tg = bool(u.AttachThreadInput(current, target_thread, True))
    try:
        u.BringWindowToTop(target)
        u.SetForegroundWindow(target)
        time.sleep(0.15)
        return u.GetForegroundWindow() == hwnd
    finally:
        if attached_tg:
            u.AttachThreadInput(current, target_thread, False)
        if attached_fg:
            u.AttachThreadInput(current, fg_thread, False)


def _focus_hwnd(
    hwnd: int,
    title: str = "",
    offscreen: bool = False,
    minimized: bool = False,
) -> tuple[bool, str]:
    """Focus an exact window handle (titles are not unique)."""
    if not IS_WINDOWS:
        return False, "window focus is only implemented on Windows"
    u = _user32()
    handle = wintypes.HWND(hwnd)

    moved = ""
    if offscreen:
        # A window parked outside every monitor cannot be clicked, so pull it back.
        px, py, pw, ph = _monitors()[0]["bounds"]
        u.ShowWindow(handle, 9)  # SW_RESTORE
        _, _, width, height = _window_rect(hwnd)
        u.SetWindowPos(
            handle, wintypes.HWND(0), px + 60, py + 60,
            max(480, min(width, pw - 120)),
            max(360, min(height, ph - 120)),
            0x0040,  # SWP_SHOWWINDOW
        )
        moved = " (window was outside every monitor, moved onto the primary display)"

    if minimized:
        u.ShowWindow(handle, 9)  # SW_RESTORE

    label = f"{title!r}" if title else f"hwnd {hwnd}"
    for attempt in range(3):
        if _force_foreground(hwnd):
            return True, f"focused: {title or hwnd}{moved}"
        if attempt == 0:
            # Windows unblocks SetForegroundWindow after a real input event.
            try:
                pyautogui.press("alt")
            except Exception:
                pass

    return False, (
        f"tried to focus {label} but Windows kept another window in front "
        "(click it once by hand, then retry)"
    )


def _focus_window(substring: str) -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "window focus is only implemented on Windows"
    needle = (substring or "").strip().lower()
    if not needle:
        return False, "title must not be empty"
    matches = [w for w in _list_windows() if needle in w["title"].lower()]
    if not matches:
        return False, f"no visible window title contains {substring!r}"
    target = _rank_title_matches(matches, needle)[0]
    return _focus_hwnd(
        target["hwnd"], target["title"], target["offscreen"], target["minimized"]
    )


# --------------------------------------------------------------------------
# UI Automation: element tree, stable ids and element-level actions
# --------------------------------------------------------------------------

_UI_CACHE: dict = {"at": 0.0, "window": "", "hwnd": 0, "items": {}, "rows": []}
_UI_TTL = 30.0

_CLICKABLE = (
    "Button", "MenuItem", "TabItem", "ListItem", "TreeItem",
    "CheckBox", "RadioButton", "Hyperlink", "SplitButton", "ComboBox",
)
_TEXTY = ("Edit", "Document", "ComboBox")
_INTERACTIVE = set(_CLICKABLE) | {
    "Edit", "Document", "Slider", "Spinner", "DataGrid", "DataItem",
    "Menu", "MenuBar", "ToolBar", "ScrollBar", "Text", "List", "Table",
    "Tree", "Header", "HeaderItem", "ColumnHeader",
}


def _uia_desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _pick_window(substring: str = "") -> dict | None:
    windows = _list_windows()
    needle = (substring or "").strip().lower()
    if needle:
        matches = [w for w in windows if needle in w["title"].lower()]
        if not matches:
            return None
        return _rank_title_matches(matches, needle)[0]
    active = int(_user32().GetForegroundWindow() or 0)
    for window in windows:
        if window["hwnd"] == active:
            return window
    return windows[0] if windows else None


def _element_rect(control) -> tuple[int, int, int, int]:
    rect = control.rectangle()
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _build_ui_snapshot(window: str = "", max_items: int = 120) -> dict:
    """Walk the accessibility tree once, assign eN ids, cache the wrappers."""
    _UI_CACHE["at"] = 0.0
    try:
        from pywinauto import Desktop  # noqa: F401
    except Exception:
        return {"error": "ui_tree unavailable: pywinauto is not installed in this runtime."}

    target_info = _pick_window(window)
    if not target_info:
        label = window.strip() or "the active window"
        return {"error": f"no visible window matching {label!r}"}

    try:
        target = _uia_desktop().window(handle=target_info["hwnd"])
        name = target.window_text() or target_info["title"]
    except Exception as exc:
        return {"error": f"could not attach to {target_info['title']!r}: {type(exc).__name__}: {exc}"}

    max_items = max(10, min(int(max_items), 400))
    rows: list[str] = []
    items: dict[str, dict] = {}
    truncated = False
    deadline = time.monotonic() + 8.0

    try:
        for control in target.descendants():
            if len(rows) >= max_items:
                truncated = True
                break
            if time.monotonic() > deadline:
                truncated = True
                break
            try:
                info = control.element_info
                kind = info.control_type or ""
                label = (control.window_text() or info.name or "").strip().replace("\n", " ")
                left, top, width, height = _element_rect(control)
                if width <= 0 or height <= 0:
                    # Virtualized lists expose off-screen rows with an empty
                    # rect. Keep them so scroll_into_view can find and reveal
                    # them instead of reporting "no such element".
                    if kind not in {"ListItem", "TreeItem", "DataItem"}:
                        continue
                if not label and kind not in _INTERACTIVE:
                    continue
                eid = f"e{len(rows) + 1}"
                automation_id = _automation_id(control)
                items[eid] = {
                    "id": eid,
                    "control": control,
                    "name": label,
                    "type": kind,
                    "rect": (left, top, width, height),
                    "window": name,
                    "hwnd": target_info["hwnd"],
                    "automation_id": automation_id,
                }
                suffix = (
                    f" #{automation_id}"
                    if automation_id and automation_id.casefold() != label.casefold()
                    else ""
                )
                rows.append(
                    f"{eid:<4} {kind:<13} [{left},{top},{width},{height}] {label}{suffix}"[:190]
                )
            except Exception:
                continue
    except Exception as exc:  # noqa: BLE001
        return {"error": f"ui_tree failed for {name!r}: {type(exc).__name__}: {exc}"}

    header = (
        f"window: {name!r} hwnd={target_info['hwnd']} "
        f"rect={tuple(target_info['rect'])} controls={len(rows)}"
    )
    if truncated:
        header += f" (truncated at {max_items})"
    text = header + "\n" + ("\n".join(rows) if rows else "(no accessible controls)")
    _UI_CACHE.update(
        {
            "at": time.monotonic(),
            "window": name,
            "hwnd": target_info["hwnd"],
            "items": items,
            "rows": rows,
        }
    )
    return {
        "window": name,
        "hwnd": target_info["hwnd"],
        "rect": tuple(target_info["rect"]),
        "text": text,
        "count": len(rows),
        "truncated": truncated,
        "items": items,
    }


def _match_rank(item: dict, needle: str) -> int:
    """Match by accessible name or stable automation id (0 best, 9 no match)."""
    name = item["name"].lstrip("* ").casefold()
    automation = str(item.get("automation_id", "")).casefold()
    for candidate in (name, automation):
        if not candidate:
            continue
        if candidate == needle:
            return 0
        if candidate.startswith(needle):
            return 1
        if needle in candidate:
            return 2
    return 9


def _find_in_cache(target: str, prefer: tuple[str, ...] | None = None) -> dict | None:
    needle = target.strip().lower()
    if re.fullmatch(r"e\d+", needle):
        return _UI_CACHE["items"].get(needle)
    candidates = []
    for item in _UI_CACHE["items"].values():
        rank = _match_rank(item, needle)
        if rank > 2:
            continue
        type_rank = prefer.index(item["type"]) if prefer and item["type"] in prefer else 9
        candidates.append(
            (rank, type_rank, len(item["name"]), item["rect"][1], item["rect"][0], item)
        )
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[:5])
    return candidates[0][5]


def _resolve_element(
    target: str, window: str = "", prefer: tuple[str, ...] | None = None
) -> tuple[dict | None, str]:
    needle = (target or "").strip()
    if not needle:
        return None, "empty element target"

    if re.fullmatch(r"e\d+", needle, re.IGNORECASE):
        item = _UI_CACHE["items"].get(needle.lower())
        if item is None:
            return None, f"unknown element id {needle!r}; take a snapshot/ui_tree first"
        if window.strip():
            requested = _pick_window(window)
            if requested is None:
                return None, f"no visible window matching {window!r}"
            if item["hwnd"] != requested["hwnd"]:
                return None, (
                    f"element {needle!r} belongs to {item['window']!r}, not "
                    f"{requested['title']!r}; take a snapshot/ui_tree of the requested "
                    "window first"
                )
        # An id resolves to the exact cached control, never to a re-numbered one.
        return item, ""

    cache_matches_window = not window or window.strip().lower() in _UI_CACHE["window"].lower()
    if (
        _UI_CACHE["items"]
        and cache_matches_window
        and time.monotonic() - _UI_CACHE["at"] <= _UI_TTL
    ):
        item = _find_in_cache(needle, prefer)
        if item is not None:
            return item, ""

    result = _build_ui_snapshot(window, 160)
    if "error" in result:
        return None, result["error"]
    item = _find_in_cache(needle, prefer)
    if item is not None:
        return item, ""
    visible = ", ".join(
        f"{it['id']}={it['name'][:24]}" for it in list(result["items"].values())[:8]
    )
    return None, f"no element matching {target!r} in {result['window']!r}; first controls: {visible}"


def _element_enabled(item: dict) -> bool | None:
    """Best-effort enabled state; some custom controls expose no UIA state."""
    try:
        return bool(item["control"].is_enabled())
    except Exception:
        return None


def _norm_control_type(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


# --------------------------------------------------------------------------
# Optional local OCR (Tesseract; no model or API key)
# --------------------------------------------------------------------------

def _tesseract_exe() -> str | None:
    env = os.environ.get("TESSERACT_CMD")
    if env and os.path.exists(env):
        return env
    for base in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if os.path.exists(base):
            return base
    return None


def _ocr(data: bytes) -> str:
    try:
        import pytesseract
    except Exception:
        return ""
    exe = _tesseract_exe()
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe
    else:
        return ""
    try:
        return pytesseract.image_to_string(PILImage.open(io.BytesIO(data))).strip()
    except Exception:
        return ""


def _ocr_lines(image) -> list[dict]:
    """OCR word boxes grouped into lines. Requires the optional Tesseract install."""
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:
        return []
    exe = _tesseract_exe()
    if not exe:
        return []
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception:
        return []
    lines: dict[tuple, dict] = {}
    for index, raw in enumerate(data.get("text", [])):
        word = (raw or "").strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError):
            confidence = -1.0
        if confidence < 20:
            continue
        key = (
            data.get("block_num", [0] * len(data["text"]))[index],
            data.get("par_num", [0] * len(data["text"]))[index],
            data.get("line_num", [0] * len(data["text"]))[index],
        )
        left = int(data["left"][index])
        top = int(data["top"][index])
        width = int(data["width"][index])
        height = int(data["height"][index])
        line = lines.setdefault(
            key,
            {"text": [], "left": left, "top": top, "right": left + width, "bottom": top + height,
             "confidence": confidence},
        )
        line["text"].append(word)
        line["left"] = min(line["left"], left)
        line["top"] = min(line["top"], top)
        line["right"] = max(line["right"], left + width)
        line["bottom"] = max(line["bottom"], top + height)
        line["confidence"] = min(line["confidence"], confidence)
    result = []
    for line in lines.values():
        result.append(
            {
                "text": " ".join(line["text"]),
                "left": line["left"],
                "top": line["top"],
                "width": line["right"] - line["left"],
                "height": line["bottom"] - line["top"],
                "confidence": round(line["confidence"], 1),
            }
        )
    result.sort(key=lambda row: (row["top"], row["left"]))
    return result


# --------------------------------------------------------------------------
# Capture mapping, element visibility and control state helpers
# --------------------------------------------------------------------------

def _capture_window_image(hwnd: int):
    """Capture a window and report the screen rectangle that the image covers.

    `capture_as_image` sometimes returns the client area while `GetWindowRect`
    includes the DWM frame. Detect which one the bitmap matches so image
    coordinates map to the right screen pixels instead of being offset.
    """
    rect = _window_rect(hwnd)
    client = _client_rect(hwnd)
    try:
        shot = _uia_desktop().window(handle=hwnd).capture_as_image()
    except Exception:
        shot = None
    if shot is not None:
        size = (int(shot.size[0]), int(shot.size[1]))
        if client[2] > 0 and client[3] > 0 and size == (client[2], client[3]):
            return shot, client
        if rect[2] > 0 and rect[3] > 0:
            return shot, rect
        return shot, (rect[0], rect[1], size[0], size[1])
    if rect[2] <= 0 or rect[3] <= 0:
        raise RuntimeError("the window is minimized or has no drawable area")
    return _grab(rect), rect


def _capture_source(
    window: str = "", monitor: int = 0, region: list[int] | None = None
) -> tuple[object, tuple[int, int, int, int], str]:
    """Capture without changing the last-screenshot mapping (used by OCR/waits)."""
    if window.strip():
        target = _pick_window(window)
        if not target:
            raise RuntimeError(f"no visible window title contains {window!r}")
        if _user32().IsIconic(target["hwnd"]):
            raise RuntimeError(f"window {target['title']!r} is minimized; restore it first")
        shot, rect = _capture_window_image(target["hwnd"])
        return shot, rect, f"window {target['title']!r}"
    if region is not None:
        values = [int(v) for v in region]
        if len(values) != 4:
            raise RuntimeError("region must be [left, top, right, bottom]")
        left, top, right, bottom = values
        vx, vy, vw, vh = _monitor_info()["virtual"]
        left, top = max(left, vx), max(top, vy)
        right, bottom = min(right, vx + vw), min(bottom, vy + vh)
        if right <= left or bottom <= top:
            raise RuntimeError("region is empty after clamping to the visible desktop")
        shot = _grab((left, top, right - left, bottom - top))
        return shot, (left, top, right - left, bottom - top), f"region {values}"
    monitors = _monitors()
    wanted = int(monitor)
    if wanted < 0:
        rect = tuple(_monitor_info()["virtual"])
        shot = _grab(rect)
        return shot, rect, "all monitors"
    chosen = monitors[min(max(0, wanted), len(monitors) - 1)]
    rect = tuple(chosen["bounds"])
    return _grab(rect), rect, f"monitor {chosen['index']}"


def _element_visible(item: dict) -> bool:
    """Whether the element's center is inside its window's client area."""
    try:
        left, top, width, height = _element_rect(item["control"])
    except Exception:
        left, top, width, height = item["rect"]
    center_x, center_y = left + max(1, width) // 2, top + max(1, height) // 2
    client = _client_rect(item["hwnd"])
    if client[2] <= 0 or client[3] <= 0:
        client = _window_rect(item["hwnd"])
    if client[2] <= 0 or client[3] <= 0:
        return False
    margin = 1
    return (
        client[0] + margin <= center_x < client[0] + client[2] - margin
        and client[1] + margin <= center_y < client[1] + client[3] - margin
    )


def _item_container(item: dict):
    """Nearest List/Tree/Table/DataGrid around an element (or the element itself)."""
    try:
        control = item["control"]
        kind = control.element_info.control_type or ""
        if kind in {"List", "Tree", "Table", "DataGrid"}:
            return control
        parent = control.parent()
        for _ in range(4):
            if parent is None:
                break
            kind = parent.element_info.control_type or ""
            if kind in {"List", "Tree", "Table", "DataGrid"}:
                return parent
            parent = parent.parent()
    except Exception:
        pass
    candidates = [
        other
        for other in _UI_CACHE["items"].values()
        if other["hwnd"] == item["hwnd"]
        and other["type"] in {"List", "Tree", "Table", "DataGrid"}
    ]
    if candidates:
        return max(
            candidates, key=lambda other: other["rect"][2] * other["rect"][3]
        )["control"]
    return None


def _scroll_pattern(control):
    try:
        return control.iface_scroll
    except Exception:
        return None


def _set_scroll_percent(pattern, percent: float, horizontal: bool = False) -> bool:
    percent = max(0.0, min(100.0, float(percent)))
    try:
        if horizontal:
            pattern.SetScrollPercent(percent, -1)
        else:
            pattern.SetScrollPercent(-1, percent)
        return True
    except Exception:
        return False


def _wheel_to_item(item: dict) -> bool:
    """Reveal a scrolled-out list/tree row, using UIA ScrollPattern when possible."""
    container = _item_container(item)
    if container is None:
        return False
    pattern = _scroll_pattern(container)
    siblings = sorted(
        (
            other
            for other in _UI_CACHE["items"].values()
            if other["hwnd"] == item["hwnd"] and other["type"] == item["type"]
        ),
        key=lambda other: int(other["id"][1:]),
    )
    try:
        target_index = siblings.index(item)
    except ValueError:
        target_index = 0

    if pattern is not None:
        try:
            if pattern.CurrentVerticallyScrollable:
                view = float(pattern.CurrentVerticalViewSize)
                view = view if 0 < view <= 100 else 10.0
                estimate = 100.0 * target_index / max(1, len(siblings) - 1)
                step = max(2.0, view * 0.6)
                candidates = [estimate]
                for factor in (1, -1, 2, -2, 4, -4):
                    candidates.append(estimate + factor * step)
                for percent in candidates:
                    if not 0.0 <= percent <= 100.0:
                        continue
                    if not _set_scroll_percent(pattern, percent):
                        break
                    time.sleep(0.2)
                    if _element_visible(item):
                        return True
        except Exception:
            pass

    try:
        left, top, width, height = _element_rect(container)
    except Exception:
        return False
    if width <= 0 or height <= 0:
        return False
    center_x, center_y = left + width // 2, top + height // 2
    visible = [
        index
        for index, sibling in enumerate(siblings)
        if sibling["rect"][2] > 0 and sibling["rect"][3] > 0
    ]
    direction = -1
    if visible and target_index < visible[0]:
        direction = 1
    try:
        container.set_focus()
    except Exception:
        pass

    for attempt in range(2):
        pyautogui.moveTo(center_x, center_y, duration=0.1)
        for _ in range(40):
            pyautogui.scroll(direction * 3, x=center_x, y=center_y)
            time.sleep(0.12)
            if _element_visible(item):
                return True
        direction = -direction
    return False


def _scroll_into_view(item: dict) -> bool:
    """Ask UI Automation (or the container's ScrollPattern) to reveal a control."""
    control = item["control"]
    if _element_visible(item):
        return True
    method = getattr(control, "scroll_into_view", None)
    if callable(method):
        try:
            method()
            time.sleep(0.15)
            if _element_visible(item):
                return True
        except Exception:
            pass
    try:
        control.iface_scrollitem.ScrollIntoView()
        time.sleep(0.15)
        if _element_visible(item):
            return True
    except Exception:
        pass
    return _wheel_to_item(item)


def _ensure_element_visible(item: dict) -> tuple[bool, str]:
    """Return (visible, note). Auto-scrolls once when the control is off-screen."""
    if _element_visible(item):
        return True, ""
    if _scroll_into_view(item):
        time.sleep(0.25)
        if _element_visible(item):
            return True, " (scrolled into view)"
    try:
        left, top, width, height = _element_rect(item["control"])
    except Exception:
        left, top, width, height = item["rect"]
    return False, (
        f"{item['name']!r} ({item['type']}) is scrolled out of view at "
        f"({left},{top},{width},{height}); call scroll_into_view or scroll its "
        "container first"
    )


def _control_value(control) -> str:
    """Best available current value: ValuePattern, selected text, then window text."""
    try:
        value = control.get_value()
        if value:
            return str(value)
    except Exception:
        pass
    try:
        value = control.iface_value.CurrentValue
        if value:
            return str(value)
    except Exception:
        pass
    method = getattr(control, "selected_text", None)
    if callable(method):
        try:
            value = method()
            if value:
                return str(value)
        except Exception:
            pass
    try:
        return str(control.window_text() or "")
    except Exception:
        return ""


def _automation_id(control) -> str:
    try:
        return str(getattr(control.element_info, "automation_id", "") or "")
    except Exception:
        return ""


def _probe(control, name: str):
    method = getattr(control, name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _control_texts(control) -> list[str]:
    """Row texts from a UIA control, using texts() then child window text."""
    method = getattr(control, "texts", None)
    if callable(method):
        try:
            values = [str(value).strip() for value in method() if str(value).strip()]
            if values:
                return values
        except Exception:
            pass
    values = []
    try:
        for child in control.children():
            text = ""
            try:
                text = str(child.window_text() or "").strip()
            except Exception:
                pass
            if not text:
                try:
                    text = str(child.element_info.name or "").strip()
                except Exception:
                    pass
            values.append(text)
    except Exception:
        pass
    if not values:
        try:
            values = [str(control.window_text() or "").strip()]
        except Exception:
            values = []
    return values


def _row_controls(control) -> list:
    """Find row-like descendants (DataItem/ListItem/TreeItem) under a container."""
    rows = []
    try:
        for child in control.descendants():
            try:
                kind = child.element_info.control_type or ""
            except Exception:
                continue
            if kind in {"DataItem", "ListItem", "TreeItem"}:
                rows.append(child)
    except Exception:
        pass
    if rows:
        return rows
    for method_name in ("items", "tree_items"):
        method = getattr(control, method_name, None)
        if callable(method):
            try:
                found = list(method())
                if found:
                    return found
            except Exception:
                continue
    return []


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    try:
        _user32().GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
    except Exception:
        return ""
    return buffer.value


def _process_id(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 0
    pid = wintypes.DWORD()
    try:
        _user32().GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def _popup_wrappers(main_hwnd: int):
    """Top-level popup windows owned by the same process (menus, combo drops)."""
    main_pid = _process_id(main_hwnd)
    try:
        wrappers = _uia_desktop().windows()
    except Exception:
        return
    for wrapper in wrappers:
        try:
            handle = int(wrapper.handle)
            if handle == main_hwnd:
                continue
            if _window_class(handle) in {"#32768", "ToolbarWindow32", "ComboLBox"}:
                yield wrapper
                continue
            if main_pid and int(wrapper.element_info.process_id) == main_pid:
                yield wrapper
        except Exception:
            continue


def _window_elevated(hwnd: int) -> bool | None:
    """Whether the process owning a window runs elevated (UIPI will block input)."""
    if not IS_WINDOWS:
        return None
    u = _user32()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not pid.value:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not ctypes.windll.advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            return None
        elevation = wintypes.DWORD()
        size = wintypes.DWORD(ctypes.sizeof(elevation))
        ok = ctypes.windll.advapi32.GetTokenInformation(
            token, 20, ctypes.byref(elevation), size, ctypes.byref(size)  # TokenElevation
        )
        return bool(elevation.value) if ok else None
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def _health() -> dict:
    """Pre-flight checks that explain most 'nothing happened' failures."""
    health: dict = {
        "paused": _PAUSED,
        "held_keys": list(_HELD_KEYS),
        "held_mouse": sorted(_HELD_MOUSE),
        "audit_log": _AUDIT_PATH,
        "tesseract": _tesseract_exe() or "not installed (OCR disabled)",
    }
    if not IS_WINDOWS:
        return health
    u = _user32()
    desktop = u.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    health["input_desktop"] = "available" if desktop else "locked or secure desktop (UAC)"
    if desktop:
        u.CloseDesktop(desktop)
    hwnd = int(u.GetForegroundWindow() or 0)
    if hwnd:
        health["foreground_class"] = _window_class(hwnd)
        elevated = _window_elevated(hwnd)
        if elevated is not None:
            health["foreground_elevated"] = elevated
            if elevated:
                health["warning"] = (
                    "the active window is elevated; Windows may ignore input sent "
                    "by this non-elevated session"
                )
    try:
        health["dpi"] = int(u.GetDpiForSystem())
    except Exception:
        pass
    return health


def _start_hotkey() -> None:
    """Ctrl+Alt+F12 toggles pause; held keys/buttons are released on pause."""
    if not IS_WINDOWS:
        return

    def run() -> None:
        u = _user32()
        try:
            registered = bool(u.RegisterHotKey(None, 1, 0x0003, 0x7B))  # Ctrl+Alt+F12
        except Exception:
            registered = False
        if not registered:
            _log_event("pause hotkey unavailable (Ctrl+Alt+F12 already taken)")
            return
        msg = wintypes.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    global _PAUSED
                    _PAUSED = not _PAUSED
                    if _PAUSED:
                        _release_held()
                    _log_event(
                        f"automation {'paused' if _PAUSED else 'resumed'} via Ctrl+Alt+F12"
                    )
        finally:
            try:
                u.UnregisterHotKey(None, 1)
            except Exception:
                pass

    threading.Thread(target=run, name="pause-hotkey", daemon=True).start()


def _annotate_items(shot, items: dict, region: tuple[int, int, int, int], max_boxes: int = 80):
    """Draw eN boxes/labels on a screenshot (Set-of-Mark grounding)."""
    from PIL import ImageDraw

    if not items:
        return shot
    canvas = shot.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    scale_x = canvas.size[0] / max(1, region[2])
    scale_y = canvas.size[1] / max(1, region[3])
    drawn = 0
    for item in items.values():
        if drawn >= max_boxes:
            break
        left, top, width, height = item["rect"]
        x0 = (left - region[0]) * scale_x
        y0 = (top - region[1]) * scale_y
        x1 = x0 + max(1, width) * scale_x
        y1 = y0 + max(1, height) * scale_y
        if x1 < 0 or y1 < 0 or x0 > canvas.size[0] or y0 > canvas.size[1]:
            continue
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(canvas.size[0] - 1, x1), min(canvas.size[1] - 1, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle((x0, y0, x1, y1), outline="#ff2d2d", width=2)
        label = item["id"]
        text_y = max(0, y0 - 12)
        draw.rectangle((x0, text_y, x0 + 7 * len(label) + 4, text_y + 12), fill="#ff2d2d")
        draw.text((x0 + 2, text_y), label, fill="#ffffff")
        drawn += 1
    return canvas


def _flash_rect(left: int, top: int, width: int, height: int, ms: int = 900) -> None:
    """Draw a temporary topmost border around a rectangle (Tk, transparent interior)."""
    import tkinter as tk

    width, height = max(8, int(width)), max(8, int(height))
    root = tk.Tk()
    try:
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", "#010203")
        root.configure(bg="#010203")
        root.geometry(f"{width}x{height}+{int(left)}+{int(top)}")
        canvas = tk.Canvas(root, bg="#010203", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(1, 1, width - 2, height - 2, outline="#ff2d2d", width=4)
        root.after(max(200, int(ms)), root.destroy)
        root.mainloop()
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

INSTRUCTIONS = """\
You control a real Windows desktop through this server.

Preferred loop: call `snapshot` (one call = active window, accessibility tree with
element ids e1/e2/... and optionally a screenshot), then act on the UI by id or
accessible name with `click_element`, `invoke_element`, `set_element_text` or
`read_element`. These hit the real control without coordinate guessing and are far
more reliable than clicking pixels. For a large window, `find_elements(query,
window, control_type)` returns only the matching controls and their ids. Take a
fresh snapshot after the UI changes; ids belong to the latest tree.

Click modifiers: pass `modifiers="ctrl+shift"` to `click` or `click_element` for
multi-select and range selection. Drag between controls with `drag_element(source,
destination, window)`; it uses live element centers and always releases the mouse.

If an app exposes no useful tree, fall back to the screenshot: coordinates you pass
to click/drag/scroll are in the last screenshot's image space (possibly downscaled)
and are mapped back to the right monitor or window automatically.
`scroll(target=..., window=...)` places the wheel over a specific pane or list.

Waits: `wait_for_window` and `wait_for_element` accept `state="appear" | "close"`
(and elements also `enabled` / `disabled`); `wait_for_idle` waits for the window
and element tree to settle; `wait_for_change` detects visible pixel changes.
Holds: `key_down` / `key_up` (or `hold_key`) for custom modifier sequences,
`mouse_button` for custom drags. Window control: `window_action` (activate/
minimize/maximize/restore/move/resize). `launch` can take arguments and wait for
a new window.

Structured UI: `read_table` extracts grid/list rows; `select_element` chooses a
combo/list item; `menu(path="File > Save As")` navigates menus; `file_dialog`
fills Open/Save dialogs; `get_element_state` verifies checked/selected/expanded
state; `scroll_into_view` brings a scrolled-out control into view. `snapshot`
annotates the screenshot with eN boxes. For canvas/Electron content with no
useful tree, `find_text`/`click_text` use optional local Tesseract OCR.

Multiple monitors: `screenshot(monitor=0)` is the primary; `screen_info` lists the
layout; `monitor=-1` captures everything. `screenshot(window="Title")` captures one
window even when it is behind another. Use the display that shows the target, or
clicks will land on the wrong screen.

Safety: the pointer corner aborts pyautogui. Prefer `close_window(title)` over a
blind Alt+F4: it asks one specific window to close and never touches another.
`pause(action="on")` (or Ctrl+Alt+F12) freezes automation and releases held
input; `release_all` is the emergency release. `runtime_status` reports lock/UAC,
DPI, elevation, OCR and held input. Every tool call is appended to
`.runtime/logs/actions.log` with typed text redacted.
"""

if _read_only():
    INSTRUCTIONS += (
        "\nREAD-ONLY MODE is active: inspect with snapshot/ui_tree/screenshot/"
        "find_elements/read_element, but all mouse, keyboard, window and launch "
        "actions are blocked by the server.\n"
    )

mcp = FastMCP("computer-use", instructions=INSTRUCTIONS, log_level="WARNING")


@mcp.tool()
@_safe
def screenshot(
    monitor: int = 0,
    max_width: int = 1280,
    ocr: bool = False,
    region: list[int] | None = None,
    window: str = "",
) -> list:
    """Capture a screen, a rectangle or a window and return it as an image.

    monitor: 0 = primary, 1..N = that monitor (see screen_info), -1 = all monitors.
    region: optional [left, top, right, bottom] in screen pixels (overrides monitor).
    window: optional title substring; captures that window even if it is behind
    other windows (overrides monitor/region).
    Coordinates for click/drag/scroll refer to this image's pixels.
    Set ocr=True to also get text recognized from the screen (if Tesseract is installed).
    """
    global _shot
    max_width = max(320, min(int(max_width), 3840))

    if window.strip():
        target = _pick_window(window)
        if not target:
            return [f"no visible window title contains {window!r}"]
        hwnd = target["hwnd"]
        if _user32().IsIconic(hwnd):
            return [f"window {target['title']!r} is minimized; restore it with window_action first"]
        try:
            shot, rect = _capture_window_image(hwnd)
        except RuntimeError as exc:
            return [f"window {target['title']!r}: {exc}"]
        screen_w, screen_h = rect[2], rect[3]
        origin_x, origin_y = rect[0], rect[1]
        label = f"window {target['title']!r}"
    elif region is not None:
        try:
            values = [int(v) for v in region]
        except Exception:
            return ["region must be [left, top, right, bottom] in screen pixels"]
        if len(values) != 4:
            return ["region must be [left, top, right, bottom] in screen pixels"]
        left, top, right, bottom = values
        vx, vy, vw, vh = _monitor_info()["virtual"]
        left, top = max(left, vx), max(top, vy)
        right, bottom = min(right, vx + vw), min(bottom, vy + vh)
        if right <= left or bottom <= top:
            return ["region is empty after clamping to the visible desktop"]
        # _grab expects (left, top, width, height); region supplies absolute
        # right/bottom edges, so convert instead of treating them as sizes.
        shot = _grab((left, top, right - left, bottom - top))
        screen_w, screen_h = shot.size
        origin_x, origin_y = left, top
        region = (left, top, right, bottom)
        label = f"region {region}"
    else:
        monitors = _monitors()
        if int(monitor) < 0:
            region = tuple(_monitor_info()["virtual"])
            label = "all monitors"
        else:
            wanted = int(monitor)
            chosen = monitors[min(wanted, len(monitors) - 1)]
            region = chosen["bounds"]
            label = f"monitor {chosen['index']}" + (" (primary)" if chosen["primary"] else "")
            if wanted != chosen["index"]:
                label += f" - requested monitor {wanted}, only {len(monitors)} present"
        shot = _grab(region)
        screen_w, screen_h = shot.size
        origin_x, origin_y = region[0], region[1]

    if screen_w > max_width:
        height = int(round(screen_h * max_width / screen_w))
        shot = shot.resize((max_width, height), PILImage.LANCZOS)

    buf = io.BytesIO()
    shot.save(buf, format="PNG")
    data = buf.getvalue()

    _shot = {
        "img_w": shot.size[0],
        "img_h": shot.size[1],
        "screen_w": screen_w,
        "screen_h": screen_h,
        "origin_x": origin_x,
        "origin_y": origin_y,
    }

    cursor = pyautogui.position()
    lines = [
        f"{label}: screenshot {shot.size[0]}x{shot.size[1]} for {screen_w}x{screen_h} "
        f"at screen ({origin_x},{origin_y}) - use this image's coordinates.",
        f"Active window: {_active_window_title() or '(unknown)'}",
        f"Cursor at screen ({cursor.x}, {cursor.y}); screen corners abort automation (failsafe).",
    ]
    if len(_monitors()) > 1:
        layout = "; ".join(
            f"{m['index']}{'*' if m['primary'] else ''}={m['bounds']}" for m in _monitors()
        )
        lines.append(f"{len(_monitors())} monitors: {layout}  (* = primary)")
    if ocr:
        text = _ocr(data)
        lines.append("OCR text:\n" + (text if text else "(unavailable: install Tesseract OCR)"))
    return [MCPImage(data=data, format="png"), "\n".join(lines)]


@mcp.tool()
@_safe
def screen_info() -> str:
    """Screen size, monitor layout, active window, cursor position and screenshot mapping."""
    sw, sh = pyautogui.size()
    cursor = pyautogui.position()
    mapping = "none yet - take a screenshot first" if not _shot else json.dumps(_shot)
    layout = "; ".join(
        f"{m['index']}{'*' if m['primary'] else ''}={m['bounds']}" for m in _monitors()
    )
    return (
        f"primary_screen={sw}x{sh}; monitors({layout}, * = primary); "
        f"cursor=({cursor.x},{cursor.y}); "
        f"active_window={_active_window_title()!r}; screenshot_mapping={mapping}"
    )


@mcp.tool()
@_safe
def click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    modifiers: str = "",
) -> str:
    """Click at (x, y) in the last screenshot's pixel space.

    button: left|right|middle. modifiers: optional ctrl, shift, alt or win,
    for example "ctrl+shift" for multi-select or range selection.
    """
    button = button if button in {"left", "right", "middle"} else "left"
    clicks = max(1, min(int(clicks), 3))
    mods, error = _parse_modifiers(modifiers)
    if error:
        return f"failed: {error}"
    sx, sy = _to_screen(x, y)
    try:
        for key in mods:
            pyautogui.keyDown(key)
            time.sleep(0.03)
        pyautogui.click(sx, sy, clicks=clicks, interval=0.08 if clicks > 1 else 0.0, button=button)
    finally:
        _release_keys(mods)
    time.sleep(0.2)
    prefix = f"{'+'.join(mods)}+" if mods else ""
    return (
        f"{prefix}{button} click x{clicks} at image ({x},{y}) -> screen ({sx},{sy}). "
        f"Active: {_active_window_title()!r}"
    )


@mcp.tool()
@_safe
def move(x: int, y: int, duration: float = 0.2) -> str:
    """Move the pointer to (x, y) in the last screenshot's pixel space (e.g. to hover)."""
    sx, sy = _to_screen(x, y)
    pyautogui.moveTo(sx, sy, duration=max(0.0, min(float(duration), 2.0)))
    return f"pointer at screen ({sx},{sy})"


@mcp.tool()
@_safe
def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5, button: str = "left") -> str:
    """Press at (x1,y1), drag to (x2,y2), release. All in screenshot pixel space."""
    s1 = _to_screen(x1, y1)
    s2 = _to_screen(x2, y2)
    button = button if button in {"left", "right", "middle"} else "left"
    pyautogui.moveTo(*s1, duration=0.15)
    _HELD_MOUSE.add(button)
    try:
        pyautogui.mouseDown(button=button)
        time.sleep(0.1)
        pyautogui.moveTo(*s2, duration=max(0.1, min(float(duration), 3.0)))
        time.sleep(0.1)
    finally:
        try:
            pyautogui.mouseUp(button=button)
        finally:
            _HELD_MOUSE.discard(button)
    time.sleep(0.2)
    return f"dragged {s1} -> {s2}"


@mcp.tool()
@_safe
def scroll(
    amount: int,
    x: int = -1,
    y: int = -1,
    horizontal: bool = False,
    target: str = "",
    window: str = "",
) -> str:
    """Scroll the wheel. Positive = up/right, negative = down/left; 1 ≈ one notch.

    Pass x/y (screenshot pixel space) to place the pointer there first; -1 keeps it.
    Pass target (element id or accessible name) and window to scroll a specific list
    or pane by placing the wheel over that control's center instead.
    Set horizontal=True to scroll sideways (e.g. wide pages or timelines).
    """
    notches = max(-50, min(int(amount), 50))
    place = ""
    if (target or "").strip():
        item, error = _resolve_element(target, window)
        if item is None:
            return f"failed: {error}"
        ok, message = _focus_item_window(item)
        if not ok:
            return f"failed: {message}"
        container = _item_container(item)
        pattern = _scroll_pattern(container) if container is not None else None
        if pattern is not None:
            try:
                if horizontal:
                    scrollable = pattern.CurrentHorizontallyScrollable
                    current = pattern.CurrentHorizontalScrollPercent
                    view = pattern.CurrentHorizontalViewSize
                else:
                    scrollable = pattern.CurrentVerticallyScrollable
                    current = pattern.CurrentVerticalScrollPercent
                    view = pattern.CurrentVerticalViewSize
                if scrollable:
                    current = 0.0 if current < 0 else float(current)
                    view = float(view) if 0 < view <= 100 else 10.0
                    delta = notches * max(1.0, view) / 5.0
                    new = current + delta if horizontal else current - delta
                    if _set_scroll_percent(pattern, new, horizontal):
                        time.sleep(0.2)
                        label = item["name"] or _automation_id(item["control"]) or item["type"]
                        return (
                            f"scrolled {notches} {'horizontal' if horizontal else 'vertical'} "
                            f"notch(es) over {label!r} via ScrollPattern "
                            f"({current:.0f}% -> {max(0.0, min(100.0, new)):.0f}%)"
                        )
            except Exception:
                pass
        px, py = _element_center(item)
        vx, vy, vw, vh = _monitor_info()["virtual"]
        if not (vx <= px < vx + vw and vy <= py < vy + vh):
            return f"failed: {item['name']!r} is outside every monitor at ({px},{py})"
        pyautogui.moveTo(px, py, duration=0.1)
        place = f"over {item['name']!r} at ({px},{py})"
    elif x >= 0 and y >= 0:
        px, py = _to_screen(x, y)
        pyautogui.moveTo(px, py, duration=0.1)
        place = f"at ({px},{py})"
    else:
        point = pyautogui.position()
        place = f"at ({point.x},{point.y})"

    if horizontal:
        pyautogui.hscroll(notches)
    else:
        pyautogui.scroll(notches)
    time.sleep(0.2)
    axis = "horizontal" if horizontal else "vertical"
    return f"scrolled {notches} {axis} notch(es) {place}"


def _type_text_impl(text: str, press_enter: bool = False) -> int:
    """Type text into the focused control. Returns the number of characters sent."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    typed = 0
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            pyautogui.press("enter")
        for part_index, part in enumerate(line.split("\t")):
            if part_index:
                pyautogui.press("tab")
            if not part:
                continue
            if IS_WINDOWS:
                typed += _send_unicode(part)
            else:
                pyautogui.write(part, interval=0.01)
                typed += len(part)
    if press_enter:
        pyautogui.press("enter")
    time.sleep(0.25)
    return typed


@mcp.tool()
@_safe
def type_text(text: str, press_enter: bool = False) -> str:
    """Type text into the focused control. Handles Unicode, newlines and tabs."""
    if not text and not press_enter:
        return "nothing to type"
    _type_text_impl(text, press_enter)
    return f"typed {len(text or '')} character(s)" + (" and pressed Enter" if press_enter else "")


@mcp.tool()
@_safe
def press_keys(keys: str) -> str:
    """Press a key or combination, e.g. "enter", "tab", "esc", "ctrl+c", "alt+f4", "win".

    Combinations are held with a short settle delay and released in reverse order,
    which is more reliable than a single synthesized hotkey for apps that inspect
    modifier state (and it cannot leave a modifier stuck if a name is invalid).
    """
    parts = _parse_keys(keys)
    if not parts:
        return "no keys given"
    try:
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            for part in parts:
                pyautogui.keyDown(part)
                time.sleep(0.03)
            for part in reversed(parts):
                pyautogui.keyUp(part)
                time.sleep(0.03)
    except Exception:
        _release_keys(parts)
        raise
    time.sleep(0.25)
    return f"pressed {'+'.join(parts)}"


@mcp.tool()
@_safe
def key_down(keys: str) -> str:
    """Hold one or more keys down (e.g. "shift", "ctrl+shift") until key_up.

    Use for Shift-click ranges, Ctrl-drag, or modifier + click sequences.
    """
    parts = _parse_keys(keys)
    if not parts:
        return "no keys given"
    for part in parts:
        pyautogui.keyDown(part)
        if part not in _HELD_KEYS:
            _HELD_KEYS.append(part)
        time.sleep(0.03)
    return f"holding {'+'.join(parts)}; release with key_up"


@mcp.tool()
@_safe
def key_up(keys: str) -> str:
    """Release keys previously held with key_down (same names)."""
    parts = _parse_keys(keys)
    if not parts:
        parts = list(reversed(_HELD_KEYS))
    released = []
    for part in reversed(parts):
        try:
            pyautogui.keyUp(part)
            released.append(part)
        except Exception:
            pass
        if part in _HELD_KEYS:
            _HELD_KEYS.remove(part)
        time.sleep(0.03)
    return f"released {', '.join(released) if released else '(nothing)'}"


@mcp.tool()
@_safe
def hold_key(keys: str, seconds: float = 1.0) -> str:
    """Hold keys for a duration, then release (e.g. hold_key("shift", 0.5))."""
    parts = _parse_keys(keys)
    if not parts:
        return "no keys given"
    seconds = max(0.05, min(float(seconds), 10.0))
    try:
        for part in parts:
            pyautogui.keyDown(part)
            time.sleep(0.03)
        time.sleep(seconds)
    finally:
        _release_keys(parts)
    return f"held {'+'.join(parts)} for {seconds:g}s and released"


@mcp.tool()
@_safe
def mouse_button(action: str = "down", button: str = "left") -> str:
    """Press or release a mouse button: action down|up, button left|right|middle.

    Use with move() to build custom drags/selections that drag() cannot express.
    """
    action = (action or "down").strip().lower()
    button = button if button in {"left", "right", "middle"} else "left"
    if action == "down":
        pyautogui.mouseDown(button=button)
        _HELD_MOUSE.add(button)
    elif action == "up":
        pyautogui.mouseUp(button=button)
        _HELD_MOUSE.discard(button)
    else:
        return "action must be down or up"
    time.sleep(0.1)
    return f"{button} mouse button {action} at {pyautogui.position()}"


@mcp.tool()
@_safe
def clipboard(action: str = "get", text: str = "") -> str:
    """Read or write the Windows clipboard. action: get|set|paste (set + ctrl+v)."""
    action = (action or "get").strip().lower()
    if _read_only() and action != "get":
        return _read_only_block()
    if action == "get":
        try:
            value = _clipboard_get()
        except OSError:
            value = _clipboard_fallback("get")
            if value is None:
                raise
        return f"clipboard ({len(value)} chars):\n{value}"
    if action in {"set", "paste"}:
        try:
            _clipboard_set(text)
        except OSError:
            if _clipboard_fallback("set", text) is None:
                raise
        if action == "paste":
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            return f"clipboard set ({len(text)} chars) and pasted with Ctrl+V"
        return f"clipboard set ({len(text)} chars)"
    return "action must be get, set or paste"


@mcp.tool()
@_safe
def list_windows() -> str:
    """List visible top-level windows: handle, title, rectangle, active/minimized."""
    windows = _list_windows()
    if not windows:
        return "no visible windows found"
    lines = [
        f"{'*' if w['active'] else ' '} {w['hwnd']:>10} {w['rect']}  "
        f"{'[min] ' if w['minimized'] else ''}"
        f"{'[off-screen] ' if w['offscreen'] else ''}{w['title']}"
        for w in windows
    ]
    return "(* = active)\n" + "\n".join(lines[:60])


@mcp.tool()
@_safe
def focus_window(title: str) -> str:
    """Bring the visible window whose title contains `title` to the foreground."""
    ok, message = _focus_window(title)
    return message


WM_CLOSE = 0x0010


def _close_window(substring: str, wait: float = 1.5) -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "window closing is only implemented on Windows"
    needle = (substring or "").strip().lower()
    if not needle:
        return False, "title must not be empty"
    matches = [w for w in _list_windows() if needle in w["title"].lower()]
    if not matches:
        return False, f"no visible window title contains {substring!r}"
    target = _rank_title_matches(matches, needle)[0]
    u = _user32()
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    if not u.PostMessageW(wintypes.HWND(target["hwnd"]), WM_CLOSE, 0, 0):
        return False, f"could not ask {target['title']!r} to close (error {ctypes.get_last_error()})"
    deadline = time.time() + max(0.2, float(wait))
    while time.time() < deadline:
        time.sleep(0.1)
        if not any(w["hwnd"] == target["hwnd"] for w in _list_windows()):
            active = _active_window_title()
            return True, f"closed: {target['title']}" + (f". Active now: {active!r}" if active else "")
    return False, (
        f"{target['title']!r} is still open - it is probably showing a save/discard "
        "dialog; deal with that dialog (or press the matching key) and try again"
    )


@mcp.tool()
@_safe
def close_window(title: str, wait: float = 1.5) -> str:
    """Ask the visible window whose title contains `title` to close (same as its X button).

    Safer than pressing Alt+F4, which always hits whatever window happens to be
    focused - the mistake that closed an unrelated browser window during a test.
    """
    ok, message = _close_window(title, wait)
    return f"{'ok: ' if ok else 'failed: '}{message}"


def _split_launch_args(arguments: str) -> list[str]:
    """Parse a Windows command line the same way Windows itself does."""
    if not arguments.strip():
        return []
    if IS_WINDOWS:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.CommandLineToArgvW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_int),
        ]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        count = ctypes.c_int(0)
        argv = shell32.CommandLineToArgvW(arguments, ctypes.byref(count))
        if not argv:
            raise OSError(ctypes.get_last_error(), "could not parse launch arguments")
        try:
            return [argv[index] for index in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    return shlex.split(arguments)


@mcp.tool()
@_safe
def launch(
    target: str,
    wait: float = 2.0,
    arguments: str = "",
    wait_for: str = "",
    timeout: float = 15.0,
) -> str:
    """Start an app, file or URL, e.g. "notepad.exe", "https://github.com", "calc".

    arguments: extra command-line arguments (quoted like a shell command line).
    wait_for:  optional window title substring to wait for; returns its handle.
    timeout:   seconds to wait for wait_for (default 15).
    Without wait_for, waits `wait` seconds and tries to focus the new window.
    """
    before = _active_window_title()
    before_hwnds = {w["hwnd"] for w in _list_windows()}
    detach = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if arguments.strip():
        command = [target, *_split_launch_args(arguments)]
        subprocess.Popen(command, shell=False, cwd=os.path.expanduser("~"), **detach)
    elif IS_WINDOWS:
        try:
            os.startfile(target)  # noqa: S606 - intentional shell-less launch
        except OSError:
            subprocess.Popen(target, shell=False, cwd=os.path.expanduser("~"), **detach)
    else:
        subprocess.Popen([target], cwd=os.path.expanduser("~"), **detach)
    time.sleep(max(0.0, min(float(wait), 15.0)))

    if wait_for.strip():
        needle = wait_for.strip().lower()
        start = time.monotonic()
        deadline = start + max(0.2, min(float(timeout), 120.0))
        fallback: dict | None = None
        while time.monotonic() < deadline:
            matches = [w for w in _list_windows() if needle in w["title"].lower()]
            if matches:
                ranked = _rank_title_matches(matches, needle)
                new = [w for w in ranked if w["hwnd"] not in before_hwnds]
                if new:
                    window = new[0]
                    return (
                        f"launched {target!r}; new window appeared: "
                        f"{window['title']!r} hwnd={window['hwnd']} rect={window['rect']}"
                    )
                fallback = fallback or ranked[0]
                # A single-instance app reuses its old window instead of creating one.
                if time.monotonic() - start >= min(2.0, float(timeout)):
                    return (
                        f"launched {target!r}; existing window matched (no new window "
                        f"appeared): {fallback['title']!r} hwnd={fallback['hwnd']}"
                    )
            time.sleep(0.25)
        if fallback:
            return (
                f"launched {target!r}; existing window matched (no new window appeared): "
                f"{fallback['title']!r} hwnd={fallback['hwnd']}"
            )
        visible = ", ".join(w["title"] for w in _list_windows()[:6])
        return (
            f"launched {target!r} but no window title contained {wait_for!r} "
            f"within {timeout:g}s; visible: {visible}"
        )

    # A second launch of an already-running app often just raises its old window
    # slowly (or not at all), so nudge it forward by name.
    note = ""
    after = _active_window_title()
    if after == before:
        stem = os.path.splitext(os.path.basename(target))[0].lower()
        if stem and stem not in {"http:", "https:", "www"}:
            ok, message = _focus_window(stem)
            if ok:
                note = f" ({message})"
    return f"launched {target!r}. Active: {_active_window_title()!r}{note}"


@mcp.tool()
@_safe
def wait(seconds: float) -> str:
    """Sleep so an app can finish loading, then report the active window."""
    seconds = max(0.0, min(float(seconds), 30.0))
    time.sleep(seconds)
    return f"waited {seconds:g}s. Active: {_active_window_title()!r}"


@mcp.tool()
@_safe
def ui_tree(title: str = "", max_items: int = 140) -> str:
    """Dump accessible controls with eN ids for a window (Windows UI Automation).

    Cheaper and more precise than clicking by eye. Empty `title` uses the active
    window. The ids it prints can be passed straight to click_element/invoke_element/
    set_element_text/read_element/drag_element/scroll (ids belong to the most recent
    tree or snapshot). For a large window, use find_elements to return only matches.
    """
    result = _build_ui_snapshot(title, max_items)
    if "error" in result:
        return "ui_tree: " + result["error"]
    return (
        result["text"]
        + "\nAct with click_element/invoke_element/set_element_text/read_element/"
        "drag_element/scroll using these ids or names; or narrow with find_elements."
    )


@mcp.tool()
@_safe
def snapshot(
    window: str = "",
    max_items: int = 100,
    screenshot: bool = True,
    max_width: int = 1280,
    annotate: bool = True,
) -> list:
    """One-call look: window + accessible element tree (eN ids) + a screenshot.

    This is the preferred first call: it gives you both the deterministic element
    ids and a visual overview in one round trip. `window` is a title substring;
    empty means the active window. Set screenshot=False for a text-only refresh.
    annotate=True draws the eN boxes and labels onto the image (Set-of-Mark).
    """
    result = _build_ui_snapshot(window, max_items)
    if "error" in result:
        return ["snapshot: " + result["error"]]

    text = (
        result["text"]
        + "\nAct with click_element/invoke_element/set_element_text/read_element/"
        "drag_element/scroll using these ids or names; ids belong to this snapshot. "
        "For a large tree, find_elements returns only matches."
    )
    if not screenshot:
        return [text]

    hwnd = result["hwnd"]
    if _user32().IsIconic(hwnd):
        return [text + "\nScreenshot unavailable: the window is minimized; restore it first."]
    try:
        shot, rect = _capture_window_image(hwnd)
    except RuntimeError:
        return [text + "\nScreenshot unavailable: the window is minimized."]

    screen_w, screen_h = rect[2], rect[3]
    max_width = max(320, min(int(max_width), 3840))
    if shot.size[0] > max_width:
        height = int(round(shot.size[1] * max_width / shot.size[0]))
        shot = shot.resize((max_width, height), PILImage.LANCZOS)
    if annotate:
        shot = _annotate_items(shot, result["items"], rect)

    buf = io.BytesIO()
    shot.save(buf, format="PNG")
    data = buf.getvalue()
    global _shot
    _shot = {
        "img_w": shot.size[0],
        "img_h": shot.size[1],
        "screen_w": screen_w,
        "screen_h": screen_h,
        "origin_x": rect[0],
        "origin_y": rect[1],
    }
    text += (
        f"\nScreenshot: {shot.size[0]}x{shot.size[1]} for {screen_w}x{screen_h} "
        f"at screen ({rect[0]},{rect[1]}) - click/drag/scroll coordinates refer to this image."
    )
    return [MCPImage(data=data, format="png"), text]


@mcp.tool()
@_safe
def find_elements(
    query: str = "",
    window: str = "",
    control_type: str = "",
    limit: int = 20,
) -> str:
    """Search a window's accessibility tree and return only matching controls.

    query matches an accessible name or control type (case-insensitive; an exact
    eN id also works). control_type narrows it (for example "Button", "Edit",
    "ListItem"). Empty query with a type lists that type; empty both lists the
    first controls. Results carry live eN ids usable by click_element,
    invoke_element, set_element_text, read_element, drag_element and scroll.
    This is much cheaper than dumping a large ui_tree.
    """
    needle = (query or "").strip().lower()
    kind = _norm_control_type(control_type)
    limit = max(1, min(int(limit), 100))
    result = _build_ui_snapshot(window, 400)
    if "error" in result:
        return "find_elements: " + result["error"]

    items = list(result["items"].values())

    exact_id = result["items"].get(needle) if re.fullmatch(r"e\d+", needle) else None
    if exact_id is not None:
        enabled = _element_enabled(exact_id)
        left, top, width, height = exact_id["rect"]
        suffix = " [disabled]" if enabled is False else ""
        return (
            f"{exact_id['id']} {exact_id['type']} {exact_id['name']!r} "
            f"at ({left},{top},{width},{height}) in {result['window']!r}{suffix}"
        )

    ranked: list[tuple] = []
    for item in items:
        type_key = _norm_control_type(item["type"])
        if kind and type_key != kind:
            continue
        type_rank = _CLICKABLE.index(item["type"]) if item["type"] in _CLICKABLE else 99
        if not needle:
            rank = 0
        else:
            name = item["name"].lstrip("* ").casefold()
            automation = str(item.get("automation_id", "")).casefold()
            if name == needle or automation == needle:
                rank = 0
            elif name.startswith(needle) or automation.startswith(needle):
                rank = 1
            elif needle in name or needle in automation:
                rank = 2
            elif type_key == needle:
                rank = 3
            elif needle in type_key:
                rank = 4
            else:
                continue
        ranked.append((rank, type_rank, item["rect"][1], item["rect"][0], item))

    ranked.sort(key=lambda row: row[:4])
    total_matches = len(ranked)
    selected = [row[4] for row in ranked[:limit]]

    header = (
        f"{total_matches} match(es) in {result['window']!r} hwnd={result['hwnd']} "
        f"from {result['count']} controls"
    )
    if result.get("truncated"):
        header += " (tree truncated at 400; narrow the query/window if needed)"
    if not selected:
        return (
            f"{header}. No matching controls. Try a shorter query, a control_type, "
            "or take snapshot/ui_tree to see the available names."
        )

    rows = []
    for item in selected:
        enabled = _element_enabled(item)
        left, top, width, height = item["rect"]
        suffix = " [disabled]" if enabled is False else ""
        automation = str(item.get("automation_id", ""))
        auto_suffix = (
            f" #{automation}"
            if automation and automation.casefold() != item["name"].casefold()
            else ""
        )
        rows.append(
            f"{item['id']:<4} {item['type']:<13} "
            f"[{left},{top},{width},{height}] {item['name']}{auto_suffix}{suffix}"
        )
    if total_matches > len(selected):
        header += f"; showing first {len(selected)} (raise limit for more)"
    return header + "\n" + "\n".join(rows)


def _element_center(item: dict) -> tuple[int, int]:
    """Current screen-space center of a cached control (re-reads the live rect)."""
    try:
        left, top, width, height = _element_rect(item["control"])
    except Exception:
        left, top, width, height = item["rect"]
    return left + max(1, width) // 2, top + max(1, height) // 2


def _focus_item_window(item: dict) -> tuple[bool, str]:
    """Focus the exact window an element came from, even if its title is shared."""
    live = next((w for w in _list_windows() if w["hwnd"] == item["hwnd"]), None)
    return _focus_hwnd(
        item["hwnd"],
        item["window"],
        bool(live and live["offscreen"]),
        bool(live and live["minimized"]),
    )


@mcp.tool()
@_safe
def drag_element(
    source: str,
    destination: str,
    window: str = "",
    duration: float = 0.6,
    button: str = "left",
) -> str:
    """Drag from one accessible element to another in the same window.

    source/destination are eN ids or accessible names. Uses each control's live
    center, focuses their window first, and always releases the mouse even when
    the drag raises. Use this for file moves, list/tree reordering and drag-drop
    targets where pixel coordinates would be fragile.
    """
    source_item, error = _resolve_element(source, window)
    if source_item is None:
        return f"failed: source {error}"
    dest_item, error = _resolve_element(destination, window)
    if dest_item is None:
        return f"failed: destination {error}"
    if source_item["hwnd"] != dest_item["hwnd"]:
        return "failed: source and destination must belong to the same window"
    if source_item["id"] == dest_item["id"]:
        return "failed: source and destination are the same element"

    ok, message = _focus_item_window(source_item)
    if not ok:
        return f"failed: {message}"
    for label, item in (("source", source_item), ("destination", dest_item)):
        visible, note = _ensure_element_visible(item)
        if not visible:
            return f"failed: {label} {note}"

    sx, sy = _element_center(source_item)
    dx, dy = _element_center(dest_item)
    vx, vy, vw, vh = _monitor_info()["virtual"]
    for label, point in (("source", (sx, sy)), ("destination", (dx, dy))):
        x, y = point
        if not (vx <= x < vx + vw and vy <= y < vy + vh):
            return f"failed: {label} is outside every monitor at ({x},{y})"

    button = button if button in {"left", "right", "middle"} else "left"
    pyautogui.moveTo(sx, sy, duration=0.2)
    _HELD_MOUSE.add(button)
    try:
        pyautogui.mouseDown(button=button)
        time.sleep(0.15)
        pyautogui.moveTo(dx, dy, duration=max(0.1, min(float(duration), 3.0)))
        time.sleep(0.15)
    finally:
        try:
            pyautogui.mouseUp(button=button)
        finally:
            _HELD_MOUSE.discard(button)
    time.sleep(0.25)
    return (
        f"dragged {source_item['name']!r} ({source_item['type']}) from screen "
        f"({sx},{sy}) to {dest_item['name']!r} ({dest_item['type']}) at ({dx},{dy}) "
        f"in {source_item['window']!r}"
    )


@mcp.tool()
@_safe
def click_element(
    target: str,
    window: str = "",
    button: str = "left",
    clicks: int = 1,
    modifiers: str = "",
) -> str:
    """Click a UI element by id ("e12") or accessible name (e.g. "Save", "Cancel").

    Focuses the element's window first, then clicks the center of the live control
    rectangle. modifiers: optional ctrl, shift, alt or win, for example
    "ctrl+shift" for multi-select or range selection. Use invoke_element for
    buttons/menu items that support UI Automation invoke.
    """
    item, error = _resolve_element(target, window, _CLICKABLE)
    if item is None:
        return f"failed: {error}"
    button = button if button in {"left", "right", "middle"} else "left"
    clicks = max(1, min(int(clicks), 3))
    mods, error = _parse_modifiers(modifiers)
    if error:
        return f"failed: {error}"

    ok, message = _focus_item_window(item)
    if not ok:
        return f"failed: {message}"
    visible, note = _ensure_element_visible(item)
    if not visible:
        return f"failed: {note}"
    x, y = _element_center(item)
    vx, vy, vw, vh = _monitor_info()["virtual"]
    if not (vx <= x < vx + vw and vy <= y < vy + vh):
        return f"failed: {item['name']!r} is outside every monitor at ({x},{y})"
    try:
        for key in mods:
            pyautogui.keyDown(key)
            time.sleep(0.03)
        pyautogui.click(x, y, clicks=clicks, interval=0.08 if clicks > 1 else 0.0, button=button)
    finally:
        _release_keys(mods)
    time.sleep(0.25)
    prefix = f"{'+'.join(mods)}+" if mods else ""
    return (
        f"{prefix}clicked {item['name']!r} ({item['type']}) at screen ({x},{y}); "
        f"active={_active_window_title()!r}"
    )


@mcp.tool()
@_safe
def invoke_element(target: str, window: str = "") -> str:
    """Invoke a UI element through UI Automation (invoke/select/toggle pattern).

    Works without moving the mouse and often without focusing the window, so it is
    safer than coordinate clicks for buttons, menu items, tabs, checkboxes and list
    rows. Falls back to a real click if the control supports no pattern.
    """
    item, error = _resolve_element(target, window, _CLICKABLE)
    if item is None:
        return f"failed: {error}"

    control = item["control"]
    used = ""
    for method_name in ("invoke", "select", "toggle"):
        method = getattr(control, method_name, None)
        if method is None:
            continue
        try:
            method()
            used = method_name
            break
        except Exception:
            continue

    if not used:
        ok, message = _focus_item_window(item)
        if not ok:
            return f"failed: {message}"
        visible, note = _ensure_element_visible(item)
        if not visible:
            return f"failed: {note}"
        try:
            control.click_input()
            used = "click_input"
        except Exception as exc:  # noqa: BLE001
            return f"failed: could not invoke {item['name']!r}: {type(exc).__name__}: {exc}"

    time.sleep(0.3)
    return (
        f"invoked {item['name']!r} ({item['type']}) via {used}. "
        f"Active: {_active_window_title()!r}"
    )


@mcp.tool()
@_safe
def set_element_text(
    target: str,
    text: str,
    window: str = "",
    clear: bool = True,
    press_enter: bool = False,
) -> str:
    """Write text into an edit/document element, by id or accessible name.

    Tries the UI Automation ValuePattern first (fast, no focus stealing); if the
    control does not support it, focuses the window, clicks the control, selects all
    (when clear=True) and types real keystrokes. press_enter applies to the typing
    fallback only.
    """
    item, error = _resolve_element(target, window, _TEXTY)
    if item is None:
        return f"failed: {error}"

    value = text or ""
    used = ""
    try:
        item["control"].set_edit_text(value)
        used = "ValuePattern"
    except Exception:
        ok, message = _focus_item_window(item)
        if not ok:
            return f"failed: {message}"
        visible, note = _ensure_element_visible(item)
        if not visible:
            return f"failed: {note}"
        x, y = _element_center(item)
        pyautogui.click(x, y)
        time.sleep(0.1)
        if clear:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
        _type_text_impl(value, press_enter)
        used = "click + typing"

    time.sleep(0.2)
    current = _control_value(item["control"])
    if value and value in current:
        return f"set {item['name']!r} ({item['type']}) via {used} (verified)"
    if current:
        return (
            f"set {item['name']!r} ({item['type']}) via {used} "
            f"(current value {current[:80]!r})"
        )
    return f"set {item['name']!r} ({item['type']}) via {used} (could not verify)"


@mcp.tool()
@_safe
def read_element(target: str, window: str = "") -> str:
    """Read the current text/value of an element by id or accessible name.

    Useful to verify an edit field, label, list row or title without a screenshot.
    """
    item, error = _resolve_element(target, window, _TEXTY)
    if item is None:
        return f"failed: {error}"

    value = ""
    if item["type"] in {"Edit", "Document", "ComboBox"}:
        try:
            value = item["control"].get_value()
        except Exception:
            value = ""
    if not value:
        try:
            value = item["control"].window_text() or ""
        except Exception:
            value = ""
    left, top, width, height = item["rect"]
    return (
        f"{item['type']} {item['name']!r} at ({left},{top},{width},{height}): "
        f"{value!r}"
    )


@mcp.tool()
@_safe
def scroll_into_view(target: str, window: str = "") -> str:
    """Scroll a list/tree/grid so an element becomes visible inside its window.

    target is an eN id or accessible name. Call this when click_element reports
    that a control is scrolled out of view; click_element and drag_element also
    try it automatically once.
    """
    item, error = _resolve_element(target, window)
    if item is None:
        return f"failed: {error}"
    if _element_visible(item):
        left, top, width, height = _element_rect(item["control"])
        return (
            f"{item['name']!r} ({item['type']}) is already visible at "
            f"({left},{top},{width},{height})"
        )
    if not _scroll_into_view(item):
        return (
            f"failed: {item['name']!r} does not support ScrollItem; scroll its "
            "container with scroll(target=..., amount=...) instead"
        )
    time.sleep(0.3)
    left, top, width, height = _element_rect(item["control"])
    return (
        f"scrolled {item['name']!r} ({item['type']}) into view at "
        f"({left},{top},{width},{height}); visible={_element_visible(item)}"
    )


@mcp.tool()
@_safe
def get_element_state(target: str, window: str = "") -> str:
    """Read one element's enabled/visible/focused/checked/selected/expanded state.

    Also returns its current value and automation id. Cheaper than a screenshot
    when verifying that a checkbox, radio button, tab or combo actually changed.
    """
    item, error = _resolve_element(target, window, _CLICKABLE + _TEXTY)
    if item is None:
        return f"failed: {error}"
    control = item["control"]
    state: dict = {
        "element": item["id"],
        "name": item["name"],
        "type": item["type"],
        "window": item["window"],
        "rect": list(item["rect"]),
        "automation_id": _automation_id(control),
        "enabled": _probe(control, "is_enabled"),
        "visible": _probe(control, "is_visible"),
        "focused": _probe(control, "has_keyboard_focus"),
        "value": _control_value(control),
    }
    toggle = _probe(control, "get_toggle_state")
    if toggle is not None:
        state["toggle"] = {0: "off", 1: "on", 2: "indeterminate"}.get(toggle, toggle)
    checked = _probe(control, "is_checked")
    if checked is not None:
        state["checked"] = bool(checked)
    selected = _probe(control, "is_selected")
    if selected is not None:
        state["selected"] = bool(selected)
    expand = _probe(control, "get_expand_state")
    if expand is not None:
        state["expanded"] = {0: "collapsed", 1: "expanded", 2: "partially"}.get(expand, expand)
    try:
        state["offscreen"] = bool(control.element_info.offscreen)
    except Exception:
        pass
    state["in_window_view"] = _element_visible(item)
    return json.dumps(state, indent=2, ensure_ascii=False)


@mcp.tool()
@_safe
def select_element(target: str, item_text: str, window: str = "") -> str:
    """Choose an item in a combo box, list, tree or option group by text.

    One call instead of click → expand → find → click. Tries the control's
    select() API, expands the control and searches its items/popups, then falls
    back to typing the item text with the keyboard. Returns which method worked.
    """
    value = (item_text or "").strip()
    if not value:
        return "item_text must not be empty"
    container, error = _resolve_element(
        target, window, ("ComboBox", "List", "Tree", "ListItem", "MenuItem", "RadioButton")
    )
    if container is None:
        return f"failed: {error}"
    control = container["control"]
    used = ""

    # A ListItem/MenuItem passed directly is the item: just activate it.
    if container["type"] in {"ListItem", "MenuItem", "RadioButton", "TreeItem"}:
        for method_name in ("select", "invoke", "click_input"):
            method = getattr(control, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                used = method_name
                break
            except Exception:
                continue
        if used:
            return (
                f"selected {container['name']!r} ({container['type']}) via {used} "
                f"in {container['window']!r}"
            )

    # Direct API first: pywinauto's ComboBox/List wrappers accept item text.
    method = getattr(control, "select", None)
    if callable(method):
        try:
            method(value)
            used = "select()"
        except Exception:
            used = ""

    if not used:
        for method_name in ("expand", "click_input"):
            method = getattr(control, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                time.sleep(0.3)
                break
            except Exception:
                continue

        needle = value.casefold()
        candidates = []
        try:
            for child in control.descendants():
                kind = child.element_info.control_type or ""
                if kind not in {"ListItem", "MenuItem", "TreeItem", "RadioButton", "CheckBox"}:
                    continue
                name = (child.window_text() or "").strip()
                label = name.casefold()
                if label == needle:
                    rank = 0
                elif label.startswith(needle):
                    rank = 1
                elif needle in label:
                    rank = 2
                else:
                    continue
                candidates.append((rank, len(name), child))
        except Exception:
            pass
        if not candidates:
            try:
                for wrapper in _popup_wrappers(container["hwnd"]):
                    for child in wrapper.descendants():
                        kind = child.element_info.control_type or ""
                        if kind not in {"ListItem", "MenuItem", "TreeItem"}:
                            continue
                        name = (child.window_text() or "").strip()
                        label = name.casefold()
                        if label == needle:
                            candidates.append((0, len(name), child))
                        elif needle in label:
                            candidates.append((2, len(name), child))
            except Exception:
                pass
        candidates.sort(key=lambda row: (row[0], row[1]))
        for _, _, candidate in candidates:
            for method_name in ("select", "invoke", "click_input"):
                method = getattr(candidate, method_name, None)
                if not callable(method):
                    continue
                try:
                    method()
                    used = method_name
                    break
                except Exception:
                    continue
            if used:
                break

    if not used:
        # Keyboard fallback: focus, click the control, type the item, press Enter.
        ok, message = _focus_item_window(container)
        if not ok:
            return f"failed: {message}"
        visible, note = _ensure_element_visible(container)
        if not visible:
            return f"failed: {note}"
        x, y = _element_center(container)
        pyautogui.click(x, y)
        time.sleep(0.15)
        _type_text_impl(value, False)
        time.sleep(0.2)
        pyautogui.press("enter")
        used = "click + type + Enter"

    time.sleep(0.3)
    return (
        f"selected {value!r} in {container['name']!r} ({container['type']}) "
        f"via {used}; current value {_control_value(control)[:80]!r}"
    )


def _extract_table(container) -> tuple[list[str], list[list[str]]]:
    """Headers + rows from a UIA table/list/tree, including cell-based grids."""
    headers: list[str] = []
    try:
        for child in container.descendants():
            kind = child.element_info.control_type or ""
            if kind in {"Header", "HeaderItem", "ColumnHeader"}:
                text = (child.window_text() or child.element_info.name or "").strip()
                if text:
                    headers.append(text)
    except Exception:
        pass

    # DataGridView-style grids expose each *cell* as a DataItem whose parent is
    # a row. Group those cells by row instead of returning one row per cell.
    try:
        data_items = list(container.descendants(control_type="DataItem"))
    except Exception:
        data_items = []
    cell_mode = bool(data_items)
    if cell_mode:
        for child in data_items[:6]:
            try:
                if list(child.children()):
                    cell_mode = False
                    break
            except Exception:
                pass
    if cell_mode:
        grouped: dict[tuple, list[tuple[int, str]]] = {}
        for child in data_items:
            value = _control_value(child)
            if not value:
                try:
                    value = (child.window_text() or "").strip()
                except Exception:
                    value = ""
            if not value:
                continue
            try:
                parent = child.parent()
                key = (
                    parent.rectangle().top,
                    parent.window_text() or parent.element_info.name or "",
                )
            except Exception:
                key = (0, "")
            try:
                left, _, _, _ = _element_rect(child)
            except Exception:
                left = 0
            grouped.setdefault(key, []).append((left, value))
        if grouped:
            rows = [
                [value for _, value in sorted(cells, key=lambda cell: cell[0])]
                for _, cells in sorted(grouped.items(), key=lambda row: row[0])
            ]
            return headers, rows

    rows: list[list[str]] = []
    for row in _row_controls(container):
        values = _control_texts(row)
        if len(values) <= 1:
            try:
                name = (row.window_text() or "").strip()
            except Exception:
                name = ""
            if name and (not values or values[0] != name):
                values = [name]
        if values:
            rows.append(values)
    if rows:
        return headers, rows

    cells = []
    try:
        for child in container.descendants():
            kind = child.element_info.control_type or ""
            if kind not in {"Edit", "Text", "DataItem", "ListItem"}:
                continue
            text = _control_value(child) or (child.window_text() or "").strip()
            if not text:
                continue
            left, top, width, height = _element_rect(child)
            cells.append((top, left, text, height))
    except Exception:
        cells = []
    cells.sort(key=lambda row: (row[0], row[1]))
    grouped_rows: list[list[tuple[int, str]]] = []
    for top, left, text, height in cells:
        if not grouped_rows or abs(grouped_rows[-1][0][0] - top) > max(6, height // 2):
            grouped_rows.append([(top, left, text)])
        else:
            grouped_rows[-1].append((top, left, text))
    for group in grouped_rows:
        rows.append([cell[2] for cell in sorted(group, key=lambda cell: cell[1])])
    return headers, rows


@mcp.tool()
@_safe
def read_table(
    target: str = "",
    window: str = "",
    max_rows: int = 50,
    offset: int = 0,
    format: str = "tsv",
) -> str:
    """Extract rows from a table, grid, list or tree as TSV or JSON.

    target may be a table/list element (eN id or name). Empty target uses the
    first Table/DataGrid/List/Tree in `window` (or the active window). Handles
    UIA DataItem/ListItem/TreeItem rows and falls back to grouping cell text by
    row when the app exposes cells but no row wrappers. max_rows caps output;
    offset skips leading rows. This is much cheaper than reading a large grid
    row by row with read_element.
    """
    fmt = (format or "tsv").strip().lower()
    if fmt not in {"tsv", "json"}:
        return "format must be tsv or json"
    max_rows = max(1, min(int(max_rows), 500))
    offset = max(0, int(offset))
    container = None
    label = ""
    if target.strip():
        item, error = _resolve_element(
            target, window, ("Table", "DataGrid", "List", "Tree", "Pane")
        )
        if item is None:
            return f"failed: {error}"
        container = item["control"]
        label = f"{item['name']!r} ({item['type']}) in {item['window']!r}"
    else:
        result = _build_ui_snapshot(window, 400)
        if "error" in result:
            return "read_table: " + result["error"]
        for item in result["items"].values():
            if item["type"] in {"Table", "DataGrid", "List", "Tree"}:
                container = item["control"]
                label = f"{item['name']!r} ({item['type']}) in {result['window']!r}"
                break
        if container is None:
            return (
                "read_table: no Table/DataGrid/List/Tree in "
                f"{result['window']!r}; pass target=<element> explicitly"
            )

    headers, all_rows = _extract_table(container)
    table = all_rows[offset : offset + max_rows]
    total = len(all_rows)
    header = (
        f"{label}: {total} row(s)"
        + (f", showing {offset + 1}-{offset + len(table)}" if table else "")
        + (f", columns={len(headers)}" if headers else "")
    )
    if fmt == "json":
        return json.dumps(
            {"table": label, "headers": headers, "rows": table, "offset": offset},
            indent=2,
            ensure_ascii=False,
        )
    lines = [header]
    if headers:
        lines.append("\t".join(headers))
    lines.extend("\t".join(row) for row in table)
    if not table:
        lines.append("(no rows)")
    return "\n".join(lines)


def _popup_menu_items(name: str, main_hwnd: int, limit: int = 12):
    """Menu items in same-process popup windows (#32768, WinForms drop-downs)."""
    needle = name.strip().lower()
    found = []
    for wrapper in _popup_wrappers(main_hwnd):
        try:
            for child in wrapper.descendants(control_type="MenuItem"):
                label = (child.window_text() or "").strip().lower()
                if label == needle:
                    found.append((0, len(label), child))
                elif label and needle in label:
                    found.append((2, len(label), child))
        except Exception:
            continue
    found.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in found[:limit]]


def _find_menu_item(name: str, main_hwnd: int):
    """Find a MenuItem by exact/prefix/substring name in the window or its popups."""
    needle = name.strip().lower()
    matches = []
    try:
        root = _uia_desktop().window(handle=main_hwnd)
        for child in root.descendants(control_type="MenuItem"):
            label = (child.window_text() or "").strip().lower()
            if label == needle:
                matches.append((0, len(label), child))
            elif label.startswith(needle):
                matches.append((1, len(label), child))
            elif needle in label:
                matches.append((2, len(label), child))
    except Exception:
        pass
    if matches:
        matches.sort(key=lambda row: (row[0], row[1]))
        return matches[0][2]
    popups = _popup_menu_items(name, main_hwnd)
    return popups[0] if popups else None


@mcp.tool()
@_safe
def menu(path: str, window: str = "") -> str:
    """Navigate a classic menu bar by path, e.g. "File > Save As".

    Each segment is matched by exact/prefix/substring name. Opens submenus and
    searches popup menu windows, so it works with WinForms/Win32 apps where the
    expanded items are not descendants of the main window. The last segment is
    invoked. Use click_element instead when the menu item is already visible in
    the accessibility tree.
    """
    segments = [part.strip() for part in re.split(r"\s*(?:>|->|\|)\s*", path or "") if part.strip()]
    if len(segments) < 2:
        return "path must look like \"File > Save As\" (at least two segments)"
    target = _pick_window(window)
    if not target:
        return f"failed: no visible window title contains {window!r}"
    ok, message = _focus_hwnd(
        target["hwnd"], target["title"], target["offscreen"], target["minimized"]
    )
    if not ok:
        return f"failed: {message}"

    clicked: list[str] = []
    for index, segment in enumerate(segments):
        control = _find_menu_item(segment, target["hwnd"])
        if control is None:
            return (
                f"failed: no menu item {segment!r} after "
                f"{' > '.join(clicked) or '(menu bar)'}; open the menu once with "
                "click_element and use snapshot/find_elements to see its items"
            )
        used = ""
        if index < len(segments) - 1:
            for method_name in ("click_input", "invoke", "select"):
                method = getattr(control, method_name, None)
                if not callable(method):
                    continue
                try:
                    method()
                    used = method_name
                    break
                except Exception:
                    continue
        else:
            for method_name in ("invoke", "click_input", "select"):
                method = getattr(control, method_name, None)
                if not callable(method):
                    continue
                try:
                    method()
                    used = method_name
                    break
                except Exception:
                    continue
        if not used:
            return f"failed: found {segment!r} but could not activate it"
        clicked.append(segment)
        time.sleep(0.35)

    return f"menu: {' > '.join(clicked)} (activated in {target['title']!r})"


def _file_dialog_score(result: dict) -> tuple[int, dict | None, dict | None]:
    edits = [
        item
        for item in result.get("items", {}).values()
        if item["type"] in {"Edit", "ComboBox"}
    ]
    buttons = [
        item for item in result.get("items", {}).values() if item["type"] == "Button"
    ]
    # The file-name control is a ComboBox wrapping an Edit with the same
    # automation id. The Edit supports ValuePattern, so prefer it.
    def edit_rank(item: dict) -> tuple[int, int]:
        automation = _automation_id(item["control"])
        named = any(
            hint in item["name"].lower()
            for hint in ("file name", "folder", "name:")
        )
        return (
            0 if automation in {"1148", "1001"} or named else 1,
            0 if item["type"] == "Edit" else 1,
        )

    file_edit = min(edits, key=edit_rank) if edits else None
    # Prefer the dialog's default button (automation id 1). An "Open" split
    # button also exposes a drop-down arrow whose id is "DropDown"; invoking
    # that only opens a menu.
    confirm = next(
        (item for item in buttons if _automation_id(item["control"]) == "1"),
        None,
    )
    if confirm is None:
        confirm = next(
            (
                item
                for item in buttons
                if item["name"].strip().lower()
                in {"open", "save", "select folder", "choose", "ok"}
                and _automation_id(item["control"]).lower() != "dropdown"
                and "drop-down" not in item["name"].lower()
                and "dropdown" not in item["name"].lower()
            ),
            None,
        )
    score = 0
    if file_edit is not None:
        score += 2
    if confirm is not None:
        score += 2
    title = result.get("window", "").lower()
    if any(word in title for word in ("open", "save", "select", "choose", "browse", "folder")):
        score += 1
    return score, file_edit, confirm


@mcp.tool()
@_safe
def file_dialog(
    path: str,
    action: str = "open",
    window: str = "",
    confirm: bool = True,
) -> str:
    """Fill and confirm a standard Open/Save/Select-Folder dialog.

    Uses the dialog's automation ids (file-name box 1148, default button 1) so it
    works on localized Windows. `path` may be a full file path or a folder for
    action="select_folder". Set confirm=False to only fill the box. When window
    is empty, the active window and visible #32770 dialogs are scored; a dialog
    with a file-name edit and a default button wins.
    """
    action = (action or "open").strip().lower()
    if action not in {"open", "save", "select_folder"}:
        return "action must be open, save or select_folder"
    if not path.strip():
        return "path must not be empty"

    candidates = []
    if window.strip():
        picked = _pick_window(window)
        if not picked:
            return f"failed: no visible window title contains {window!r}"
        candidates = [picked["title"]]
    else:
        active = _active_window_title()
        if active:
            candidates.append(active)
        for entry in _list_windows():
            title = entry["title"]
            if title in candidates:
                continue
            if _window_class(entry["hwnd"]) == "#32770" or any(
                word in title.lower()
                for word in ("open", "save", "select", "choose", "browse", "folder")
            ):
                candidates.append(title)
        if not candidates:
            return "failed: no file dialog found (pass window=<title>)"

    chosen = None
    for title in candidates[:8]:
        result = _build_ui_snapshot(title, 300)
        if "error" in result:
            continue
        score, file_edit, confirm_button = _file_dialog_score(result)
        if score >= 3:
            chosen = (result, file_edit, confirm_button)
            break
    if chosen is None:
        return (
            "failed: no visible dialog looked like a file dialog (need a file-name "
            "edit and an Open/Save button); pass window=<exact title>"
        )

    result, file_edit, confirm_button = chosen
    if file_edit is None:
        return f"failed: no file-name edit in {result['window']!r}"
    value = path.strip().strip('"')
    used = ""
    try:
        file_edit["control"].set_edit_text(value)
        used = "ValuePattern"
    except Exception:
        ok, message = _focus_item_window(file_edit)
        if not ok:
            return f"failed: {message}"
        visible, note = _ensure_element_visible(file_edit)
        if not visible:
            return f"failed: {note}"
        x, y = _element_center(file_edit)
        pyautogui.click(x, y)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        _type_text_impl(value, False)
        used = "click + typing"
    time.sleep(0.2)
    if not confirm:
        return (
            f"filled {result['window']!r} file name with {value!r} via {used} "
            "(not confirmed)"
        )

    if confirm_button is not None:
        invoked = ""
        for method_name in ("invoke", "click_input"):
            method = getattr(confirm_button["control"], method_name, None)
            if not callable(method):
                continue
            try:
                method()
                invoked = method_name
                break
            except Exception:
                continue
        if not invoked:
            return f"failed: could not press {confirm_button['name']!r}"
    else:
        pyautogui.press("enter")
        invoked = "enter key"
    time.sleep(0.4)
    return (
        f"{action}: {result['window']!r} file name {value!r} via {used}, "
        f"confirmed via {invoked}"
    )


def _ocr_matches(text: str, shot, rect: tuple[int, int, int, int], exact: bool) -> list[dict]:
    needle = text.strip().casefold()
    matches = []
    for line in _ocr_lines(shot):
        candidate = line["text"].strip()
        haystack = candidate.casefold()
        if (exact and haystack == needle) or (not exact and needle in haystack):
            scale_x = rect[2] / max(1, shot.size[0])
            scale_y = rect[3] / max(1, shot.size[1])
            left = rect[0] + int(round(line["left"] * scale_x))
            top = rect[1] + int(round(line["top"] * scale_y))
            width = int(round(line["width"] * scale_x))
            height = int(round(line["height"] * scale_y))
            matches.append(
                {
                    "text": candidate,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "confidence": line["confidence"],
                }
            )
    return matches


@mcp.tool()
@_safe
def find_text(
    text: str,
    window: str = "",
    monitor: int = 0,
    region: list[int] | None = None,
    max_results: int = 20,
    exact: bool = False,
) -> str:
    """Locate visible text with local OCR and return its screen rectangle.

    The fallback for canvas/Electron/game/remote-desktop content where UI
    Automation has no useful tree. Requires the optional Tesseract install (no
    model or API key); runtime_status reports whether it is available. Searches
    a window, a region [left,top,right,bottom], or a monitor. exact=True matches
    the whole OCR line; the default matches a case-insensitive substring.
    """
    needle = (text or "").strip()
    if not needle:
        return "text must not be empty"
    if not _tesseract_exe():
        return (
            "OCR unavailable: install Tesseract OCR (or set TESSERACT_CMD) to use "
            "find_text/click_text. UI Automation tools work without it."
        )
    try:
        shot, rect, label = _capture_source(window, monitor, region)
    except RuntimeError as exc:
        return f"failed: {exc}"
    matches = _ocr_matches(needle, shot, rect, bool(exact))[: max(1, min(int(max_results), 50))]
    if not matches:
        preview = ", ".join(line["text"] for line in _ocr_lines(shot)[:8])
        return (
            f"no OCR text matching {text!r} in {label}; visible text: "
            f"{preview or '(none recognized)'}"
        )
    lines = [f"{len(matches)} OCR match(es) for {text!r} in {label}:"]
    for index, match in enumerate(matches, 1):
        lines.append(
            f"{index}. {match['text']!r} at screen "
            f"({match['left']},{match['top']},{match['width']},{match['height']}) "
            f"confidence={match['confidence']}"
        )
    return "\n".join(lines)


@mcp.tool()
@_safe
def click_text(
    text: str,
    window: str = "",
    index: int = 0,
    monitor: int = 0,
    region: list[int] | None = None,
    exact: bool = False,
) -> str:
    """OCR the screen and click the center of matching text.

    Use when an app exposes no accessible controls. index selects among multiple
    matches (0 = first, top-to-bottom). Requires Tesseract. Prefer
    click_element/invoke_element when a UIA tree is available: OCR can match
    visually similar text and cannot see hidden controls.
    """
    needle = (text or "").strip()
    if not needle:
        return "text must not be empty"
    if not _tesseract_exe():
        return (
            "OCR unavailable: install Tesseract OCR (or set TESSERACT_CMD) to use "
            "click_text. UI Automation tools work without it."
        )
    try:
        shot, rect, label = _capture_source(window, monitor, region)
    except RuntimeError as exc:
        return f"failed: {exc}"
    matches = _ocr_matches(needle, shot, rect, bool(exact))
    if not matches:
        return f"failed: no OCR text matching {text!r} in {label}"
    if index < 0 or index >= len(matches):
        return f"failed: index {index} out of range ({len(matches)} match(es))"
    match = matches[index]
    if window.strip():
        target = _pick_window(window)
        if target:
            _focus_hwnd(
                target["hwnd"], target["title"], target["offscreen"], target["minimized"]
            )
    x = match["left"] + match["width"] // 2
    y = match["top"] + match["height"] // 2
    vx, vy, vw, vh = _monitor_info()["virtual"]
    if not (vx <= x < vx + vw and vy <= y < vy + vh):
        return f"failed: OCR match is outside every monitor at ({x},{y})"
    pyautogui.click(x, y)
    time.sleep(0.25)
    return (
        f"clicked OCR text {match['text']!r} at screen ({x},{y}) "
        f"(confidence {match['confidence']}) in {label}; "
        f"active={_active_window_title()!r}"
    )


@mcp.tool()
@_safe
def highlight_element(target: str, window: str = "", ms: int = 900) -> str:
    """Flash a temporary red border around an element so a human can see it.

    Useful in pair mode and for verification before a risky action. The overlay
    is click-through, topmost and disappears after `ms` milliseconds. It changes
    nothing about the target.
    """
    item, error = _resolve_element(target, window)
    if item is None:
        return f"failed: {error}"
    left, top, width, height = _element_rect(item["control"])
    vx, vy, vw, vh = _monitor_info()["virtual"]
    if not (
        vx <= left < vx + vw
        and vy <= top < vy + vh
        and left + width > vx
        and top + height > vy
    ):
        return f"failed: {item['name']!r} is outside every monitor at ({left},{top})"
    duration = max(200, min(int(ms), 5000))
    threading.Thread(
        target=_flash_rect,
        args=(left, top, width, height, duration),
        name="highlight-element",
        daemon=True,
    ).start()
    return (
        f"highlighted {item['name']!r} ({item['type']}) at "
        f"({left},{top},{width},{height}) for {duration}ms"
    )


@mcp.tool()
@_safe
def wait_for_window(title: str, timeout: float = 10.0, state: str = "appear") -> str:
    """Wait for a visible window to appear or close.

    state="appear" (default) waits for a title match; state="close" waits until no
    visible window matches. Returns the window details, or a timeout listing what
    is visible. Much better than a fixed sleep after launch or a slow dialog.
    """
    needle = (title or "").strip().lower()
    if not needle:
        return "title must not be empty"
    state = (state or "appear").strip().lower()
    if state not in {"appear", "close"}:
        return "state must be appear or close"
    timeout = max(0.2, min(float(timeout), 120.0))
    start = time.monotonic()
    seen: dict | None = None
    while True:
        matches = [w for w in _list_windows() if needle in w["title"].lower()]
        ranked = _rank_title_matches(matches, needle) if matches else []
        elapsed = time.monotonic() - start
        if state == "appear" and ranked:
            window = ranked[0]
            return (
                f"appeared after {elapsed:.1f}s: {window['title']!r} "
                f"hwnd={window['hwnd']} rect={window['rect']}"
            )
        if state == "close":
            if ranked:
                seen = ranked[0]
            else:
                if seen:
                    return (
                        f"closed after {elapsed:.1f}s: {seen['title']!r} "
                        "is no longer visible"
                    )
                return (
                    f"already closed after {elapsed:.1f}s: no visible window title "
                    f"contains {title!r}"
                )
        if elapsed >= timeout:
            if state == "close":
                visible = ranked[0]["title"] if ranked else "(none)"
                return (
                    f"timed out after {timeout:g}s waiting for {title!r} to close; "
                    f"still visible: {visible}"
                )
            visible = ", ".join(w["title"] for w in _list_windows()[:6])
            return (
                f"timed out after {timeout:g}s waiting for {title!r}; "
                f"visible: {visible or '(none)'}"
            )
        time.sleep(0.25)


@mcp.tool()
@_safe
def wait_for_element(
    target: str,
    window: str = "",
    timeout: float = 10.0,
    state: str = "appear",
    must_exist: bool = False,
) -> str:
    """Wait for an element to appear, close, become enabled or become disabled.

    target is an accessible name (or an eN id from the latest tree; names are
    safer while the tree is changing). state="appear" (default), "close",
    "enabled" or "disabled". Rebuilds the accessibility tree while polling, so a
    successful wait also leaves a fresh element cache ready for subsequent
    click_element/invoke_element/set_element_text calls. Pass window for dialogs:
    it keeps the search in the right window while the UI changes. state="close"
    with must_exist=True first waits for the element to appear, so a target that
    was never there is a timeout instead of a false success.
    """
    needle = (target or "").strip()
    if not needle:
        return "target must not be empty"
    state = (state or "appear").strip().lower()
    if state not in {"appear", "close", "enabled", "disabled"}:
        return "state must be appear, close, enabled or disabled"
    timeout = max(0.2, min(float(timeout), 120.0))
    start = time.monotonic()
    last_error = ""
    seen: dict | None = None
    # Resolve an eN id to stable properties before polling: ids are renumbered
    # every time the tree is rebuilt, so the literal "e7" may point elsewhere.
    original = _UI_CACHE["items"].get(needle.lower()) if re.fullmatch(r"e\d+", needle, re.I) else None
    stable_name = original["name"] if original else needle
    stable_type = original["type"] if original else ""
    while True:
        result = _build_ui_snapshot(window, 160)
        elapsed = time.monotonic() - start
        if "error" in result:
            last_error = result["error"]
            if state == "close" and result["error"].startswith("no visible window matching"):
                return (
                    f"closed after {elapsed:.1f}s: no visible window matching "
                    f"{window!r} remains"
                )
        else:
            item = None
            if original is not None:
                item = next(
                    (
                        candidate
                        for candidate in _UI_CACHE["items"].values()
                        if candidate["name"] == stable_name
                        and candidate["type"] == stable_type
                    ),
                    None,
                )
            if item is None:
                item = _find_in_cache(stable_name)
            if item is not None:
                seen = item
                left, top, width, height = item["rect"]
                label = (
                    f"{item['id']} {item['type']} {item['name']!r} "
                    f"at ({left},{top},{width},{height}) in {result['window']!r}"
                )
                if state == "appear":
                    return f"found after {elapsed:.1f}s: {label}"
                if state == "enabled":
                    enabled = _element_enabled(item)
                    if enabled is True:
                        return f"enabled after {elapsed:.1f}s: {label}"
                elif state == "disabled":
                    if _element_enabled(item) is False:
                        return f"disabled after {elapsed:.1f}s: {label}"
            elif state == "close":
                if seen:
                    return (
                        f"disappeared after {elapsed:.1f}s: {seen['name']!r} "
                        f"({seen['type']}) is no longer in {result['window']!r}"
                    )
                if not must_exist:
                    return (
                        f"already absent after {elapsed:.1f}s: no element matching "
                        f"{target!r} in {result['window']!r} (it was not present at "
                        "the start; pass must_exist=true to require it to appear first)"
                    )
        if elapsed >= timeout:
            detail = f"; last error: {last_error}" if last_error else ""
            if state == "close":
                if must_exist and seen is None:
                    return (
                        f"timed out after {timeout:g}s waiting for {target!r} to "
                        f"appear and close; it was never present{detail}"
                    )
                return (
                    f"timed out after {timeout:g}s waiting for {target!r} to close; "
                    f"it is still present{detail}"
                )
            if state in {"enabled", "disabled"}:
                state_note = ""
                if seen is not None and _element_enabled(seen) is None:
                    state_note = " (its enabled state could not be read)"
                return (
                    f"timed out after {timeout:g}s waiting for {target!r} to become "
                    f"{state}{state_note}{detail}"
                )
            return f"timed out after {timeout:g}s waiting for {target!r}{detail}"
        time.sleep(0.4)


def _window_signature() -> tuple:
    return tuple(
        sorted(
            (
                window["hwnd"],
                window["title"],
                tuple(window["rect"]),
                bool(window["minimized"]),
                bool(window["offscreen"]),
            )
            for window in _list_windows()
        )
    )


@mcp.tool()
@_safe
def wait_for_idle(
    window: str = "",
    idle_ms: int = 800,
    timeout: float = 10.0,
    include_tree: bool = True,
) -> str:
    """Wait until windows (and optionally the element tree) stop changing.

    A deterministic replacement for arbitrary sleeps after launch, save,
    navigation or dialog transitions. Returns as soon as the signature has been
    stable for idle_ms, or reports what is still changing at the timeout.
    include_tree=False only watches the window list, which is cheaper.
    """
    idle_ms = max(200, min(int(idle_ms), 10000))
    timeout = max(0.3, min(float(timeout), 120.0))
    start = time.monotonic()
    last_signature = None
    last_change = start
    tree_count = 0
    while True:
        signature = _window_signature()
        if include_tree:
            result = _build_ui_snapshot(window, 120)
            if "error" not in result:
                tree_count = result["count"]
                signature += tuple(
                    (item["type"], item["name"], tuple(item["rect"]))
                    for item in result["items"].values()
                )
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            last_change = now
        elapsed = now - start
        stable_ms = int((now - last_change) * 1000)
        if stable_ms >= idle_ms:
            return (
                f"idle after {elapsed:.1f}s (stable {stable_ms}ms): "
                f"{len(_list_windows())} window(s)"
                + (f", {tree_count} control(s)" if include_tree else "")
            )
        if elapsed >= timeout:
            return (
                f"timed out after {timeout:g}s: still changing (last change "
                f"{stable_ms}ms ago); {len(_list_windows())} window(s)"
            )
        time.sleep(0.2)


@mcp.tool()
@_safe
def wait_for_change(
    window: str = "",
    monitor: int = 0,
    region: list[int] | None = None,
    timeout: float = 10.0,
    threshold: float = 0.2,
    interval: float = 0.3,
) -> str:
    """Wait until a window, monitor or region visibly changes.

    threshold is the percentage of pixels that must change (default 0.2%, good
    for spinners, progress bars, page loads and charts). Returns the changed
    screen rectangle. For "has it finished changing", use wait_for_idle instead.
    """
    timeout = max(0.3, min(float(timeout), 120.0))
    threshold = max(0.0, min(float(threshold), 100.0))
    interval = max(0.1, min(float(interval), 5.0))
    try:
        baseline, rect, label = _capture_source(window, monitor, region)
    except RuntimeError as exc:
        return f"failed: {exc}"
    baseline = baseline.convert("RGB")
    start = time.monotonic()
    total_pixels = max(1, baseline.size[0] * baseline.size[1])
    while True:
        time.sleep(interval)
        try:
            current, _, _ = _capture_source(window, monitor, region)
        except RuntimeError as exc:
            return f"failed: {exc}"
        current = current.convert("RGB")
        elapsed = time.monotonic() - start
        if current.size != baseline.size:
            return (
                f"changed after {elapsed:.1f}s: the captured size changed from "
                f"{baseline.size} to {current.size} in {label}"
            )
        from PIL import ImageChops

        diff = ImageChops.difference(baseline, current)
        mask = diff.convert("L").point(lambda value: 255 if value > 8 else 0)
        changed = sum(mask.histogram()[1:])
        percent = changed * 100.0 / total_pixels
        if percent >= threshold:
            box = mask.getbbox()
            if box:
                scale_x = rect[2] / max(1, baseline.size[0])
                scale_y = rect[3] / max(1, baseline.size[1])
                screen_box = (
                    rect[0] + int(round(box[0] * scale_x)),
                    rect[1] + int(round(box[1] * scale_y)),
                    rect[0] + int(round(box[2] * scale_x)),
                    rect[1] + int(round(box[3] * scale_y)),
                )
            else:
                screen_box = (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
            return (
                f"changed after {elapsed:.1f}s: {percent:.2f}% of pixels in "
                f"{label}; screen bbox={screen_box}"
            )
        if elapsed >= timeout:
            return (
                f"no significant change after {timeout:g}s in {label} "
                f"(threshold {threshold:g}%, last {percent:.2f}%)"
            )


@mcp.tool()
@_safe
def pause(action: str = "toggle") -> str:
    """Pause or resume all desktop automation (also Ctrl+Alt+F12).

    While paused, every action except pause/release_all/status/read-only
    inspection is refused, so a human can take over safely. Pausing releases any
    keys or mouse buttons held by key_down/mouse_button/drag.
    """
    global _PAUSED
    action = (action or "toggle").strip().lower()
    if action in {"on", "pause", "true", "1"}:
        _PAUSED = True
    elif action in {"off", "resume", "false", "0"}:
        _PAUSED = False
    elif action == "toggle":
        _PAUSED = not _PAUSED
    elif action not in {"status", "get"}:
        return "action must be on, off, toggle or status"
    if _PAUSED:
        _release_held()
    return f"automation {'paused' if _PAUSED else 'running'}" + (
        " (Ctrl+Alt+F12 toggles)" if action != "status" else ""
    )


@mcp.tool()
@_safe
def release_all() -> str:
    """Release every key and mouse button held through this server.

    Safety valve after a custom key_down/mouse_button sequence or an interrupted
    drag. Equivalent to pressing/releasing the stuck input, not a global reset.
    """
    keys = list(_HELD_KEYS)
    buttons = sorted(_HELD_MOUSE)
    _release_held()
    return f"released {len(keys)} key(s) and {len(buttons)} mouse button(s)"


@mcp.tool()
@_safe
def window_action(
    title: str,
    action: str = "activate",
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Act on a whole window: activate|minimize|maximize|restore|move|resize|move_resize.

    x/y/width/height are screen pixels and only used by move/resize/move_resize;
    omit them to keep the current value. Negative x/y are valid: monitors can sit
    to the left of or above the primary display. Returns the resulting rectangle.
    """
    target = _pick_window(title)
    if not target:
        return f"failed: no visible window title contains {title!r}"
    hwnd = wintypes.HWND(target["hwnd"])
    u = _user32()
    action = (action or "activate").strip().lower()

    if action in {"activate", "focus"}:
        ok, message = _focus_hwnd(
            target["hwnd"], target["title"], target["offscreen"], target["minimized"]
        )
        return ("ok: " if ok else "failed: ") + message
    if action in {"minimize", "maximize", "restore"}:
        commands = {"minimize": 6, "maximize": 3, "restore": 9}  # SW_MINIMIZE/MAXIMIZE/RESTORE
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        u.ShowWindow(hwnd, commands[action])
        time.sleep(0.25)
        # ShowWindow returns the previous visibility, not success, so verify state.
        minimized_now = bool(u.IsIconic(hwnd))
        maximized_now = bool(u.IsZoomed(hwnd))
        ok = (
            (action == "minimize" and minimized_now)
            or (action == "maximize" and maximized_now)
            or (action == "restore" and not minimized_now and not maximized_now)
        )
        if not ok:
            state = (
                "minimized" if minimized_now
                else "maximized" if maximized_now
                else "restored"
            )
            return f"failed: asked to {action} {target['title']!r} but it is {state}"
    elif action in {"move", "resize", "move_resize"}:
        left, top, current_width, current_height = _window_rect(target["hwnd"])
        # None means "keep current". Do not use -1 as the sentinel: it is a real
        # coordinate on a monitor positioned left of or above the primary display.
        new_x = left if x is None else int(x)
        new_y = top if y is None else int(y)
        new_width = current_width if width is None or width <= 0 else int(width)
        new_height = current_height if height is None or height <= 0 else int(height)
        # SWP_NOZORDER | SWP_NOACTIVATE
        if not u.SetWindowPos(hwnd, 0, new_x, new_y, new_width, new_height, 0x0004 | 0x0010):
            return f"failed: SetWindowPos returned 0 (error {ctypes.get_last_error()})"
    else:
        return "action must be activate, minimize, maximize, restore, move, resize or move_resize"

    time.sleep(0.3)
    left, top, current_width, current_height = _window_rect(target["hwnd"])
    return (
        f"{action}: {target['title']!r} -> "
        f"({left},{top},{current_width},{current_height})"
    )


@mcp.tool()
@_safe
def runtime_status() -> str:
    """Report native runtime readiness: screen, input desktop, DPI, elevation, OCR, lease."""
    lease = bool(os.environ.get(PARENT_ENV))
    return json.dumps(
        {
            "python": sys.executable,
            "platform": platform.platform(),
            "screen": list(pyautogui.size()),
            "monitors": _monitors(),
            "active_window": _active_window_title(),
            "screenshot_mapping": _shot,
            "session_token": "active",
            "launcher_lease": "active" if lease else "not configured",
            "read_only": _read_only(),
            "paused": _PAUSED,
            "health": _health(),
        },
        indent=2,
    )


def _log_startup() -> None:
    """Append a line to .runtime/logs/sessions.log so the launcher can be verified."""
    lease = os.environ.get(PARENT_ENV, "-")
    _log_event(f"started  launcher={lease}")


if __name__ == "__main__":
    _log_startup()
    _start_hotkey()
    sys.stderr.write("[computer-use] MCP server up (session token ok)\n")
    mcp.run("stdio")
