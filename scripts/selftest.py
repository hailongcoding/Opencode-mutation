"""Self-test: proves the computer-use MCP server works, without starting OpenCode.

Checks, in order:
  * every tool is registered
  * agent_status / screen_info answer
  * screenshots come back as images
  * the clipboard round-trips (accented text, emoji, newlines)
  * accessibility snapshot returns a tree plus an image
  * type_text really delivers Unicode into a scratch editor window (SendInput bug)
  * element invoke + window minimize/restore work on that scratch window
  * element set/read/click/wait work on the throwaway Run dialog (no document,
    no save prompt, Escape discards everything)
  * press_keys, focus_window and close_window work, leaving no test window behind
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

EXPECTED_TOOLS = {
    "screenshot", "screen_info", "click", "move", "drag", "scroll",
    "type_text", "press_keys", "key_down", "key_up", "hold_key", "mouse_button",
    "clipboard", "list_windows", "focus_window", "close_window", "launch", "wait",
    "ui_tree", "snapshot", "click_element", "invoke_element", "set_element_text",
    "read_element", "wait_for_window", "wait_for_element", "window_action",
    "agent_s", "agent_status",
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


async def main() -> int:
    env = {**os.environ, "COMPUTER_USE_SESSION_TOKEN": "selftest"}
    params = StdioServerParameters(
        command=sys.executable, args=[SERVER], env=env, cwd=ROOT
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            missing = sorted(EXPECTED_TOOLS - set(tools))
            record(
                "tools registered",
                not missing,
                f"{len(tools)} tools" + (f"; missing: {', '.join(missing)}" if missing else ""),
            )

            status, _ = await call(session, "agent_status")
            record("agent_status", '"session_token": "active"' in status)

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
            payload = "line one — Lý 🐉\nline two"
            await call(session, "clipboard", action="set", text=payload)
            got, _ = await call(session, "clipboard", action="get")
            record(
                "clipboard round-trip",
                payload in got,
                repr(got[:60]),
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
                        clicked2, _ = await call(session, "click", x=100, y=120)
                        record(
                            "monitor 1 click",
                            clicked2.startswith("left click"),
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

            # --- element actions on a throwaway system dialog -------------------
            # The Run dialog is ideal: a small UIA tree, no document to lose, and
            # Escape discards it. Nothing is executed here (no Enter is pressed).
            run_open = False
            try:
                await call(session, "press_keys", keys="win+r")
                waited, _ = await call(session, "wait_for_window", title="Run", timeout=8)
                run_open = waited.startswith("appeared after")
                record("wait_for_window", run_open, waited[:90])

                if run_open:
                    tree, _ = await call(
                        session, "snapshot", window="Run", max_items=60, screenshot=False
                    )
                    has_cancel = any(
                        "Button" in line and "Cancel" in line for line in tree.splitlines()
                    )
                    record(
                        "snapshot (uia tree)",
                        "window: 'Run'" in tree and has_cancel,
                        next(
                            (line for line in tree.splitlines() if "Cancel" in line),
                            tree.splitlines()[0][:80] if tree else "no text",
                        ),
                    )

                    found, _ = await call(
                        session, "wait_for_element", target="Cancel", window="Run", timeout=4
                    )
                    record("wait_for_element", found.startswith("found"), found[:90])

                    typed, _ = await call(
                        session,
                        "set_element_text",
                        target="Open:",
                        text="opencode-selftest",
                        window="Run",
                    )
                    record("set_element_text", typed.startswith("set"), typed[:90])

                    read, _ = await call(
                        session, "read_element", target="Open:", window="Run"
                    )
                    record("read_element", "opencode-selftest" in read, read[:90])

                    clicked, _ = await call(
                        session, "click_element", target="Cancel", window="Run"
                    )
                    record("click_element", clicked.startswith("clicked"), clicked[:90])

                    for _ in range(12):
                        await asyncio.sleep(0.25)
                        windows, _ = await call(session, "list_windows")
                        run_open = any(
                            line.rstrip().endswith("Run") for line in windows.splitlines()
                        )
                        if not run_open:
                            break
                    record(
                        "run dialog closed",
                        not run_open,
                        "still open" if run_open else "closed",
                    )
            finally:
                if run_open:
                    await call(session, "press_keys", keys="esc")
                    await asyncio.sleep(0.4)

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

    failed = [name for name, ok, _ in results if not ok]
    print("\nSELFTEST:", "OK" if not failed else f"FAILED ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
