"""
Weird Traveler — single-exe entry point.

Dispatches to the browser or the viewer based on argv:

    Weird Traveler.exe                   -> Weird Traveler browser
    Weird Traveler.exe  C:\Photos        -> browser starting in that folder
    Weird Traveler.exe  --viewer a.wif   -> Weird Viewer for that file
"""

import sys
from pathlib import Path


def run_browser(start_path=None):
    import tkinter as tk
    from wif_browser import WifBrowser
    root = tk.Tk()
    root.state("zoomed")
    app = WifBrowser(root)
    if start_path and start_path.is_dir():
        app._navigate(start_path)
    root.mainloop()


def run_viewer(file_path=None):
    import tkinter as tk
    from wif_viewer import WifViewer
    root = tk.Tk()
    root.state("zoomed")
    app = WifViewer(root)
    if file_path and file_path.exists():
        app.load(file_path)
    root.mainloop()


def main():
    args = sys.argv[1:]

    if args and args[0] == "--viewer":
        path = Path(args[1]) if len(args) > 1 else None
        run_viewer(path)
    else:
        path = Path(args[0]) if args else None
        run_browser(path)


if __name__ == "__main__":
    main()
