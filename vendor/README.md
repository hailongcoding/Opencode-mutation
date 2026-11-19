# vendor

Portable toolchain, used first by `..\Computer-Use.cmd` when present (otherwise the
launcher falls back to an existing install on `PATH`, or downloads into
`.runtime\tools`).

| File | Origin |
| --- | --- |
| `opencode\opencode.exe` | OpenCode v2.3.2, official `opencode-windows-x64.zip` from <https://github.com/anomalyco/opencode/releases> — unmodified, MIT (`opencode\LICENSE`) |
| `uv\uv.exe` | uv 0.11.28, official `uv-x86_64-pc-windows-msvc.zip` from <https://github.com/astral-sh/uv/releases> — unmodified, MIT / Apache-2.0 (`uv\LICENSE-MIT`, `uv\LICENSE-APACHE`) |

Both are pinned. There is no refresh/update switch. To change them, edit
`$script:OpenCodeVersion` / `$script:UvVersion` in
`..\scripts\Start-ComputerUse.ps1`, delete the corresponding files here, and run
`..\Computer-Use.cmd -PrepareVendor` to fetch the new pinned pair.

Downloads are checked against the projects' published SHA-256 sums before use.
