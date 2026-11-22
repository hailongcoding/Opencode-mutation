# OpenCode + Computer Use (session-scoped)

This folder is a **launcher**. Double-click `Computer-Use.cmd` and OpenCode starts
with a computer-use ability attached **to that session only**. Close OpenCode and
the ability is gone — nothing is left enabled globally.

No global `opencode.json` is edited, no MCP server is registered machine-wide, no
PATH entries are added, and the computer-use runtime stays inside this folder.
OpenCode itself still writes its normal logs, auth and session data under your user
profile, exactly as any OpenCode run does.

---

## What you get

Inside the launched session, OpenCode's agent gets a `computer` tool group. The
fast path is **element-level**: see the window as a list of accessible controls,
then act on them by id/name instead of guessing pixels.

| Tool | What it does |
| --- | --- |
| `snapshot` | **Start here.** One call returns the active window, its accessible tree with `eN` ids, and a screenshot. `annotate=true` draws numbered boxes on the image (Set-of-Mark), and controls whose stable automation id differs from their visible label show it as `#AutomationId` |
| `ui_tree`, `find_elements` | Text-only tree and cheap name/type/automation-id search; results carry the same `eN` ids and bounds |
| `click_element`, `invoke_element` | Click or invoke a control by id (`e12`), name (`"Save"`) or automation id; `click_element` accepts `modifiers="ctrl+shift"`, and `invoke_element` uses UI Automation without moving the mouse |
| `drag_element` | Drag from one element to another by id/name (file moves, list/tree reordering), with a guaranteed mouse release |
| `set_element_text`, `read_element`, `get_element_state` | Write/read an edit box, label or list row; `get_element_state` returns enabled/visible/focused/checked/selected/expanded plus the current value |
| `scroll_into_view` | Bring a row in a long list/tree/grid into view using UI Automation `ScrollItem` or the container's `ScrollPattern`; `click_element` and `drag_element` also try it once automatically |
| `select_element` | Choose an item in a combo box, list box, tab strip or menu, including popup/drop-down items, by text |
| `read_table` | Extract headers and rows from a list, tree or grid as TSV/CSV/JSON, with `offset`/`max_rows` for large tables |
| `menu` | Navigate a menu path such as `File > Save As`, including WinForms/Electron menu strips |
| `file_dialog` | Fill and confirm a standard Open/Save/Select-Folder dialog by automation id, so it works on localized Windows |
| `find_text`, `click_text` | Local OCR text search/click on a window or region, returning boxes; requires Tesseract and is skipped cleanly when absent |
| `highlight_element` | Flash a colored rectangle around an element for a visual check without clicking it |
| `screenshot` | Capture a monitor, all monitors, a rectangle (`region`), or one window (even if behind another); optional OCR |
| `click`, `move`, `drag`, `scroll` | Pixel fallback for apps with no useful tree; coordinates come from the last screenshot. `scroll(target=..., window=...)` scrolls a specific pane via `ScrollPattern`, and `scroll(horizontal=true)` goes sideways |
| `type_text` | Types arbitrary Unicode (emoji, accents, tabs, newlines) |
| `press_keys`, `key_down`, `key_up`, `hold_key`, `mouse_button` | Key combos (`ctrl+c`, `alt+f4`, `enter`, `win`, ...) and modifier/mouse holds for custom sequences, selections and drags |
| `clipboard` | Get / set / paste clipboard text |
| `list_windows`, `focus_window`, `close_window`, `window_action`, `screen_info` | Window inventory, monitor layout, focus, targeted close (safer than a blind `alt+f4`; exact/active title matches win), and minimize/maximize/restore/move/resize |
| `wait_for_window`, `wait_for_element`, `wait_for_idle`, `wait_for_change` | Wait for a window to appear/close, an element to appear/close/become enabled/disabled, the UI tree to stop changing, or pixels in a window to change — no fixed sleeps |
| `wait`, `launch` | Explicit delay when needed; launch apps with arguments and wait for the new window (not an older window with the same title) |
| `pause`, `release_all` | Freeze automation and release every held key/mouse button; `pause` is also available on the **Ctrl+Alt+F12** hotkey |
| `runtime_status` | Native readiness: screen/monitors, active window, screenshot mapping, OCR, launcher lease, read-only state, pause state, desktop lock/UAC, DPI, held input, audit log and health checks |

Controls are matched by **accessible name, stable automation id, or `eN` id**, so a
field whose visible label differs from its automation id can still be targeted
directly. `snapshot(annotate=true)` is useful when the model needs to reason about
spatial layout; the text tree remains the cheaper default.

Multi-monitor is handled natively: `screen_info` shows the physical layout
(`x,y,width,height`; primary first), `screenshot(monitor=0|1|...|-1)` picks a
display, and clicks are translated back to the correct screen, including negative
origins. `window_action` accepts negative `x/y`, so it can move a window onto a
display left of or above the primary. `snapshot(window="Title")` and
`screenshot(window="Title")` target one window. Verified with a 1280x720 primary
and a 3840x2160 secondary at `(-1341,-2160)`.

**OpenCode is the only model in the loop.** There is no second GUI agent, no
delegated "do this whole task" model and no extra API key. Every tool is
deterministic native automation; OpenCode decides and calls them. Optional local
OCR (Tesseract) is text recognition, not another model.

The trade-off is deliberate: a second autonomous GUI agent adds another model,
a grounding endpoint and key management, and duplicates the planning OpenCode
already does. Here OpenCode sees the tree/screenshot, chooses the next native
action and verifies the result — one model, one policy, one audit trail.

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

# OpenCode opens in the folder you launched from by default:
cd C:\Users\Admin\projects\demo
& "C:\path\to\Computer-Use.cmd"

# or pick a different project folder / pass normal opencode flags through:
.\Computer-Use.cmd -Directory C:\Users\Admin\projects\demo
.\Computer-Use.cmd --prompt "Open Notepad and type hello"
```

### Launcher updates

When this folder is a git checkout, the launcher checks GitHub `origin/main` at
startup. If the checkout is **clean and strictly behind**, it fast-forwards with
`git pull --ff-only`. Local commits, uncommitted edits and untracked files are
never overwritten; the check is skipped with a warning instead. `.runtime\`,
`vendor\` and ignored files are untouched, so the private Python, venv, logs and
cached tools survive updates.

Launcher changes (`Computer-Use.cmd`, `Start-ComputerUse.ps1`) apply on the
**next** run; MCP server changes apply immediately because the server starts
after the update. Offline, or if git is not installed, the check is skipped and
the local version runs.

```powershell
.\Computer-Use.cmd -NoUpdate      # skip the GitHub check for this run
.\Computer-Use.cmd --no-update    # same, for scripted callers
```

The pinned OpenCode/uv/Python versions are **not** changed by auto-update; only
this launcher repository's own files move forward.

Auto-update is **launcher-only**. It is not an MCP tool, the model cannot invoke
git, and it never commits, pushes, resets, cleans or force-updates anything. It
only fast-forwards a clean checkout from `origin/main`; local commits,
uncommitted edits and untracked files are left alone. Use `-NoUpdate` to disable
the check entirely.

### Pair a phone, tablet or remote device

`opencode pair` needs OpenCode's shared background service, so pair mode cannot
use the normal private `--standalone` launch:

```powershell
.\Computer-Use.cmd pair                 # local / LAN links
.\Computer-Use.cmd pair --remote        # public OpenTunnel link (asks first)
.\Computer-Use.cmd pair --remote --yes  # accept the exposure without prompting
.\Computer-Use.cmd pair --remote --read-only   # observe only, never control
.\Computer-Use.cmd pair --remote -Directory C:\Users\Admin\projects\demo
```

In pair mode the launcher:

1. runs `opencode pair` with your flags (no `--standalone`, which pair rejects);
2. adds the `computer` MCP server to that background service at runtime, scoped
   to the launch folder;
3. waits for the MCP server to report `connected`, then keeps the window open;
4. on Enter/exit removes the runtime entry again, and stops the background
   service **only** if this launcher started it.

Keep the launcher window open while the paired device is in use. The one-time
pairing links expire after five minutes, but the tools stay attached while that
window is open. In the paired app, choose the **same folder** as the project; the
runtime entry is location-scoped, so other projects do not receive desktop tools.
For scripted use, set `COMPUTER_USE_PAIR_HOLD_SECONDS=<n>` to detach and clean up
automatically after `n` seconds.

Press Enter (or Ctrl+C) to detach; the launcher removes the runtime entry in a
`finally` block. If the process is killed hard, the entry can remain, but the MCP
lease makes it inert. Run `opencode service restart` to clear a stale entry.

The paired browser/app is only a client. OpenCode, the MCP server and every tool
call stay on this PC, scoped to the folder the launcher attaches. The phone or
remote device needs no Python, no runtime and no computer-use install.

`--remote` exposes the server to the internet (an OpenTunnel URL). Anyone with a
valid one-time link can control this PC through OpenCode, so treat pairing links
like passwords. The MCP process also carries a launcher-PID lease: if the window
is force-closed, it exits and refuses further tool calls instead of staying live.

### Read-only mode

Use this when you want someone (or a remote session) to look at the desktop but
never touch it:

```powershell
.\Computer-Use.cmd -ReadOnly
.\Computer-Use.cmd pair --remote --read-only
$env:COMPUTER_USE_READ_ONLY = "1"; .\Computer-Use.cmd pair
```

The MCP server enforces the policy itself, not just the prompt. It allows
`snapshot`, `ui_tree`, `find_elements`, `screenshot`, `read_element`,
`get_element_state`, `read_table`, `find_text`, `list_windows`, `wait_for_window`,
`wait_for_element`, `wait_for_idle`, `wait_for_change`, `wait`, `highlight_element`,
`screen_info`, `runtime_status`, `pause`, `release_all`, and
`clipboard(action="get")`. It blocks every mutating call: mouse/keyboard input,
element clicks/invokes/text writes, drags, scrolling, `scroll_into_view`,
`select_element`, `menu`, `file_dialog`, `click_text`, window focus/move/close,
clipboard writes/paste, and `launch`. The block returns a clear
`BLOCKED: ... read-only` message.

Read-only is an action policy, not a privacy filter: the inspection tools still
expose screen contents and the clipboard to the model you are using. It is also
not a Windows security boundary — the server runs as your user. Treat it as a
strong guardrail against accidental edits, not a sandbox.

**Nothing needs to be installed first** — no Node, no npm, no Bun, and no system
Python. On first run the launcher uses the portable OpenCode + `uv` already in
`vendor\` (or downloads them, ~150 MB, checksum-verified), downloads a **private
uv-managed Python 3.12** into `.runtime\tools\python` (~21 MB), builds
`.runtime\venv`, and installs:

- `mcp` (server SDK), `pyautogui`, `pillow`, `pywinauto` — the native GUI and UI
  Automation stack.

No model API key is installed or required. The first run therefore needs internet
for Python and packages, and OpenCode needs internet for models anyway. Later runs
reuse the runtime and start quickly.

If you copy the **whole folder including `.runtime`, with `tools\python` and
`tools\uv-cache` present** (a normal first run by this launcher creates them), the
first launch at the new path detects that the venv cannot run there and rebuilds it
from those bundled pieces instead of downloading Python and packages again. Virtual
environments are path-specific; `.runtime\tools\python` plus
`.runtime\tools\uv-cache` are what make a copied folder cheap to rebuild. Useful
switches:

| Switch | Effect |
| --- | --- |
| `-Reinstall` | Wipe and rebuild `.runtime\venv` |
| `-SelfTest` | Check the whole native surface without launching OpenCode: 43 tools, screenshots, clipboard (saved/restored), Unicode typing, annotated snapshots, element actions on a throwaway WinForms harness (no localized system dialogs), tables/grids, combo selection, menus, file dialog, targeted scrolling, wait states, pause/release, launch/wait, and read-only enforcement |
| `-NoUpdate` | Skip the GitHub auto-update check for this run |
| `-PrepareVendor` | Copy portable OpenCode + uv into `vendor\` (toolchain only; Python and packages are still downloaded on first run) |

An older install that still carries the previous agent packages can reclaim the
space with `-Reinstall`; the rebuilt venv contains only the native stack.

## Portable bundle (no npm/Node)

```powershell
.\Computer-Use.cmd -PrepareVendor   # portable opencode.exe + uv.exe into vendor\
```

`vendor\` removes the npm/Node requirement entirely: the folder carries the
official OpenCode binary and `uv`, so `Computer-Use.cmd` runs on a PC without
Node, npm, or a system-wide OpenCode.

`vendor\opencode\opencode.exe` is the **official compiled binary**, not a source
checkout. Bun is embedded in it, which is why no Bun/Node/npm install is needed.
A source checkout would require Bun to build and would make the fresh-PC problem
worse; the binary is the self-contained equivalent.

Python is the remaining first-run download: the launcher prefers a **uv-managed
Python 3.12 inside `.runtime\tools\python`**, using a system Python only as a
fallback. It then installs packages from PyPI.

A `.runtime\venv` by itself is path-specific. When the folder is copied, the
launcher detects a venv that cannot start and rebuilds it. If
`.runtime\tools\python` and the uv cache came along, the rebuild reuses them;
otherwise it downloads Python and packages again.

Do not commit `.runtime`, `vendor\opencode` or `vendor\uv`; they are ignored.

Approximate sizes: `vendor\` ~320 MB, `.runtime\venv` ~65 MB with only the native
stack.

`vendor\opencode\opencode.exe` is ~250 MB, so a plain GitHub repo will reject it
(100 MB file limit). Use Git LFS, or ship the prepared folder as a zip / release
asset instead of committing `vendor\`.

Tool lookup order: `vendor\` → `.runtime\tools\` (auto-download cache) → `PATH`.
That means an existing OpenCode/uv install is reused instead of downloading.

## Version pinning and updates

The toolchain is intentionally frozen to a known-good set: **OpenCode 2.3.2**,
**uv 0.11.28**, and **Python 3.12**. Auto-update only advances this launcher
repository's own scripts; it never swaps the OpenCode binary, uv or Python. The
OpenCode binary and this MCP server were tested together, so silently swapping
one component is more likely to break the MCP connection than to improve the
setup.

OpenCode still needs internet for models. The launcher's own files are checked
against GitHub at startup and fast-forwarded only when the checkout is clean and
behind (see **Launcher updates**), or you can update deliberately with `git pull`
from <https://github.com/hailongcoding/Opencode-mutation>.

If a newer OpenCode is genuinely required, treat it as a development change: edit
the `$script:OpenCodeVersion` / `$script:UvVersion` constants in
`scripts\Start-ComputerUse.ps1`, delete the corresponding files under `vendor\`,
then run `-PrepareVendor`, `-SelfTest`, and verify the `computer` tools in a real
session. Keep the previous folder so you can roll back. Do not run
`opencode upgrade` against this portable copy; it assumes an installer-managed
installation and bypasses the launcher's pins.

---

## Optional local OCR

Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) to get text
from `screenshot(ocr=true)`, `find_text` and `click_text`. Set `TESSERACT_CMD` if
it is not in `C:\Program Files\Tesseract-OCR`. `find_text`/`click_text` return the
matched text with screen boxes; `click_text` clicks the box center. OCR is local
text recognition, not an AI model, and is not required for any other tool. When
Tesseract is absent those calls return a clear "not installed" message instead of
failing.

## Pause, audit log and health

- **Ctrl+Alt+F12** toggles pause at any time. `pause(action="on"|"off"|"toggle")`
  does the same programmatically. While paused, every mouse/keyboard/window/
  launch action returns `PAUSED`, and held keys or mouse buttons are released;
  inspection, waits, `runtime_status` and clipboard reads still work.
- `release_all` releases every key or mouse button the server is holding, even
  after an interrupted `hold_key` or drag.
- Every tool call is appended as one JSON line to
  `.runtime\logs\actions.log` with timestamp, tool, arguments, outcome, duration
  and read-only state. Typed text is recorded as `<N chars>`, not its contents;
  other string arguments are truncated. The file rotates to `actions.log.1` at
  5 MB.
- `runtime_status` also reports health: launcher lease, desktop lock/UAC state,
  foreground window class and elevation, DPI awareness, held input, pause state,
  OCR path and the audit log path. Use it first when a call behaves unexpectedly.

---

## How the session scoping works

1. `Computer-Use.cmd` runs `scripts\Start-ComputerUse.ps1`.
2. The launcher creates a random **session token**, builds a small OpenCode config
   **in memory**, and exports it as `OPENCODE_CONFIG_CONTENT` for the child process.
3. It then runs `opencode --standalone` — a private server, so the injected config
   applies to that process only and cannot leak into your other OpenCode sessions.
4. The MCP server (`scripts\computer_use_mcp.py`) refuses to start unless that
   token is present.
5. When OpenCode exits, the environment variables die with the process. The
   computer-use side writes only inside this folder: `.runtime\` (venv, logs, tool
   cache) and, if prepared, `vendor\`. OpenCode still writes its own logs, auth and
   session data under your user profile — normal OpenCode behavior, not a global
   MCP grant.

**Pair mode is the one exception.** `opencode pair` rejects `--standalone` and
requires the shared background service. There the launcher does not inject
`OPENCODE_CONFIG_CONTENT`; it lets `opencode pair` ensure the service, then adds
`computer` through the runtime MCP API for the launch folder only. On exit it
deletes that entry and stops the service only if this launcher started it. The
MCP server also receives `COMPUTER_USE_PARENT_PID`; if the launcher disappears,
its lease fails and the server refuses tool calls (and exits on its own).

So: no global grant, no always-on listener while the launcher is closed, and no
background process after detach.

---

## Safety notes

- Automation moves your real mouse and keyboard. `pyautogui` failsafe is on: slam
  the pointer into a screen corner to abort. **Ctrl+Alt+F12** pauses automation
  and releases held input; `release_all` does the same for held keys/buttons.
- Every call is audited in `.runtime\logs\actions.log` (typed text redacted). The
  log is a useful record of what a session did, but it still reveals app names,
  window titles and coordinates; review it before sharing.
- Auto-update only fast-forwards a clean checkout. It never discards local
  commits or uncommitted files; use `-NoUpdate` when you want to stay exactly on
  the current revision.
- `-ReadOnly` / `--read-only` blocks actions inside the server, but inspection
  still exposes the screen and clipboard. It is a guardrail, not a sandbox.
- `invoke_element` acts through UI Automation without moving the pointer, but it is
  still a real action. Read the target name before clicking something destructive.
- Prefer `snapshot`/`click_element`/`set_element_text` over blind pixel clicks.
  When an app exposes no accessibility tree, fall back to a screenshot.
- The server can run arbitrary GUI actions as your user; treat every session like
  handing someone your desktop. Auto-update itself is not an MCP tool and adds no
  repo/shell capability, but the general `launch`/`type_text` tools still run as
  your user, so the session is not a sandbox.
- `ui_tree`, `screenshot` and clipboard reads expose whatever is on screen or in
  your clipboard to the model you are chatting with.
- Use it on a machine you control, and watch the first few runs.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| First run cannot download OpenCode/uv | Corporate proxy or restricted network. Run `-PrepareVendor` on a connected machine and copy the folder, or drop `opencode.exe`/`uv.exe` into `vendor\opencode\` and `vendor\uv\` |
| `checksum mismatch` | A proxy or antivirus modified the download. Retry the launcher; if it repeats, check the proxy/antivirus |
| OpenCode starts but shows `0 MCP` | Give it a few seconds; if it stays 0, check `.runtime\logs\sessions.log` and the OpenCode log (`~/.local/share/opencode/log/opencode.log`) |
| No `computer` tools in the session | Exit OpenCode fully, relaunch via `Computer-Use.cmd` (not `opencode` directly) |
| Paired app shows no `computer` tools | In the paired app, open the **same folder** the launcher printed. Runtime MCP entries are location-scoped, so another project has no tools |
| `pair` says a `computer` server already exists | Close the other Computer-Use window, or run `opencode service restart` to clear a stale runtime entry, then retry |
| Pair mode window was force-closed | The MCP lease makes the old server inert. Relaunch `pair`; if the service still lists a stale entry, run `opencode service restart` |
| `Refusing to start: no active launcher session` | Start through `Computer-Use.cmd`; that message means the server was run outside the launcher |
| `BLOCKED: this Computer-Use session is read-only` | Relaunch without `-ReadOnly` / `--read-only` (and clear `COMPUTER_USE_READ_ONLY`) if the session is supposed to act |
| Every action returns `PAUSED` | Press **Ctrl+Alt+F12** or call `pause(action="off")`; `runtime_status` shows `paused` |
| Auto-update says it was skipped | The checkout has local commits, uncommitted edits or untracked files, git is missing, or GitHub is unreachable. Commit/stash your work, or use `-NoUpdate`; local work is never overwritten |
| `file_dialog` fills the box but the dialog stays open | Pass `window=<exact title>`; the tool prefers the default button (automation id 1) over the split-button drop-down, and reports the method it used |
| `read_table` returns no rows | Pass `target=<list/grid element>` explicitly and raise `max_rows`; virtualized rows may need `scroll(target=..., amount=...)` first |
| `scroll_into_view` cannot find a far row | Scroll the container with `scroll(target=..., amount=...)` until the row is exposed, then call `scroll_into_view`; some apps only materialize visible rows |
| Clicks land slightly off | Screenshots are scaled; always click using coordinates from the most recent `screenshot`, and capture the monitor that holds the target window |
| `snapshot`/`ui_tree` shows no controls | That app exposes no useful UI Automation tree (some games, some Tk/Electron builds). Use `screenshot` + `click`/`type_text` instead |
| `snapshot`/`ui_tree` is slow or hangs | One control's accessibility provider is stalling. Retry with a smaller `max_items`, target the exact window, or use `screenshot` for that app |
| `unknown element id` or an action hits the wrong control | Ids belong to the latest snapshot. Take a fresh `snapshot`/`ui_tree` after the UI changes (or use `click_element` with a name) |
| `click_element` does nothing on a button | Try `invoke_element` (UI Automation pattern) or focus the window first; some custom-drawn controls are not clickable targets |
| `type_text` returns "typed ..." but nothing appears | The focused control is not a text field, or the target app runs **elevated** (Windows blocks input from a normal process to an admin window). Click the field first; run the launcher elevated if you really need it |
| `focus_window` says Windows kept another window in front | Retries and the AttachThreadInput workaround are already applied; if it still fails, click the window once by hand. Windows also forbids a background app from stealing focus from a foreground one |
| Clipboard says "another app is holding it" | A clipboard manager is mid-write; the tool retries, then falls back to PowerShell. Try again |
| A window is missing from `list_windows` | It is filtered only when it has no title or zero area. Look for the `[off-screen]` marker — `focus_window` moves such a window back onto the primary display |
| Need a clean slate | Delete `.runtime\` and run again (keeps `vendor\`) |

Logs from the MCP server sessions land in `.runtime\logs\sessions.log`; the
per-call audit trail is `.runtime\logs\actions.log` (rotated to `actions.log.1`).

## Files

```text
Computer-Use.cmd              <- double-click this (session-only launcher)
scripts\Start-ComputerUse.ps1 <- bootstrap: auto-update, portable tools, runtime, token, launch
scripts\computer_use_mcp.py   <- the computer-use MCP server (43 tools)
scripts\selftest.py           <- -SelfTest implementation
scripts\scratch_editor.py     <- throwaway Tk window the self-test types into
scripts\scratch_uia.ps1       <- throwaway WinForms window with stable UIA controls
scripts\update_selftest.ps1   <- offline test of auto-update against a temporary repo
vendor\opencode\opencode.exe  <- portable OpenCode (compiled binary, embedded Bun)
vendor\uv\uv.exe              <- portable uv (fetches Python/packages unless bundled)
.runtime\tools                <- private Python + downloaded tool/uv cache
.runtime\venv                 <- isolated venv (native MCP dependencies)
.runtime\logs                 <- session log + per-call audit log
```

The vendored OpenCode binary is MIT-licensed and unmodified; version pins are the
`$script:OpenCodeVersion` / `$script:UvVersion` constants in
`Start-ComputerUse.ps1`.

## Uninstall

Delete this folder. Nothing else was modified.
