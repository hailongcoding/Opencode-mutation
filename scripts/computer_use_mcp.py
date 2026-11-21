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
        "wait", "clipboard", "runtime_status",
    }
)


def _read_only_block() -> str:
    return (
        "BLOCKED: this Computer-Use session is read-only. Inspection tools "
        "(snapshot, ui_tree, screenshot, find_elements, read_element, "
        "list_windows, waits, runtime_status, clipboard get) are available, "
        "but every mouse, keyboard, window and launch action is disabled."
    )


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _parent_alive():
            _release_held()
            return (
                "DETACHED: the Computer-Use launcher that created this session is no longer running. "
                "Start Computer-Use.cmd again for a new session."
            )
        if _read_only() and fn.__name__ not in _READ_ONLY_TOOLS:
            return _read_only_block()
        try:
            return fn(*args, **kwargs)
        except pyautogui.FailSafeException:
            return (
                "ABORTED: pyautogui failsafe triggered (mouse at a screen corner). "
                "The user may have interrupted automation. Nothing else was done."
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return f"ERROR in {fn.__name__}: {type(exc).__name__}: {exc}"

    return wrapper


def _to_screen(x: float, y: float) -> tuple[int, int]:
    """Map coordinates from the last screenshot's pixel space to screen pixels."""
    if _shot:
        return (
            _shot["origin_x"] + int(round(x * _shot["screen_w"] / _shot["img_w"])),
            _shot["origin_y"] + int(round(y * _shot["screen_h"] / _shot["img_h"])),
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
    "Menu", "MenuBar", "ToolBar", "ScrollBar", "Text",
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
                    continue
                if not label and kind not in _INTERACTIVE:
                    continue
                eid = f"e{len(rows) + 1}"
                items[eid] = {
                    "id": eid,
                    "control": control,
                    "name": label,
                    "type": kind,
                    "rect": (left, top, width, height),
                    "window": name,
                    "hwnd": target_info["hwnd"],
                }
                rows.append(f"{eid:<4} {kind:<13} [{left},{top},{width},{height}] {label}"[:190])
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
    name = item["name"].lstrip("* ").lower()
    if not name:
        return 9
    if name == needle:
        return 0
    if name.startswith(needle):
        return 1
    if needle in name:
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
(and elements also `enabled` / `disabled`) instead of fixed sleeps. Holds:
`key_down` / `key_up` (or `hold_key`) for custom modifier sequences, `mouse_button`
for custom drags. Window control: `window_action` (activate/minimize/maximize/
restore/move/resize). `launch` can take arguments and wait for a window.

Multiple monitors: `screenshot(monitor=0)` is the primary; `screen_info` lists the
layout; `monitor=-1` captures everything. `screenshot(window="Title")` captures one
window even when it is behind another. Use the display that shows the target, or
clicks will land on the wrong screen.

Safety: the pointer corner aborts pyautogui. Prefer `close_window(title)` over a
blind Alt+F4: it asks one specific window to close and never touches another.
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
        rect = _window_rect(hwnd)
        try:
            shot = _uia_desktop().window(handle=hwnd).capture_as_image()
        except Exception:
            if rect[2] <= 0 or rect[3] <= 0:
                return [f"window {target['title']!r} is minimized or has no drawable area"]
            shot = _grab(rect)
        screen_w, screen_h = (rect[2], rect[3]) if rect[2] > 0 and rect[3] > 0 else shot.size
        origin_x, origin_y = rect[0], rect[1]
        region = (origin_x, origin_y, origin_x + max(0, screen_w), origin_y + max(0, screen_h))
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
) -> list:
    """One-call look: window + accessible element tree (eN ids) + a screenshot.

    This is the preferred first call: it gives you both the deterministic element
    ids and a visual overview in one round trip. `window` is a title substring;
    empty means the active window. Set screenshot=False for a text-only refresh.
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
    rect = _window_rect(hwnd)
    if _user32().IsIconic(hwnd):
        return [text + "\nScreenshot unavailable: the window is minimized; restore it first."]
    try:
        shot = _uia_desktop().window(handle=hwnd).capture_as_image()
    except Exception:
        if rect[2] <= 0 or rect[3] <= 0:
            return [text + "\nScreenshot unavailable: the window is minimized."]
        shot = _grab(rect)
        rect = rect if rect[2] > 0 else (0, 0, *shot.size)

    screen_w, screen_h = (rect[2], rect[3]) if rect[2] > 0 and rect[3] > 0 else shot.size
    max_width = max(320, min(int(max_width), 3840))
    if shot.size[0] > max_width:
        height = int(round(shot.size[1] * max_width / shot.size[0]))
        shot = shot.resize((max_width, height), PILImage.LANCZOS)

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
            name = item["name"].lstrip("* ").lower()
            if name == needle:
                rank = 0
            elif name.startswith(needle):
                rank = 1
            elif needle in name:
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
        rows.append(
            f"{item['id']:<4} {item['type']:<13} "
            f"[{left},{top},{width},{height}] {item['name']}{suffix}"
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
        x, y = _element_center(item)
        pyautogui.click(x, y)
        time.sleep(0.1)
        if clear:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
        _type_text_impl(value, press_enter)
        used = "click + typing"

    time.sleep(0.2)
    current = ""
    try:
        current = item["control"].window_text() or ""
    except Exception:
        pass
    verified = " (verified)" if value and value in current else ""
    return f"set {item['name']!r} ({item['type']}) via {used}{verified}"


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
) -> str:
    """Wait for an element to appear, close, become enabled or become disabled.

    target is an accessible name (or an eN id from the latest tree; names are
    safer while the tree is changing). state="appear" (default), "close",
    "enabled" or "disabled". Rebuilds the accessibility tree while polling, so a
    successful wait also leaves a fresh element cache ready for subsequent
    click_element/invoke_element/set_element_text calls. Pass window for dialogs:
    it keeps the search in the right window while the UI changes.
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
            item = _find_in_cache(needle)
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
                    if enabled is None:
                        return (
                            f"found after {elapsed:.1f}s but its enabled state could "
                            f"not be read: {label}"
                        )
                elif state == "disabled":
                    if _element_enabled(item) is False:
                        return f"disabled after {elapsed:.1f}s: {label}"
            elif state == "close":
                if seen:
                    return (
                        f"disappeared after {elapsed:.1f}s: {seen['name']!r} "
                        f"({seen['type']}) is no longer in {result['window']!r}"
                    )
                return (
                    f"already absent after {elapsed:.1f}s: no element matching "
                    f"{target!r} in {result['window']!r}"
                )
        if elapsed >= timeout:
            detail = f"; last error: {last_error}" if last_error else ""
            if state == "close":
                return (
                    f"timed out after {timeout:g}s waiting for {target!r} to close; "
                    f"it is still present{detail}"
                )
            if state in {"enabled", "disabled"}:
                return (
                    f"timed out after {timeout:g}s waiting for {target!r} to become "
                    f"{state}{detail}"
                )
            return f"timed out after {timeout:g}s waiting for {target!r}{detail}"
        time.sleep(0.4)


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
    """Report native runtime readiness: screen, monitors, active window, OCR and launcher lease."""
    lease = bool(os.environ.get(PARENT_ENV))
    return json.dumps(
        {
            "python": sys.executable,
            "platform": platform.platform(),
            "screen": list(pyautogui.size()),
            "monitors": _monitors(),
            "active_window": _active_window_title(),
            "screenshot_mapping": _shot,
            "tesseract": _tesseract_exe() or "not installed (OCR disabled)",
            "session_token": "active",
            "launcher_lease": "active" if lease else "not configured",
            "read_only": _read_only(),
        },
        indent=2,
    )


def _log_startup() -> None:
    """Append a line to .runtime/logs/sessions.log so the launcher can be verified."""
    lease = os.environ.get(PARENT_ENV, "-")
    _log_event(f"started  launcher={lease}")


if __name__ == "__main__":
    _log_startup()
    sys.stderr.write("[computer-use] MCP server up (session token ok)\n")
    mcp.run("stdio")
