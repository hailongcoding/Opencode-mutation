# OpenCode + Computer Use (session-scoped)

This folder is a **launcher**. Double-click `Computer-Use.cmd` and OpenCode starts
with a computer-use ability attached **to that session only**. Close OpenCode and
the ability is gone — nothing is left enabled globally.

No global `opencode.json` is edited, no MCP server is registered machine-wide, no
PATH entries are added, and nothing is installed outside this folder.

---

## What you get

Inside the launched session, OpenCode's agent gets a `computer` tool group. The
fast path is **element-level**: see the window as a list of accessible controls,
then act on them by id/name instead of guessing pixels.

| Tool | What it does |
| --- | --- |
| `snapshot` | **Start here.** One call returns the active window, its accessible tree with `eN` ids, and a screenshot |
| `click_element`, `invoke_element` | Click or invoke a control by id (`e12`) or name (`"Save"`); `invoke_element` uses UI Automation and needs no mouse move |
| `set_element_text`, `read_element` | Write/read an edit box, label or list row directly |
| `ui_tree` | Text-only tree with the same ids (cheap refresh) |
| `screenshot` | Capture a monitor, all monitors, a rectangle (`region`), or one window (even if behind another); optional OCR |
| `click`, `move`, `drag`, `scroll` | Pixel fallback for apps with no useful tree; coordinates come from the last screenshot. `scroll(horizontal=true)` for sideways |
| `type_text` | Types arbitrary Unicode (emoji, accents, tabs, newlines) |
| `press_keys` | Key combos: `ctrl+c`, `alt+f4`, `enter`, `win`, ... |
| `key_down`, `key_up`, `hold_key`, `mouse_button` | Modifier/mouse holds for ranges, selections and custom drags |
| `clipboard` | Get / set / paste clipboard text |
| `list_windows`, `focus_window`, `close_window`, `window_action`, `screen_info` | Window inventory, monitor layout, focus, targeted close (safer than a blind `alt+f4`), and minimize/maximize/restore/move/resize |
| `wait_for_window`, `wait_for_element`, `wait`, `launch` | Wait for a window/control instead of sleeping; launch apps with arguments and wait for their window |
| `agent_s` | Optional: hands a whole task to **Agent-S** (autonomous GUI agent) |
| `agent_status` | What is actually available in the runtime |

Multi-monitor is handled: `screen_info` shows the layout, `screenshot(monitor=0|1|...|-1)`
picks a display, and clicks are translated back to the right screen coordinates.
`snapshot(window="Title")` and `screenshot(window="Title")` target one window.

`agent_s` is only usable if you provide model credentials and a grounding
endpoint (see below). Without them, the agent still drives the GUI itself via the
primitives above — that path needs no extra API keys.

### How this compares with other computer-use servers

The design borrows the parts that made other projects reliable:

- **Accessibility-tree-first, like Windows-MCP and deploymenttheory/windows-mcp-server.** Stable `eN` ids and `Invoke`/`ValuePattern` actions avoid pixel guessing; `snapshot` combines the tree and a screenshot so the model gets both in one round trip.
- **Explicit element targeting** (`click_element`, `set_element_text`, `read_element`) in the spirit of Playwright MCP's ref-based tools.
- **Wait-for-window/control** instead of fixed sleeps, plus **key/mouse holds** and window actions, matching the action vocabulary of Anthropic's computer-use reference implementation and desktop agents such as UI-TARS.
- **Region and per-window capture** (including windows behind another), so verification does not require rearranging the desktop.

Deliberately *not* included: shell/registry/process-admin tools (dangerous and
outside a computer-use grant), browser-internal APIs (this is app-agnostic), video
recording, and always-on background input. UIA `invoke` already acts without
moving the pointer where the target app supports it.

---

## Run it

```powershell
# double-click, or from a terminal:
.\Computer-Use.cmd

# pick a project folder / pass normal opencode flags through:
.\Computer-Use.cmd -Directory C:\Users\Admin\projects\demo
.\Computer-Use.cmd --prompt "Open Notepad and type hello"
```

**Nothing needs to be installed first** — no Node, no npm, and no system Python.
On first run the launcher uses the portable OpenCode + `uv` already in `vendor\`
(or downloads them, ~150 MB, checksum-verified), then builds `.runtime\venv` and
installs:

- `mcp` (server SDK), `pyautogui`, `pillow` — the GUI primitives
- **Agent-S** (`gui-agents`) from <https://github.com/simular-ai/Agent-S.git>

First run takes a few minutes (Agent-S pulls paddle/OCR/selenium deps). Later runs
start instantly and work offline. Useful switches:

| Switch | Effect |
| --- | --- |
| `-NoAgentS` | Skip the heavy Agent-S install (primitives still work) |
| `-Reinstall` | Wipe and rebuild `.runtime\venv` |
| `-SelfTest` | Check every primitive (screenshot, clipboard, real Unicode typing into a scratch window the test owns, accessibility snapshot, element actions on a throwaway Run dialog, launch/wait, window list) without launching OpenCode |
| `-Offline` | Never download; fail fast if something is missing |
| `-PrepareVendor` | Copy portable OpenCode + uv into `vendor\` to make an offline bundle |
| `-RefreshTools` | Re-download OpenCode/uv (pinned versions) |
| `-LatestTools` | With `-RefreshTools`, use the newest upstream release |

## Portable bundle (no npm/Node)

```powershell
.\Computer-Use.cmd -PrepareVendor   # portable opencode.exe + uv.exe into vendor\
```

`vendor\` removes the npm/Node requirement entirely: the folder then carries the
official OpenCode binary and `uv`, so `Computer-Use.cmd` runs on a PC without
Node, npm, or a system-wide OpenCode.

Python is the one piece `uv` still provides on the target: on first run the
launcher builds `.runtime\venv`, reusing the machine's Python 3.12 if there is one
and otherwise fetching a private copy. That build needs network access to install
the Python packages. A `.runtime\venv` copied from another machine is detected and
rebuilt automatically when it does not run in the new location.

Approximate sizes: `vendor\` ~320 MB, `.runtime\venv` ~930 MB.

`vendor\opencode\opencode.exe` is ~250 MB, so a plain GitHub repo will reject it
(100 MB file limit). Use Git LFS, or ship the prepared folder as a zip / release
asset instead of committing `vendor\`.

Tool lookup order: `vendor\` → `.runtime\tools\` (auto-download cache) → `PATH`.
That means an existing OpenCode/uv install is reused instead of downloading.

---

## Enabling the autonomous Agent-S path (optional)

Agent-S needs its own LLM plus a **grounding** model (e.g. UI-TARS served by
vLLM/TGI). Set these before launching — the launcher passes them through to the
MCP server:

```powershell
$env:OPENAI_API_KEY        = "sk-..."        # or ANTHROPIC_API_KEY / GEMINI_API_KEY
$env:AGENT_S_PROVIDER      = "openai"
$env:AGENT_S_MODEL         = "gpt-5-2025-08-07"
$env:AGENT_S_GROUND_URL    = "http://localhost:8080"
$env:AGENT_S_GROUND_MODEL  = "ui-tars-1.5-7b"
$env:AGENT_S_GROUNDING_WIDTH  = "1920"
$env:AGENT_S_GROUNDING_HEIGHT = "1080"
.\Computer-Use.cmd
```

Then ask the agent to call `agent_s` with a task, or call it yourself.

Optional extra: install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
to get text from `screenshot(ocr=true)`. Set `TESSERACT_CMD` if it is not in
`C:\Program Files\Tesseract-OCR`.

---

## How the session scoping works

1. `Computer-Use.cmd` runs `scripts\Start-ComputerUse.ps1`.
2. The launcher creates a random **session token**, builds a small OpenCode config
   **in memory**, and exports it as `OPENCODE_CONFIG_CONTENT` for the child process.
3. It then runs `opencode --standalone` — a private server, so the injected config
   applies to that process only and cannot leak into your other OpenCode sessions.
4. The MCP server (`scripts\computer_use_mcp.py`) refuses to start unless that
   token is present.
5. When OpenCode exits, the environment variables die with the process. The only
   things written live inside this folder: `.runtime\` (venv, logs, tool cache) and,
   if you prepared it, `vendor\`.

So: no global grant, no always-on listener, no background agent.

---

## Safety notes

- Automation moves your real mouse and keyboard. `pyautogui` failsafe is on: slam
  the pointer into a screen corner to abort.
- `invoke_element` acts through UI Automation without moving the pointer, but it is
  still a real action. Read the target name before clicking something destructive.
- Prefer `snapshot`/`click_element`/`set_element_text` over blind pixel clicks.
  When an app exposes no accessibility tree, fall back to a screenshot.
- Agent-S (and the `agent_s` tool) can run arbitrary GUI actions and optionally
  local code; treat it like handing someone your desktop.
- `ui_tree`, `screenshot` and clipboard reads expose whatever is on screen or in
  your clipboard to the model you are chatting with.
- Use it on a machine you control, and watch the first few runs.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| First run cannot download OpenCode/uv | Corporate proxy or offline PC. Run `-PrepareVendor` on a connected machine and copy the folder, or drop `opencode.exe`/`uv.exe` into `vendor\opencode\` and `vendor\uv\` |
| `checksum mismatch` | A proxy or antivirus modified the download. Retry with `-RefreshTools` |
| OpenCode starts but shows `0 MCP` | Give it a few seconds; if it stays 0, check `.runtime\logs\sessions.log` and the OpenCode log (`~/.local/share/opencode/log/opencode.log`) |
| No `computer` tools in the session | Exit OpenCode fully, relaunch via `Computer-Use.cmd` (not `opencode` directly) |
| `Refusing to start: no active launcher session` | Start through `Computer-Use.cmd`; that message means the server was run outside the launcher |
| Agent-S install failed | Primitives still work. Retry `.\Computer-Use.cmd -Reinstall`, or use `-NoAgentS` |
| Clicks land slightly off | Screenshots are scaled; always click using coordinates from the most recent `screenshot`, and capture the monitor that holds the target window |
| `snapshot`/`ui_tree` shows no controls | That app exposes no useful UI Automation tree (some games, some Tk/Electron builds). Use `screenshot` + `click`/`type_text` instead |
| `snapshot`/`ui_tree` is slow or hangs | One control's accessibility provider is stalling. Retry with a smaller `max_items`, target the exact window, or use `screenshot` for that app |
| `unknown element id` or an action hits the wrong control | Ids belong to the latest snapshot. Take a fresh `snapshot`/`ui_tree` after the UI changes (or use `click_element` with a name) |
| `click_element` does nothing on a button | Try `invoke_element` (UI Automation pattern) or focus the window first; some custom-drawn controls are not clickable targets |
| `type_text` returns "typed ..." but nothing appears | The focused control is not a text field, or the target app runs **elevated** (Windows blocks input from a normal process to an admin window). Click the field first; run the launcher elevated if you really need it |
| `focus_window` says Windows kept another window in front | Retries and the AttachThreadInput workaround are already applied; if it still fails, click the window once by hand. Windows also forbids a background app from stealing focus from a foreground one |
| Clipboard says "another app is holding it" | A clipboard manager is mid-write; the tool retries, then falls back to PowerShell. Try again |
| A window is missing from `list_windows` | It is filtered only when it has no title or zero area. Look for the `[off-screen]` marker — `focus_window` moves such a window back onto the primary display |
| `agent_s` unicode / encoding noise | Harmless Python or console warnings; check `.runtime\logs\` |
| Need a clean slate | Delete `.runtime\` and run again (keeps `vendor\`) |

Logs from the MCP server sessions land in `.runtime\logs\sessions.log`, and
Agent-S task logs in `.runtime\logs\agent-s-*.log`.

## Files

```text
Computer-Use.cmd              <- double-click this (session-only launcher)
scripts\Start-ComputerUse.ps1 <- bootstrap: portable tools, runtime, token, launch
scripts\computer_use_mcp.py   <- the computer-use MCP server (29 tools)
scripts\selftest.py           <- -SelfTest implementation
scripts\scratch_editor.py     <- throwaway Tk window the self-test types into
vendor\opencode\opencode.exe  <- optional offline bundle (portable OpenCode)
vendor\uv\uv.exe              <- optional offline bundle (portable uv)
.runtime\tools                <- auto-downloaded OpenCode/uv cache
.runtime\venv                 <- private Python runtime (Agent-S lives here)
.runtime\logs                 <- session + Agent-S logs
```

The vendored OpenCode binary is MIT-licensed and unmodified; version pins are the
`-OpenCodeVersion` / `-UvVersion` defaults in `Start-ComputerUse.ps1`.

## Uninstall

Delete this folder. Nothing else was modified.
