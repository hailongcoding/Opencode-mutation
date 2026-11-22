"""Self-test: proves the computer-use MCP server works, without starting OpenCode.

Checks, in order:
  * every tool is registered
  * runtime_status / screen_info answer
  * screenshots come back as images
  * the clipboard round-trips (accented text, emoji, newlines) and is restored
  * accessibility snapshot returns a tree plus an image, including annotated boxes
  * type_text really delivers Unicode into a scratch editor window (SendInput bug)
  * element invoke + window minimize/restore work on that scratch window
  * element actions run against a throwaway WinForms window with stable English
    UIA names/automation ids (no localized system dialogs): search, state,
    selection, tables/grids, menus, file dialog, targeted scroll, scroll-into-view,
    drag, modifier click, waits and OCR (skipped when Tesseract is absent)
  * pause blocks actions, release_all works, and the audit log is written
  * press_keys, focus_window and close_window work, leaving no test window behind
  * a read-only server process allows inspection and blocks every action
"""

import asyncio
import base64
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

from PIL import Image

# The self-test prints sample text with accents/emoji; a cp1252 console would
# otherwise explode mid-test and report a false failure.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(HERE, "computer_use_mcp.py")
AUDIT_LOG = os.path.join(ROOT, ".runtime", "logs", "actions.log")

EXPECTED_TOOLS = {
    "screenshot", "screen_info", "click", "move", "drag", "drag_element", "scroll",
    "type_text", "press_keys", "key_down", "key_up", "hold_key", "mouse_button",
    "clipboard", "list_windows", "focus_window", "close_window", "launch", "wait",
    "ui_tree", "snapshot", "find_elements", "click_element", "invoke_element",
    "set_element_text", "read_element", "wait_for_window", "wait_for_element",
    "window_action", "runtime_status", "scroll_into_view", "get_element_state",
    "read_table", "select_element", "menu", "file_dialog", "find_text",
    "click_text", "highlight_element", "wait_for_idle", "wait_for_change",
    "pause", "release_all",
}

# The computer-use grant must not become a shell/git/process-admin grant. The
# launcher's auto-update is not callable from the model; keep it that way.
FORBIDDEN_TOOLS = {
    "git", "shell", "exec", "run_command", "powershell", "cmd", "terminal",
    "process", "registry", "service",
}

PROBE = "selftest 123 — Lý Thái Tổ"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'ok' if ok else '!!'}] {name}{(' - ' + detail) if detail else ''}")


async def call(session, tool, **args):
    reply = await session.call_tool(tool, args)
    text = " ".join(
        getattr(block, "text", "") for block in reply.content if hasattr(block, "text")
    )
    return text, reply


def image_size(reply) -> tuple[int, int] | None:
    for block in reply.content:
        if type(block).__name__ == "ImageContent":
            return Image.open(io.BytesIO(base64.b64decode(block.data))).size
    return None


async def wait_state(path: str, expected: str, timeout: float = 6.0) -> str:
    """Poll a file the scratch app writes, so UI actions have real ground truth."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = ""
        if expected in text:
            return text
        await asyncio.sleep(0.2)
    return text


def audit_since(offset: int) -> str:
    """Only inspect records appended by this run, not the user's older history."""
    try:
        with open(AUDIT_LOG, encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return ""


async def main() -> int:
    env = {**os.environ, "COMPUTER_USE_SESSION_TOKEN": "selftest"}
    env["COMPUTER_USE_READ_ONLY"] = "0"
    params = StdioServerParameters(
        command=sys.executable, args=[SERVER], env=env, cwd=ROOT
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                audit_offset = os.path.getsize(AUDIT_LOG)
            except OSError:
                audit_offset = 0
            tools = [tool.name for tool in (await session.list_tools()).tools]
            missing = sorted(EXPECTED_TOOLS - set(tools))
            record(
                "tools registered",
                not missing,
                f"{len(tools)} tools" + (f"; missing: {', '.join(missing)}" if missing else ""),
            )
            exposed = sorted(FORBIDDEN_TOOLS & set(tools))
            record(
                "no shell/git tools exposed",
                not exposed,
                "none" if not exposed else f"forbidden: {', '.join(exposed)}",
            )

            status, _ = await call(session, "runtime_status")
            record("runtime_status", '"session_token": "active"' in status)
            record(
                "audit log",
                '"tool": "runtime_status"' in audit_since(audit_offset),
                AUDIT_LOG,
            )
            record(
                "health check",
                '"input_desktop": "available"' in status
                and '"dpi":' in status
                and '"audit_log":' in status,
                "desktop/DPI/audit fields present",
            )

            info, _ = await call(session, "screen_info")
            record("screen_info", "monitors(" in info, info[:90])

            shot = await session.call_tool("screenshot", {"monitor": 0, "max_width": 640})
            kinds = [type(block).__name__ for block in shot.content]
            record("screenshot", "ImageContent" in kinds, str(kinds))

            monitor_one = None
            match = re.search(r"1=\((-?\d+),\s*(-?\d+),\s*(\d+),\s*(\d+)\)", info)
            if match:
                monitor_one = tuple(int(value) for value in match.groups())

            if monitor_one:
                mx, my, _mw, _mh = monitor_one
                shot1 = await session.call_tool(
                    "screenshot", {"monitor": 1, "max_width": 640}
                )
                text1 = " ".join(
                    getattr(block, "text", "")
                    for block in shot1.content
                    if hasattr(block, "text")
                )
                record(
                    "monitor 1 screenshot",
                    image_size(shot1) is not None and "monitor 1" in text1,
                    (text1.splitlines()[0][:90] if text1 else "no text"),
                )
                region = [mx + 50, my + 50, mx + 370, my + 250]
            else:
                record("monitor 1 screenshot", True, "only one monitor; skipped")
                region = [100, 100, 420, 300]

            # Region edges are absolute screen coordinates (not width/height).
            # A nonzero/negative origin catches the old right/bottom mix-up.
            region_shot = await session.call_tool(
                "screenshot", {"region": region, "max_width": 640}
            )
            expected = (region[2] - region[0], region[3] - region[1])
            actual = image_size(region_shot)
            record(
                "region screenshot",
                actual == expected,
                f"{actual} expected {expected}",
            )

            # --- clipboard: unicode + emoji + newline round-trip ---------------
            # Save the user's clipboard first and restore it immediately after the
            # check: the self-test must not destroy whatever they had copied.
            original_clipboard, _ = await call(session, "clipboard", action="get")
            original_text = ""
            if original_clipboard.startswith("clipboard (") and "\n" in original_clipboard:
                original_text = original_clipboard.split("\n", 1)[1]
            payload = "line one — Lý 🐉\nline two"
            await call(session, "clipboard", action="set", text=payload)
            got, _ = await call(session, "clipboard", action="get")
            record(
                "clipboard round-trip",
                payload in got,
                repr(got[:60]),
            )
            await call(session, "clipboard", action="set", text=original_text)
            restored, _ = await call(session, "clipboard", action="get")
            record(
                "clipboard restored",
                original_text in restored or not original_text,
                f"{len(original_text)} chars",
            )

            # --- typing: the SendInput struct-size regression -------------------
            # Type into a scratch Tk window owned by this test, never into whatever
            # "Notepad" happens to be open: no user document, no save dialog, and
            # the widget mirrors itself to a file so the check has real ground
            # truth instead of an undo/clipboard round-trip.
            scratch = os.path.join(
                tempfile.gettempdir(),
                f"opencode-selftest-{uuid.uuid4().hex[:8]}.txt",
            )
            editor_title = f"Computer-use selftest {uuid.uuid4().hex[:6]}"
            editor = os.path.join(HERE, "scratch_editor.py")
            proc = subprocess.Popen([sys.executable, editor, scratch, editor_title])
            try:
                for _ in range(24):
                    if os.path.exists(scratch) or proc.poll() is not None:
                        break
                    await asyncio.sleep(0.25)

                target_ready = False
                focus_note = ""
                for _ in range(6):
                    focus_note, _ = await call(session, "focus_window", title=editor_title)
                    current, _ = await call(session, "screen_info")
                    if editor_title.lower() in current.lower():
                        target_ready = True
                        break
                    await asyncio.sleep(0.6)

                if not target_ready:
                    record(
                        "type_text reaches another app",
                        False,
                        f"could not focus the scratch editor: {focus_note[:60]}",
                    )
                else:
                    snap_text, snap_reply = await call(
                        session,
                        "snapshot",
                        window=editor_title,
                        max_items=60,
                        screenshot=True,
                    )
                    snap_kinds = [type(block).__name__ for block in snap_reply.content]
                    record(
                        "snapshot (tree + image)",
                        editor_title.lower() in snap_text.lower()
                        and "ImageContent" in snap_kinds,
                        snap_text.splitlines()[0][:80] if snap_text else "no text",
                    )

                    if monitor_one:
                        mx, my, _mw, _mh = monitor_one
                        moved2, _ = await call(
                            session,
                            "window_action",
                            title=editor_title,
                            action="move_resize",
                            x=mx + 120,
                            y=my + 120,
                            width=620,
                            height=240,
                        )
                        record(
                            "window_action on monitor 1",
                            moved2.startswith("move_resize:")
                            and f"({mx + 120},{my + 120}," in moved2,
                            moved2[:100],
                        )
                        await call(session, "focus_window", title=editor_title)
                        shot2 = await session.call_tool(
                            "screenshot", {"window": editor_title, "max_width": 640}
                        )
                        text2 = " ".join(
                            getattr(block, "text", "")
                            for block in shot2.content
                            if hasattr(block, "text")
                        )
                        record(
                            "monitor 1 window screenshot",
                            image_size(shot2) is not None and "at screen (" in text2,
                            text2.splitlines()[0][:90] if text2 else "no text",
                        )
                        clicked2, _ = await call(
                            session, "click", x=100, y=120, modifiers="ctrl"
                        )
                        record(
                            "modifier click",
                            "ctrl+left click" in clicked2,
                            clicked2[:90],
                        )

                    await call(session, "type_text", text=PROBE)
                    deadline = time.monotonic() + 6.0
                    received = ""
                    while time.monotonic() < deadline:
                        try:
                            with open(scratch, encoding="utf-8") as handle:
                                received = handle.read()
                        except OSError:
                            received = ""
                        if PROBE in received:
                            break
                        await asyncio.sleep(0.2)
                    record(
                        "type_text reaches another app",
                        PROBE in received,
                        repr(received[:60]),
                    )
                    record(
                        "audit redacts typed text",
                        PROBE not in audit_since(audit_offset)
                        and '"tool": "type_text"' in audit_since(audit_offset),
                        "typed payload absent from actions.log",
                    )

                    invoked, _ = await call(
                        session, "invoke_element", target="Minimize", window=editor_title
                    )
                    windows, _ = await call(session, "list_windows")
                    minimized_line = next(
                        (
                            line
                            for line in windows.splitlines()
                            if editor_title.lower() in line.lower()
                        ),
                        "",
                    )
                    record(
                        "invoke_element",
                        invoked.startswith("invoked") and "[min]" in minimized_line,
                        invoked[:80],
                    )

                    restored, _ = await call(
                        session, "window_action", title=editor_title, action="restore"
                    )
                    record("window_action", restored.startswith("restore:"), restored[:80])

                    moved, _ = await call(
                        session,
                        "window_action",
                        title=editor_title,
                        action="move",
                        x=100,
                        y=100,
                    )
                    resized, _ = await call(
                        session,
                        "window_action",
                        title=editor_title,
                        action="resize",
                        width=520,
                        height=280,
                    )
                    record(
                        "window_action move/resize",
                        moved.startswith("move:") and resized.startswith("resize:"),
                        f"{moved[:35]} / {resized[:35]}",
                    )

                closed, _ = await call(session, "close_window", title=editor_title)
                try:
                    proc.wait(timeout=5)
                    exited = True
                except subprocess.TimeoutExpired:
                    exited = False
                record(
                    "close_window",
                    closed.startswith("ok: ") and exited,
                    closed[:80],
                )

                windows, _ = await call(session, "list_windows")
                record(
                    "list_windows",
                    editor_title.lower() not in windows.lower(),
                    next(iter(windows.splitlines()), ""),
                )
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                try:
                    os.remove(scratch)
                except OSError:
                    pass

            # --- element actions on a throwaway WinForms window -----------------
            # English control names + AutomationIds make this independent of the
            # Windows display language (the old Run-dialog test was not).
            uia_state = os.path.join(
                tempfile.gettempdir(), f"opencode-uia-{uuid.uuid4().hex[:8]}.txt"
            )
            uia_title = f"Computer-use UIA {uuid.uuid4().hex[:6]}"
            uia_script = os.path.join(HERE, "scratch_uia.ps1")
            uia_proc = subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-STA",
                    "-ExecutionPolicy", "Bypass", "-File",
                    uia_script, uia_title, uia_state,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                uia_waited, _ = await call(
                    session, "wait_for_window", title=uia_title, timeout=20
                )
                uia_open = uia_waited.startswith("appeared after")
                record("wait_for_window (scratch UIA)", uia_open, uia_waited[:90])

                if uia_open:
                    snap_text, snap_reply = await call(
                        session, "snapshot", window=uia_title, max_items=120,
                        screenshot=True, annotate=True,
                    )
                    snap_kinds = [type(block).__name__ for block in snap_reply.content]
                    record(
                        "snapshot (annotated tree + image)",
                        "Test Button" in snap_text and "ImageContent" in snap_kinds,
                        snap_text.splitlines()[0][:90] if snap_text else "no text",
                    )

                    matches, _ = await call(
                        session, "find_elements", query="Test Button", window=uia_title
                    )
                    record(
                        "find_elements (scratch UIA)",
                        "match(es)" in matches and "Test Button" in matches,
                        matches.splitlines()[-1][:90] if matches else "no text",
                    )

                    enabled, _ = await call(
                        session, "wait_for_element", target="Test Button",
                        window=uia_title, timeout=4, state="enabled",
                    )
                    record(
                        "wait_for_element (enabled)",
                        enabled.startswith("enabled after"),
                        enabled[:90],
                    )

                    typed, _ = await call(
                        session, "set_element_text", target="TestField",
                        text="hello-uia", window=uia_title,
                    )
                    record(
                        "set_element_text (verified)",
                        typed.startswith("set") and "(verified)" in typed,
                        typed[:90],
                    )
                    read, _ = await call(
                        session, "read_element", target="TestField", window=uia_title
                    )
                    record("read_element", "hello-uia" in read, read[:90])

                    element_state, _ = await call(
                        session, "get_element_state", target="TestField",
                        window=uia_title,
                    )
                    record(
                        "get_element_state",
                        '"value": "hello-uia"' in element_state
                        and '"enabled": true' in element_state,
                        element_state.replace("\n", " ")[:110],
                    )

                    clicked, _ = await call(
                        session, "click_element", target="TestButton", window=uia_title
                    )
                    button_state = await wait_state(uia_state, "button-clicked", 5)
                    record(
                        "click_element (scratch UIA)",
                        clicked.startswith("clicked")
                        and "button-clicked" in button_state,
                        clicked[:90],
                    )

                    invoked, _ = await call(
                        session, "invoke_element", target="TestCheck", window=uia_title
                    )
                    check_state = await wait_state(uia_state, "check:True", 5)
                    toggled, _ = await call(
                        session, "get_element_state", target="TestCheck",
                        window=uia_title,
                    )
                    record(
                        "invoke_element (checkbox)",
                        invoked.startswith("invoked")
                        and ("check:True" in check_state or '"checked": true' in toggled),
                        invoked[:80],
                    )

                    selected, _ = await call(
                        session, "select_element", target="TestCombo",
                        item_text="Beta", window=uia_title,
                    )
                    combo_state = await wait_state(uia_state, "combo:Beta", 5)
                    record(
                        "select_element (combo)",
                        selected.startswith("selected")
                        and "combo:Beta" in combo_state,
                        selected[:100],
                    )

                    table, _ = await call(
                        session, "read_table", target="TestList", window=uia_title,
                        max_rows=12,
                    )
                    record(
                        "read_table (list)",
                        "Row One" in table and "Row Three" in table,
                        table.splitlines()[0][:90] if table else "no text",
                    )

                    grid, _ = await call(
                        session, "read_table", target="TestGrid", window=uia_title
                    )
                    record(
                        "read_table (grid)",
                        "Alpha" in grid and "Gamma" in grid,
                        grid.splitlines()[0][:90] if grid else "no text",
                    )

                    # Virtualized lists only expose visible rows, so scroll the
                    # container first; scroll_into_view then verifies/repairs the
                    # target's position inside the window.
                    scrolled_list, _ = await call(
                        session, "scroll", amount=-30, target="TestList",
                        window=uia_title,
                    )
                    record(
                        "scroll (targeted list)",
                        "via ScrollPattern" in scrolled_list,
                        scrolled_list[:100],
                    )
                    await call(
                        session, "scroll", amount=50, target="TestList",
                        window=uia_title,
                    )
                    await asyncio.sleep(0.4)
                    scrolled, _ = await call(
                        session, "scroll_into_view", target="Item 60", window=uia_title
                    )
                    record(
                        "scroll_into_view",
                        scrolled.startswith("scrolled") and "visible=True" in scrolled,
                        scrolled[:100],
                    )

                    dragged, _ = await call(
                        session, "drag_element", source="Row One",
                        destination="Row Three", window=uia_title,
                    )
                    record(
                        "drag_element (scratch UIA)",
                        dragged.startswith("dragged")
                        and "Row One" in dragged and "Row Three" in dragged,
                        dragged[:100],
                    )

                    mod_clicked, _ = await call(
                        session, "click_element", target="Row Two",
                        window=uia_title, modifiers="ctrl",
                    )
                    record(
                        "click_element (modifier)",
                        mod_clicked.startswith("ctrl+clicked"),
                        mod_clicked[:90],
                    )

                    menu_reply, _ = await call(
                        session, "menu", path="File > Test Action", window=uia_title
                    )
                    menu_state = await wait_state(uia_state, "menu-clicked", 5)
                    record(
                        "menu (File > Test Action)",
                        menu_reply.startswith("menu:") and "menu-clicked" in menu_state,
                        menu_reply[:100],
                    )

                    highlighted, _ = await call(
                        session, "highlight_element", target="TestButton",
                        window=uia_title,
                    )
                    record(
                        "highlight_element",
                        highlighted.startswith("highlighted"),
                        highlighted[:90],
                    )

                    idle, _ = await call(
                        session, "wait_for_idle", window=uia_title,
                        idle_ms=500, timeout=6, include_tree=False,
                    )
                    record("wait_for_idle", idle.startswith("idle after"), idle[:90])

                    changed, _ = await call(
                        session, "wait_for_change", window=uia_title,
                        timeout=6, threshold=0.01,
                    )
                    record("wait_for_change", changed.startswith("changed after"), changed[:100])

                    await call(
                        session, "click_element", target="HideMe", window=uia_title
                    )
                    hidden_state = await wait_state(uia_state, "hidden", 5)
                    gone, _ = await call(
                        session, "wait_for_element", target="Hide Me",
                        window=uia_title, timeout=3, state="close",
                    )
                    record(
                        "wait_for_element (close)",
                        "hidden" in hidden_state
                        and (
                            gone.startswith("disappeared")
                            or gone.startswith("already absent")
                        ),
                        gone[:100],
                    )
                    never, _ = await call(
                        session, "wait_for_element", target="Never Exists",
                        window=uia_title, timeout=1.2, state="close",
                        must_exist=True,
                    )
                    record(
                        "wait_for_element (never appeared)",
                        never.startswith("timed out") and "never present" in never,
                        never[:100],
                    )

                    await call(
                        session, "click_element", target="OpenDialog", window=uia_title
                    )
                    dialog_waited, _ = await call(
                        session, "wait_for_window", title="Computer-use file dialog",
                        timeout=8,
                    )
                    dialog_open = dialog_waited.startswith("appeared after")
                    record("file dialog opened", dialog_open, dialog_waited[:90])
                    if dialog_open:
                        target_file = os.path.join(
                            tempfile.gettempdir(),
                            f"opencode-file-{uuid.uuid4().hex[:8]}.txt",
                        )
                        filled, _ = await call(
                            session, "file_dialog", path=target_file,
                            action="open", window="Computer-use file dialog",
                        )
                        file_state = await wait_state(uia_state, "file:", 5)
                        record(
                            "file_dialog",
                            filled.startswith("open:")
                            and "file:" in file_state
                            and target_file.lower() in file_state.lower(),
                            filled[:110],
                        )
                        try:
                            os.remove(target_file)
                        except OSError:
                            pass

                    paused, _ = await call(session, "pause", action="on")
                    blocked, _ = await call(session, "click", x=1, y=1)
                    resumed, _ = await call(session, "pause", action="off")
                    record(
                        "pause blocks actions",
                        paused.startswith("automation paused")
                        and blocked.startswith("PAUSED")
                        and resumed.startswith("automation running"),
                        f"{paused[:22]} / {blocked[:22]} / {resumed[:24]}",
                    )
                    released, _ = await call(session, "release_all")
                    record("release_all", released.startswith("released"), released[:90])

                    if '"tesseract": "not installed' in status:
                        record(
                            "find_text / click_text (OCR)", True,
                            "Tesseract not installed; skipped",
                        )
                    else:
                        ocr_found, _ = await call(
                            session, "find_text", text="Test Button", window=uia_title
                        )
                        ocr_clicked, _ = await call(
                            session, "click_text", text="Test Button", window=uia_title
                        )
                        record(
                            "find_text / click_text (OCR)",
                            "OCR match" in ocr_found
                            and ocr_clicked.startswith("clicked OCR text"),
                            f"{ocr_found.splitlines()[0][:50]} / {ocr_clicked[:50]}",
                        )
            finally:
                await call(
                    session, "close_window", title="Computer-use file dialog", wait=0.5
                )
                await call(session, "close_window", title=uia_title, wait=2)
                if uia_proc.poll() is None:
                    uia_proc.terminate()
                    try:
                        uia_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        uia_proc.kill()
                try:
                    os.remove(uia_state)
                except OSError:
                    pass

            # --- launch with arguments + wait_for_window ------------------------
            # Starting a console app used to inherit the MCP server's stdio pipes
            # and stall; this exercises the detached launch path end to end.
            scratch2 = os.path.join(
                tempfile.gettempdir(), f"opencode-launch-{uuid.uuid4().hex[:8]}.txt"
            )
            title2 = f"Computer-use launch {uuid.uuid4().hex[:6]}"
            try:
                launched, _ = await call(
                    session,
                    "launch",
                    target=sys.executable,
                    arguments=f'"{editor}" "{scratch2}" "{title2}"',
                    wait=0.3,
                    wait_for=title2,
                    timeout=10,
                )
                appeared = launched.startswith("launched ") and "window appeared:" in launched
                record("launch (args + wait_for)", appeared, launched[:90])
                if appeared:
                    closed2, _ = await call(
                        session, "close_window", title=title2, wait=4
                    )
                    record(
                        "launch window closes",
                        closed2.startswith("ok: "),
                        closed2[:80],
                    )
            finally:
                await call(session, "close_window", title=title2)
                try:
                    os.remove(scratch2)
                except OSError:
                    pass

    # --- read-only policy: a second server process, same runtime -----------
    ro_env = {
        **os.environ,
        "COMPUTER_USE_SESSION_TOKEN": "selftest-ro",
        "COMPUTER_USE_READ_ONLY": "1",
    }
    ro_params = StdioServerParameters(
        command=sys.executable, args=[SERVER], env=ro_env, cwd=ROOT
    )
    async with stdio_client(ro_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            status, _ = await call(session, "runtime_status")
            record("read-only status", '"read_only": true' in status)

            allowed, _ = await call(
                session, "find_elements", query="anything", window="__missing_window__"
            )
            record(
                "read-only inspection allowed",
                not allowed.startswith("BLOCKED")
                and allowed.startswith("find_elements:"),
                allowed[:90],
            )

            clipboard_get, _ = await call(session, "clipboard", action="get")
            record(
                "read-only clipboard get",
                clipboard_get.startswith("clipboard ("),
                clipboard_get.splitlines()[0][:90] if clipboard_get else "no text",
            )

            blocked_set, _ = await call(
                session, "clipboard", action="set", text="must-not-write"
            )
            record("read-only clipboard set blocked", blocked_set.startswith("BLOCKED"))
            blocked_click, _ = await call(session, "click", x=1, y=1)
            record("read-only click blocked", blocked_click.startswith("BLOCKED"))
            blocked_drag, _ = await call(
                session, "drag_element", source="a", destination="b"
            )
            record("read-only drag blocked", blocked_drag.startswith("BLOCKED"))
            blocked_keys, _ = await call(session, "press_keys", keys="enter")
            record("read-only keyboard blocked", blocked_keys.startswith("BLOCKED"))
            blocked_window, _ = await call(
                session, "close_window", title="__missing_window__"
            )
            record("read-only window action blocked", blocked_window.startswith("BLOCKED"))
            blocked_launch, _ = await call(session, "launch", target="cmd.exe")
            record("read-only launch blocked", blocked_launch.startswith("BLOCKED"))

    failed = [name for name, ok, _ in results if not ok]
    print("\nSELFTEST:", "OK" if not failed else f"FAILED ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
