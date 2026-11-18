"""Tiny scratch editor used by the self-test.

Runs as its own process so "typing reaches another application" can be verified
end to end without touching any real document:

  * the self-test launches this window and types into it
  * this process mirrors the widget contents to a file every 150 ms
  * the self-test compares that file with what it typed

Closing it is a plain WM_CLOSE with nothing to save, so the test never has to
press Ctrl+Z or answer a save/discard dialog in somebody else's editor.
"""

import pathlib
import sys
import tkinter as tk


def main() -> int:
    output = pathlib.Path(sys.argv[1])
    title = sys.argv[2]

    root = tk.Tk()
    root.title(title)
    root.geometry("620x200+60+60")
    text = tk.Text(root, font=("Segoe UI", 14), undo=False)
    text.pack(fill="both", expand=True)
    text.focus_set()

    state = {"last": None}

    def pump() -> None:
        current = text.get("1.0", "end-1c")
        if current != state["last"]:
            state["last"] = current
            try:
                output.write_text(current, encoding="utf-8")
            except OSError:
                pass
        root.after(150, pump)

    pump()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
