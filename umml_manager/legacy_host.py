from __future__ import annotations

import argparse
import tkinter as tk
from tkinter import messagebox

from .window_icon import apply_window_icon
from .startup import close_splash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="UMML Manager legacy-tool compatibility host"
    )
    parser.add_argument("--tool", default="")
    args = parser.parse_args(argv)

    import UMML as application

    root = tk.Tk()
    apply_window_icon(root)
    gui = application.ModLoaderGUI(root)
    close_splash()
    root.title("UMML Studio — Legacy compatibility tools")

    if args.tool:
        method = getattr(gui, args.tool, None)
        if not callable(method):
            messagebox.showerror(
                "Tool unavailable",
                f"Legacy tool is unavailable: {args.tool}",
                parent=root,
            )
        else:
            root.after(150, method)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
