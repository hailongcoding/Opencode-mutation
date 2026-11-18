# vendor

Portable toolchain, used first by `..\Computer-Use.cmd` when present (otherwise the
launcher falls back to an existing install on `PATH`, or downloads into
`.runtime\tools`).

| File | Origin |
| --- | --- |
| `opencode\opencode.exe` | OpenCode v2.3.2, official `opencode-windows-x64.zip` from <https://github.com/anomalyco/opencode/releases> — unmodified, MIT (`opencode\LICENSE`) |
| `uv\uv.exe` | uv 0.11.28, official `uv-x86_64-pc-windows-msvc.zip` from <https://github.com/astral-sh/uv/releases> — unmodified, MIT / Apache-2.0 (`uv\LICENSE-MIT`, `uv\LICENSE-APACHE`) |

Refresh both with:

```powershell
..\Computer-Use.cmd -RefreshTools -PrepareVendor
```

Downloads are checked against the projects' published SHA-256 sums before use.
