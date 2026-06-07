"""
Viewer for .wif image files (and standard images: PNG/JPG/GIF/WebP/AVIF/…).

Usage:
    python wif_viewer.py              -> opens file picker
    python wif_viewer.py image.wif   -> opens that file directly

Keyboard shortcuts:
    Ctrl+O        Open file
    Ctrl+S        Save current view as an image
    Left/Right    Previous / next image in the same folder
    +/-           Zoom in / out          (Ctrl+wheel also zooms)
    0             Reset zoom to 100%
    R             Rotate 90° clockwise
    H / V         Flip horizontal / vertical
    B             Toggle auto colour balance
    A             Show / hide the Adjust panel (sliders)
    Space         Play / pause animation
    Del           Delete current file
    F11           Toggle fullscreen (maximised)
"""

import json
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTk, ImageSequence

try:
    import pillow_avif   # registers AVIF codec with Pillow
except ImportError:
    pass

import wif_format

ZOOM_STEPS = [0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".ico", ".avif"}


def secure_delete(path: Path):
    """Overwrite file contents with zeros, flush to disk, then delete."""
    size = path.stat().st_size
    with open(path, "r+b") as fh:
        fh.write(b"\x00" * size)
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()


class WifViewer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Weird Viewer")
        try:
            _icon = Path(__file__).parent / "wif_icon.ico"
            if _icon.exists():
                self.root.iconbitmap(default=str(_icon))
        except Exception:
            pass
        self.root.configure(bg="#1e1e1e")
        self.root.minsize(300, 200)
        self.root.state("zoomed")  # start maximised (Windows fullscreen)

        self._source_img = None   # original PIL image, never scaled
        self._tk_image = None     # currently displayed ImageTk
        self._current_path = None
        self._siblings = []
        self._zoom = 1.0
        self._stretched = False
        self._file_info = ""
        self._rotation = 0                # display rotation: 0/90/180/270 (clockwise)
        self._flip_h    = False           # mirror left-right
        self._flip_v    = False           # mirror top-bottom
        self._fit_ratio = 1.0             # last fit-to-window ratio (keeps zoom continuous)

        # Image adjustments — applied non-destructively by _apply_enhancements().
        # They persist as you navigate, so a folder shot in the same light stays
        # corrected; the Reset button (or Reset adjustments) clears them.
        self._auto_balance = False        # auto colour-balance (per-channel contrast stretch)
        self._equalize     = False        # histogram equalisation
        self._brightness   = 1.0
        self._contrast     = 1.0
        self._saturation   = 1.0
        self._gamma        = 1.0          # >1 lifts shadows, <1 deepens them
        self._sharpen      = 0            # unsharp-mask strength (percent); 0 = off
        self._suspend_render = False      # set sliders in a batch without re-rendering
        self._adjust_visible = False
        self._var_brightness = tk.DoubleVar(value=1.0)
        self._var_contrast   = tk.DoubleVar(value=1.0)
        self._var_saturation = tk.DoubleVar(value=1.0)
        self._var_gamma      = tk.DoubleVar(value=1.0)
        self._var_sharpen    = tk.IntVar(value=0)

        # Passwords reused across this session for encrypted files.  When the
        # browser launches the viewer it hands its known passwords over via the
        # WIF_PW env var so you aren't prompted again.
        try:
            self._passwords: list = [p for p in json.loads(os.environ.get("WIF_PW", "[]")) if p]
        except (ValueError, TypeError):
            self._passwords = []

        # Animation state (GIF / WebP / APNG / AVIF …)
        self._anim_fmt: str        = ""
        self._frames: list         = []
        self._frame_durations: list = []
        self._current_frame: int   = 0
        self._playing: bool        = False
        self._anim_job             = None

        self._build_menu()
        self._build_toolbar()
        self._build_adjust_panel()
        self._build_canvas()
        self._build_infobar()
        self._bind_keys()

    # ------------------------------------------------------------------ UI build

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_file)
        file_menu.add_command(label="Save as image...", accelerator="Ctrl+S", command=self.save_as)
        file_menu.add_separator()
        file_menu.add_command(label="Delete current file", accelerator="Del",
                              command=self.delete_current)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Zoom in",  accelerator="+", command=self.zoom_in)
        view_menu.add_command(label="Zoom out", accelerator="-", command=self.zoom_out)
        view_menu.add_command(label="Reset zoom (100%)", accelerator="0", command=self.zoom_reset)
        view_menu.add_separator()
        view_menu.add_command(label="Stretch to window",   command=self.stretch)
        view_menu.add_command(label="Unstretch (original)", command=self.unstretch)
        view_menu.add_command(label="Rotate 90° (clockwise)", accelerator="R", command=self.rotate)
        view_menu.add_command(label="Flip horizontal", accelerator="H", command=self.flip_horizontal)
        view_menu.add_command(label="Flip vertical",   accelerator="V", command=self.flip_vertical)
        view_menu.add_separator()
        view_menu.add_command(label="Auto colour balance", accelerator="B",
                              command=self._toggle_auto_balance)
        view_menu.add_command(label="Equalize histogram", command=self._toggle_equalize)
        view_menu.add_separator()
        view_menu.add_command(label="Adjustments panel", accelerator="A", command=self._toggle_adjust)
        view_menu.add_command(label="Reset adjustments", command=self.reset_adjustments)
        menubar.add_cascade(label="View", menu=view_menu)

        self.root.config(menu=menubar)

    def _build_toolbar(self):
        tb = tk.Frame(self.root, bg="#2d2d2d", pady=3)
        tb.pack(side=tk.TOP, fill=tk.X)
        self._toolbar = tb

        btn_cfg = dict(bg="#3c3c3c", fg="#cccccc", relief=tk.FLAT,
                       activebackground="#555", activeforeground="white",
                       padx=8, pady=2)

        tk.Button(tb, text="◀", command=self.prev_file, **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="▶", command=self.next_file, **btn_cfg).pack(side=tk.LEFT, padx=2)

        # Play/Pause group — only visible for animated GIFs
        self._play_frame = tk.Frame(tb, bg="#2d2d2d")
        tk.Frame(self._play_frame, bg="#555", width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)
        self._play_btn = tk.Button(self._play_frame, text="▶  Play",
                                   command=self._toggle_play, **btn_cfg)
        self._play_btn.pack(side=tk.LEFT, padx=2)
        # hidden by default; load() shows it when a GIF with multiple frames is opened

        tk.Frame(tb, bg="#555", width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)

        tk.Button(tb, text="Stretch",   command=self.stretch,   **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="Unstretch", command=self.unstretch, **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="⟳  Rotate", command=self.rotate,    **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="↔  Flip",   command=self.flip_horizontal, **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="↕  Flip",   command=self.flip_vertical,   **btn_cfg).pack(side=tk.LEFT, padx=2)

        tk.Frame(tb, bg="#555", width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)

        tk.Button(tb, text="−", command=self.zoom_out,   **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="+", command=self.zoom_in,    **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(tb, text="100%", command=self.zoom_reset, **btn_cfg).pack(side=tk.LEFT, padx=2)

        tk.Frame(tb, bg="#555", width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)

        self._balance_btn = tk.Button(tb, text="⚖  Auto color",
                                      command=self._toggle_auto_balance, **btn_cfg)
        self._balance_btn.pack(side=tk.LEFT, padx=2)

        self._adjust_btn = tk.Button(tb, text="🎚  Adjust", command=self._toggle_adjust, **btn_cfg)
        self._adjust_btn.pack(side=tk.LEFT, padx=2)

        self._zoom_var = tk.StringVar(value="")
        tk.Label(tb, textvariable=self._zoom_var, bg="#2d2d2d", fg="#aaaaaa",
                 width=6, anchor="w").pack(side=tk.LEFT, padx=4)

        # Delete — far right, red-tinted to signal a destructive action
        self._delete_btn = tk.Button(
            tb, text="🗑  Delete", command=self.delete_current,
            bg="#3c3c3c", fg="#e08080", relief=tk.FLAT,
            activebackground="#883333", activeforeground="white", padx=8, pady=2)
        self._delete_btn.pack(side=tk.RIGHT, padx=(2, 8))

    def _build_adjust_panel(self):
        """Collapsible row of tonal/colour sliders, hidden until 🎚 Adjust is pressed."""
        p = tk.Frame(self.root, bg="#262626", pady=4)
        self._adjust_panel = p   # shown/hidden by _toggle_adjust(); not packed yet

        sl_cfg = dict(bg="#262626", fg="#cccccc", troughcolor="#3c3c3c",
                      highlightthickness=0, orient=tk.HORIZONTAL, length=130,
                      sliderrelief=tk.FLAT, activebackground="#6a6a6a")

        def add_slider(text, var, frm, to, res):
            tk.Scale(p, label=text, variable=var, from_=frm, to=to, resolution=res,
                     command=self._on_adjust, **sl_cfg).pack(side=tk.LEFT, padx=4)

        add_slider("Brightness", self._var_brightness, 0.2, 2.0, 0.05)
        add_slider("Contrast",   self._var_contrast,   0.2, 2.0, 0.05)
        add_slider("Saturation", self._var_saturation, 0.0, 2.0, 0.05)
        add_slider("Gamma",      self._var_gamma,      0.2, 2.5, 0.05)
        add_slider("Sharpen",    self._var_sharpen,    0,   300, 10)

        tk.Frame(p, bg="#555", width=1).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=6)

        btn_cfg = dict(bg="#3c3c3c", fg="#cccccc", relief=tk.FLAT,
                       activebackground="#555", activeforeground="white", padx=8, pady=2)
        self._equalize_btn = tk.Button(p, text="Equalize", command=self._toggle_equalize, **btn_cfg)
        self._equalize_btn.pack(side=tk.LEFT, padx=2)
        tk.Button(p, text="↺  Reset", command=self.reset_adjustments, **btn_cfg).pack(side=tk.LEFT, padx=2)

    def _build_canvas(self):
        frame = tk.Frame(self.root, bg="#1e1e1e")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(frame, bg="#1e1e1e", highlightthickness=0)
        self._vbar = tk.Scrollbar(frame, orient=tk.VERTICAL,   command=self.canvas.yview)
        self._hbar = tk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._vbar.set,
                              xscrollcommand=self._hbar.set,
                              yscrollincrement=1, xscrollincrement=1)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        # scrollbars start hidden; _render() shows them when needed

        # Mouse wheel: plain = vertical scroll, Shift = horizontal, Ctrl = zoom
        self.canvas.bind("<MouseWheel>",         self._on_scroll_y)
        self.canvas.bind("<Shift-MouseWheel>",   self._on_scroll_x)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_scroll)
        # Re-render on resize so stretch mode keeps filling the canvas
        self.canvas.bind("<Configure>", lambda e: self._render())

        # Click-and-drag panning
        self.canvas.bind("<ButtonPress-1>",   self._on_drag_start)
        self.canvas.bind("<B1-Motion>",       self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)

    def _build_infobar(self):
        self.info_var = tk.StringVar(value="Open a .wif file to view it  (Ctrl+O)")
        tk.Label(self.root, textvariable=self.info_var, bg="#2d2d2d", fg="#cccccc",
                 anchor="w", padx=8, pady=4).pack(side=tk.BOTTOM, fill=tk.X)

    def _bind_keys(self):
        self.root.bind("<Control-o>",  lambda e: self.open_file())
        self.root.bind("<F11>",        lambda e: self._toggle_fullscreen())
        self.root.bind("<Right>",      lambda e: self.next_file())
        self.root.bind("<Left>",       lambda e: self.prev_file())
        self.root.bind("<plus>",       lambda e: self.zoom_in())
        self.root.bind("<KP_Add>",     lambda e: self.zoom_in())
        self.root.bind("<minus>",      lambda e: self.zoom_out())
        self.root.bind("<KP_Subtract>",lambda e: self.zoom_out())
        self.root.bind("0",            lambda e: self.zoom_reset())
        self.root.bind("<space>",      lambda e: self._toggle_play())
        self.root.bind("<Delete>",     lambda e: self.delete_current())
        self.root.bind("<Control-s>",  lambda e: self.save_as())
        self.root.bind("b",            lambda e: self._toggle_auto_balance())
        self.root.bind("B",            lambda e: self._toggle_auto_balance())
        self.root.bind("r",            lambda e: self.rotate())
        self.root.bind("R",            lambda e: self.rotate())
        self.root.bind("h",            lambda e: self.flip_horizontal())
        self.root.bind("H",            lambda e: self.flip_horizontal())
        self.root.bind("v",            lambda e: self.flip_vertical())
        self.root.bind("V",            lambda e: self.flip_vertical())
        self.root.bind("a",            lambda e: self._toggle_adjust())
        self.root.bind("A",            lambda e: self._toggle_adjust())

    # ------------------------------------------------------------------ file nav

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Open image",
            filetypes=[
                ("All images", "*.wif *.jpg *.jpeg *.png *.gif *.bmp *.tiff *.tif *.webp *.ico *.avif"),
                ("WIF images", "*.wif"),
                ("Standard images", "*.jpg *.jpeg *.png *.gif *.bmp *.tiff *.tif *.webp *.ico *.avif"),
                ("All files", "*.*"),
            ]
        )
        if path:
            self.load(Path(path))

    def save_as(self):
        """Export the currently shown image to a standard format (PNG/JPG/…).
        This is how you get an image back out of a .wif (decrypted, if it was)."""
        if self._source_img is None:
            return
        stem = self._current_path.stem if self._current_path else "image"
        dst = filedialog.asksaveasfilename(
            title="Save image as",
            initialfile=stem + ".png",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("BMP", "*.bmp"), ("WEBP", "*.webp")],
        )
        if not dst:
            return
        img = self._apply_enhancements(self._display_image())
        # JPEG/BMP can't store alpha — flatten if needed
        if Path(dst).suffix.lower() in (".jpg", ".jpeg", ".bmp") and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        try:
            img.save(dst)
            self.info_var.set(f"Saved  →  {dst}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _decode_wif_file(self, path: Path):
        """Decode a .wif file, reusing session passwords and prompting only when
        an encrypted file matches none of them.  Returns (PIL Image, info str).
        Raises wif_format.WrongPassword if the user cancels an unlock."""
        data = path.read_bytes()
        if not wif_format.is_encrypted(data):
            img, meta = wif_format.decode(data)
            state = "compressed" if meta["compressed"] else "uncompressed"
            return img, f"WIF v{meta['version']} · {state}"

        # Encrypted — try what we already know, then prompt as a last resort
        try:
            img, meta, _ = wif_format.decode_try(data, self._passwords)
            return img, "WIF v2 · 🔒 encrypted"
        except wif_format.WrongPassword:
            pass

        prompt = f"“{path.name}” is encrypted.\nEnter password:"
        while True:
            pw = simpledialog.askstring("Password", prompt, show="*", parent=self.root)
            if not pw:
                raise wif_format.WrongPassword("cancelled")
            try:
                img, meta = wif_format.decode(data, pw)
                if pw not in self._passwords:
                    self._passwords.append(pw)   # remember for the rest of the session
                return img, "WIF v2 · 🔒 encrypted"
            except wif_format.WrongPassword:
                # Wrong password — re-ask, folding the notice into the same dialog
                prompt = (f"Wrong password — try again.\n\n"
                          f"“{path.name}” is encrypted.\nEnter password:")

    def load(self, path: Path):
        self._stop_animation()   # cancel any running GIF before loading new file

        try:
            if path.suffix.lower() == ".wif":
                img, info = self._decode_wif_file(path)
            else:
                img  = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
                info = path.suffix.upper().lstrip(".")
        except wif_format.WrongPassword:
            # Cancelled / not unlocked — keep whatever was already showing
            if self._source_img is None:
                self.info_var.set(f"{path.name} is encrypted — not unlocked")
            return
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        self._source_img   = img
        self._current_path = path
        self._file_info    = info
        self._stretched    = True
        self._zoom         = 1.0
        self._rotation     = 0    # each newly opened image starts un-rotated
        self._flip_h       = False
        self._flip_v       = False

        # Extract frames for animated images (GIF, WebP, APNG, …)
        self._frames          = []
        self._frame_durations = []
        self._current_frame   = 0
        if path.suffix.lower() != ".wif":
            try:
                raw = Image.open(path)
                if getattr(raw, "n_frames", 1) > 1:
                    for frame in ImageSequence.Iterator(raw):
                        self._frames.append(frame.copy().convert("RGBA"))
                        dur = frame.info.get("duration", 100)
                        self._frame_durations.append(max(20, dur))  # clamp: 0 ms = "as fast as possible"
            except Exception:
                self._frames = []

        if len(self._frames) > 1:
            n = len(self._frames)
            self._anim_fmt  = info
            self._file_info = f"{info}  1/{n}"
            self._play_frame.pack(side=tk.LEFT, after=None)  # insert after nav buttons
            self._start_animation()
        else:
            self._play_frame.pack_forget()

        # Collect all viewable siblings (wif + standard images), sorted by name
        viewable = {".wif"} | IMAGE_EXTS
        self._siblings = sorted(
            [p for p in path.parent.iterdir()
             if p.is_file() and p.suffix.lower() in viewable],
            key=lambda p: p.name.lower()
        )

        self.root.update_idletasks()  # ensure canvas dimensions are resolved before rendering
        self._render()

    def next_file(self):
        self._navigate(1)

    def prev_file(self):
        self._navigate(-1)

    def _navigate(self, direction: int):
        if not self._siblings or self._current_path not in self._siblings:
            return
        idx = self._siblings.index(self._current_path)
        new_idx = (idx + direction) % len(self._siblings)
        self.load(self._siblings[new_idx])

    def delete_current(self):
        """Confirm, zero-overwrite, delete the open file, then show the next one."""
        if self._current_path is None:
            return
        path = self._current_path

        if not messagebox.askyesno(
            "Delete file",
            f"Delete this file?\n\n{path.name}\n\n"
            "It will be overwritten with zeros before deletion.",
            icon="warning",
        ):
            return

        self._stop_animation()   # release any GIF frames referencing the file
        try:
            secure_delete(path)
        except Exception as e:
            messagebox.showerror("Delete failed", str(e))
            return

        idx = self._siblings.index(path) if path in self._siblings else -1
        self._siblings = [p for p in self._siblings if p != path]

        if self._siblings:
            # Show whatever slid into this slot (clamped to the new last item)
            self.load(self._siblings[min(max(idx, 0), len(self._siblings) - 1)])
        else:
            self._source_img   = None
            self._current_path = None
            self._frames       = []
            self.canvas.delete("all")
            self.root.title("Weird Viewer")
            self._zoom_var.set("")
            self.info_var.set("No image  —  folder is empty  (Ctrl+O to open another)")

    # ------------------------------------------------------------------ view

    def stretch(self):
        self._stretched = True
        self._zoom = 1.0
        self._render()

    def unstretch(self):
        self._stretched = False
        self._render()

    def zoom_in(self):
        if self._stretched:
            self._zoom = self._fit_ratio   # continue from the size shown on screen
        self._stretched = False
        nxt = next((z for z in ZOOM_STEPS if z > self._zoom + 1e-6), None)
        if nxt is not None:
            self._zoom = nxt
        self._render()

    def zoom_out(self):
        if self._stretched:
            self._zoom = self._fit_ratio
        self._stretched = False
        prv = next((z for z in reversed(ZOOM_STEPS) if z < self._zoom - 1e-6), None)
        if prv is not None:
            self._zoom = prv
        self._render()

    def zoom_reset(self):
        self._stretched = False
        self._zoom = 1.0
        self._render()

    def _on_drag_start(self, event):
        self.canvas.config(cursor="fleur")
        self.canvas.scan_mark(event.x, event.y)

    def _on_drag_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=3)

    def _on_drag_end(self, event):
        self.canvas.config(cursor="")

    def _toggle_fullscreen(self):
        state = self.root.state()
        self.root.state("normal" if state == "zoomed" else "zoomed")

    def _on_ctrl_scroll(self, event):
        if event.delta > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def _on_scroll_y(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120) * 40, "units")

    def _on_scroll_x(self, event):
        self.canvas.xview_scroll(int(-event.delta / 120) * 40, "units")

    # ------------------------------------------------------------------ GIF animation

    def _start_animation(self):
        self._playing = True
        self._play_btn.configure(text="⏸  Pause")
        self._animate()

    def _stop_animation(self):
        self._playing = False
        if self._anim_job is not None:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None
        if hasattr(self, "_play_btn"):
            self._play_btn.configure(text="▶  Play")

    def _toggle_play(self):
        if not self._frames:   # not a GIF — ignore Space
            return
        if self._playing:
            self._stop_animation()
        else:
            self._start_animation()

    def _animate(self):
        if not self._playing or not self._frames:
            return
        self._current_frame = (self._current_frame + 1) % len(self._frames)
        self._source_img    = self._frames[self._current_frame]
        self._file_info     = f"{self._anim_fmt}  {self._current_frame + 1}/{len(self._frames)}"
        self._render()
        delay = self._frame_durations[self._current_frame]
        self._anim_job = self.root.after(delay, self._animate)

    def rotate(self):
        """Rotate the view 90° clockwise each press; the 4th press returns to 0°."""
        self._rotation = (self._rotation + 90) % 360
        self._render()

    def flip_horizontal(self):
        """Mirror left-to-right (non-destructive; Save as exports the flipped view)."""
        self._flip_h = not self._flip_h
        self._render()

    def flip_vertical(self):
        """Mirror top-to-bottom (non-destructive; Save as exports the flipped view)."""
        self._flip_v = not self._flip_v
        self._render()

    def _display_image(self):
        """Source with geometry only (flips + rotation), full resolution.  Tonal and
        colour adjustments are applied separately by _apply_enhancements(), so the
        preview can run on the small on-screen image rather than the full-size one."""
        img = self._source_img
        if self._flip_h:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if self._flip_v:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        if self._rotation:
            img = img.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180,
                                 270: Image.ROTATE_90}[self._rotation])
        return img

    # ------------------------------------------------------------------ adjustments

    def _on_adjust(self, *_):
        """Slider callback: pull every slider value into state, then re-render."""
        self._brightness = self._var_brightness.get()
        self._contrast   = self._var_contrast.get()
        self._saturation = self._var_saturation.get()
        self._gamma      = self._var_gamma.get()
        self._sharpen    = self._var_sharpen.get()
        if not self._suspend_render:
            self._render()

    def _toggle_adjust(self):
        """Show / hide the slider panel directly under the toolbar."""
        if self._adjust_visible:
            self._adjust_panel.pack_forget()
            self._adjust_btn.configure(bg="#3c3c3c")
        else:
            self._adjust_panel.pack(side=tk.TOP, fill=tk.X, after=self._toolbar)
            self._adjust_btn.configure(bg="#3a5a8a")
        self._adjust_visible = not self._adjust_visible

    def _toggle_auto_balance(self):
        self._auto_balance = not self._auto_balance
        self._update_balance_btn()
        self._render()

    def _update_balance_btn(self):
        on = self._auto_balance
        self._balance_btn.configure(text="⚖  Auto color ✓" if on else "⚖  Auto color",
                                    bg="#3a5a8a" if on else "#3c3c3c")

    def _toggle_equalize(self):
        self._equalize = not self._equalize
        self._update_equalize_btn()
        self._render()

    def _update_equalize_btn(self):
        on = self._equalize
        self._equalize_btn.configure(text="Equalize ✓" if on else "Equalize",
                                     bg="#3a5a8a" if on else "#3c3c3c")

    def reset_adjustments(self):
        """Clear every tonal/colour adjustment back to the original image."""
        self._suspend_render = True
        self._var_brightness.set(1.0)
        self._var_contrast.set(1.0)
        self._var_saturation.set(1.0)
        self._var_gamma.set(1.0)
        self._var_sharpen.set(0)
        self._equalize     = False
        self._auto_balance = False
        self._suspend_render = False
        self._on_adjust()
        self._update_balance_btn()
        self._update_equalize_btn()
        self._render()

    def _apply_enhancements(self, img):
        """Apply the active tonal/colour/sharpen adjustments to `img` (any size).
        Run on the small on-screen image for a fast preview, and on the full-size
        image by Save as.  Alpha is kept aside so it isn't affected."""
        if not (self._auto_balance or self._equalize or self._sharpen > 0
                or abs(self._gamma - 1.0) > 1e-3 or abs(self._brightness - 1.0) > 1e-3
                or abs(self._contrast - 1.0) > 1e-3 or abs(self._saturation - 1.0) > 1e-3):
            return img

        alpha = None
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba  = img.convert("RGBA")
            alpha = rgba.getchannel("A")
            work  = rgba.convert("RGB")
        else:
            work = img.convert("RGB")

        if self._auto_balance:
            work = ImageOps.autocontrast(work, cutoff=1)
        if self._equalize:
            work = ImageOps.equalize(work)
        if abs(self._gamma - 1.0) > 1e-3:
            inv = 1.0 / self._gamma
            lut = [min(255, int((i / 255.0) ** inv * 255 + 0.5)) for i in range(256)]
            work = work.point(lut * 3)
        if abs(self._brightness - 1.0) > 1e-3:
            work = ImageEnhance.Brightness(work).enhance(self._brightness)
        if abs(self._contrast - 1.0) > 1e-3:
            work = ImageEnhance.Contrast(work).enhance(self._contrast)
        if abs(self._saturation - 1.0) > 1e-3:
            work = ImageEnhance.Color(work).enhance(self._saturation)
        if self._sharpen > 0:
            work = work.filter(ImageFilter.UnsharpMask(radius=2, percent=int(self._sharpen), threshold=3))

        if alpha is not None:
            work = work.convert("RGBA")
            work.putalpha(alpha)
        return work

    def _render(self):
        if self._source_img is None:
            return

        img = self._display_image()
        orig_w, orig_h = img.size

        if self._stretched:
            cw = self.canvas.winfo_width()  or orig_w
            ch = self.canvas.winfo_height() or orig_h
            ratio = min(cw / orig_w, ch / orig_h)
            self._fit_ratio = ratio   # remembered so zoom can continue from this size
            new_size = (max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio)))
            display = img.resize(new_size, Image.LANCZOS)
            zoom_label = f"{ratio*100:.0f}%"
        else:
            new_size = (max(1, int(orig_w * self._zoom)), max(1, int(orig_h * self._zoom)))
            display = img.resize(new_size, Image.LANCZOS) if self._zoom != 1.0 else img
            zoom_label = f"{self._zoom*100:.0f}%"

        display = self._apply_enhancements(display)
        self._tk_image = ImageTk.PhotoImage(display)
        dw, dh = display.size

        cw = self.canvas.winfo_width()  or self.root.winfo_screenwidth()
        ch = self.canvas.winfo_height() or self.root.winfo_screenheight()

        # Center image when it fits inside the canvas; otherwise anchor top-left for panning
        x = cw // 2 if dw <= cw else dw // 2
        y = ch // 2 if dh <= ch else dh // 2

        self.canvas.config(scrollregion=(0, 0, max(dw, cw), max(dh, ch)))
        self.canvas.delete("all")
        self.canvas.create_image(x, y, anchor=tk.CENTER, image=self._tk_image)

        # Show scrollbars only when the image exceeds the canvas size
        if dw > cw:
            self._hbar.grid(row=1, column=0, sticky="ew")
        else:
            self._hbar.grid_remove()
        if dh > ch:
            self._vbar.grid(row=0, column=1, sticky="ns")
        else:
            self._vbar.grid_remove()

        idx = self._siblings.index(self._current_path) + 1 if self._current_path in self._siblings else "?"
        total = len(self._siblings)

        self.root.title(f"Weird Viewer — {self._current_path.name}")
        self._zoom_var.set(zoom_label)
        self.info_var.set(
            f"{self._current_path.name}  |  {orig_w} × {orig_h}  |  "
            f"{self._current_path.stat().st_size:,} bytes  |  {self._file_info}  |  "
            f"{idx}/{total}"
        )


if __name__ == "__main__":
    root = tk.Tk()
    app = WifViewer(root)

    if len(sys.argv) > 1:
        app.load(Path(sys.argv[1]))

    root.mainloop()
