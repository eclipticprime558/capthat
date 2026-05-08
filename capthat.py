#!/usr/bin/env python3
"""
CaptThat v1.1.0 — screenshot tray app built for Claude Code on Windows.

Press a hotkey, drag a region (or capture full-screen / active window),
and the file path lands on your clipboard — paste it straight into Claude Code.

Dependencies:  pip install pillow keyboard pyperclip pystray plyer
"""

import os
import sys
import json
import re
import time
import queue
import socket
import threading
import winreg
import ctypes
import ctypes.wintypes
from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw, ImageEnhance, ImageGrab, ImageTk
import keyboard
import pyperclip
import pystray
import tkinter as tk
from tkinter import ttk, colorchooser

VERSION = "1.1.0"
APP_NAME = "CaptThat"
_BASE = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(_BASE, "config.json")
REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG = {
    # Hotkeys
    "hotkey_region":     "print screen",
    "hotkey_fullscreen": "ctrl+print screen",
    "hotkey_window":     "alt+print screen",
    "hotkey_repeat":     "shift+print screen",
    # Output
    "output_dir":        r"C:\Screenshots",
    "unique_names":      False,
    "name_pattern":      "{date}_{time}",
    "format":            "png",      # png | jpeg | webp
    "jpeg_quality":      92,
    "history_count":     10,
    # Capture
    "capture_delay":     0,          # seconds before capture (0 = instant)
    "include_cursor":    False,
    "show_magnifier":    True,
    # After capture
    "copy_image_to_clipboard": False,
    "show_preview":      True,
    "preview_duration":  2.5,
    "auto_open":         False,
    # Overlay
    "overlay_opacity":   0.45,
    "crosshair_color":   "#38bdf8",
    # System
    "start_with_windows": False,
}

# ── runtime state ─────────────────────────────────────────────────────────────

config: dict = {}
tray_icon: pystray.Icon | None = None
_hotkey_ids: list = []
main_queue: queue.Queue = queue.Queue()
tk_root: tk.Tk | None = None
_overlay_open = False
_lock_sock: socket.socket | None = None
_last_region: tuple | None = None
_history: list[str] = []
_capture_counter = 0


# ── single instance ───────────────────────────────────────────────────────────

def _ensure_single_instance() -> bool:
    global _lock_sock
    _lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_sock.bind(("127.0.0.1", 47291))
        return True
    except OSError:
        return False


# ── config ────────────────────────────────────────────────────────────────────

def load_config():
    global config
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                config = {**DEFAULT_CONFIG, **json.load(f)}
            return
        except Exception:
            pass
    config = DEFAULT_CONFIG.copy()


def save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"[CaptThat] config save failed: {e}")


# ── startup registry ──────────────────────────────────────────────────────────

def _startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


def set_startup(enabled: bool):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            exe = os.path.join(_BASE, "CaptThat.exe")
            target = exe if os.path.exists(exe) else f'pythonw "{os.path.join(_BASE, "capthat.py")}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, target)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        config["start_with_windows"] = enabled
        save_config()
    except Exception as e:
        print(f"[CaptThat] startup registry error: {e}")


# ── hotkeys ───────────────────────────────────────────────────────────────────

def register_hotkeys():
    global _hotkey_ids
    for hid in _hotkey_ids:
        try:
            keyboard.remove_hotkey(hid)
        except Exception:
            pass
    _hotkey_ids = []

    bindings = [
        ("hotkey_region",     lambda: _trigger_region()),
        ("hotkey_fullscreen", lambda: _trigger_fullscreen()),
        ("hotkey_window",     lambda: _trigger_window()),
        ("hotkey_repeat",     lambda: _trigger_repeat()),
    ]
    for key, fn in bindings:
        hk = config.get(key, "").strip()
        if not hk:
            continue
        try:
            hid = keyboard.add_hotkey(hk, fn, suppress=True)
            _hotkey_ids.append(hid)
        except Exception as e:
            print(f"[CaptThat] hotkey '{hk}' failed: {e}")


# ── capture triggers (called from hotkey thread) ──────────────────────────────

def _with_delay(action):
    delay = int(config.get("capture_delay", 0))
    if delay > 0:
        main_queue.put(lambda: _countdown_then(delay, action))
    else:
        action()


def _trigger_region():
    _with_delay(lambda: main_queue.put(show_overlay))


def _trigger_fullscreen():
    def go():
        threading.Thread(target=_capture_and_save, kwargs={"mode": "fullscreen"}, daemon=True).start()
    _with_delay(lambda: main_queue.put(go) if threading.current_thread() is not threading.main_thread() else go())


def _trigger_window():
    def go():
        threading.Thread(target=_capture_and_save, kwargs={"mode": "window"}, daemon=True).start()
    _with_delay(lambda: main_queue.put(go) if threading.current_thread() is not threading.main_thread() else go())


def _trigger_repeat():
    if not _last_region:
        return
    r = _last_region
    def go():
        threading.Thread(target=_capture_and_save, args=r, daemon=True).start()
    _with_delay(lambda: main_queue.put(go) if threading.current_thread() is not threading.main_thread() else go())


# ── countdown overlay ─────────────────────────────────────────────────────────

def _countdown_then(seconds: int, callback):
    win = tk.Toplevel(tk_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="#0f172a")
    sw = tk_root.winfo_screenwidth()
    win.geometry(f"240x52+{sw // 2 - 120}+16")

    lbl = tk.Label(win, bg="#0f172a", fg="#f8fafc", font=("Segoe UI", 15, "bold"))
    lbl.pack(expand=True)

    n = [seconds]

    def tick():
        lbl.config(text=f"  Capturing in {n[0]}s…  (Esc to cancel)")
        if n[0] <= 0:
            win.destroy()
            callback()
        else:
            n[0] -= 1
            win.after(1000, tick)

    win.bind("<Escape>", lambda e: win.destroy())
    tick()


# ── windows helpers (ctypes only, no pywin32) ─────────────────────────────────

def _get_active_window_rect() -> tuple[int, int, int, int]:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _get_active_window_title() -> str:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _draw_cursor_on_image(img: Image.Image, cx: int, cy: int):
    if not (0 <= cx < img.width and 0 <= cy < img.height):
        return
    d = ImageDraw.Draw(img)
    pts = [
        (cx, cy), (cx, cy + 14), (cx + 4, cy + 10),
        (cx + 7, cy + 16), (cx + 9, cy + 15),
        (cx + 6, cy + 9), (cx + 11, cy + 9),
    ]
    d.polygon(pts, fill="white", outline="black")


def _copy_image_to_clipboard(img: Image.Image):
    buf = BytesIO()
    img.convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]  # strip 14-byte BMP file header → CF_DIB format
    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    hMem = k32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    ptr = k32.GlobalLock(hMem)
    ctypes.memmove(ptr, dib, len(dib))
    k32.GlobalUnlock(hMem)
    u32.OpenClipboard(0)
    u32.EmptyClipboard()
    u32.SetClipboardData(CF_DIB, hMem)
    u32.CloseClipboard()


# ── output path + naming ──────────────────────────────────────────────────────

def _resolve_pattern(pattern: str) -> str:
    now = datetime.now()
    title = _get_active_window_title()
    safe = re.sub(r'[<>:"/\\|?*\s]+', "_", title)[:40].strip("_")
    return (
        pattern
        .replace("{date}",    now.strftime("%Y%m%d"))
        .replace("{time}",    now.strftime("%H%M%S"))
        .replace("{ms}",      now.strftime("%f")[:3])
        .replace("{counter}", f"{_capture_counter:03d}")
        .replace("{title}",   safe or "untitled")
    )


def _output_path() -> str:
    out_dir = config["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    fmt = config.get("format", "png")
    ext = "jpg" if fmt == "jpeg" else fmt
    if config.get("unique_names"):
        name = _resolve_pattern(config.get("name_pattern", "{date}_{time}")) + f".{ext}"
    else:
        name = f"latest.{ext}"
    return os.path.join(out_dir, name)


def _update_history(filepath: str):
    global _history
    if filepath in _history:
        _history.remove(filepath)
    _history.insert(0, filepath)
    _history = _history[: max(1, int(config.get("history_count", 10)))]


# ── core capture ──────────────────────────────────────────────────────────────

def _capture_and_save(x1=None, y1=None, x2=None, y2=None, mode="region"):
    global _last_region, _capture_counter

    time.sleep(0.06)  # let overlay fully close before grabbing

    if mode == "fullscreen":
        img = ImageGrab.grab()
        bbox = (0, 0, img.width, img.height)
    elif mode == "window":
        raw = _get_active_window_rect()
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        bbox = (max(0, raw[0]), max(0, raw[1]), min(sw, raw[2]), min(sh, raw[3]))
        img = ImageGrab.grab(bbox=bbox)
    else:
        bbox = (x1, y1, x2, y2)
        img = ImageGrab.grab(bbox=bbox)

    if config.get("include_cursor"):
        cx, cy = _get_cursor_pos()
        _draw_cursor_on_image(img, cx - bbox[0], cy - bbox[1])

    _last_region = bbox
    _capture_counter += 1

    filepath = _output_path()
    fmt = config.get("format", "png")
    quality = int(config.get("jpeg_quality", 92))
    if fmt == "jpeg":
        img.convert("RGB").save(filepath, quality=quality, optimize=True)
    elif fmt == "webp":
        img.save(filepath, quality=quality)
    else:
        img.save(filepath)

    _update_history(filepath)
    pyperclip.copy(filepath)

    if config.get("copy_image_to_clipboard"):
        try:
            _copy_image_to_clipboard(img)
        except Exception:
            pass

    if config.get("show_preview", True):
        img_copy = img.copy()
        main_queue.put(lambda i=img_copy, f=filepath: _show_preview(i, f))

    if config.get("auto_open"):
        os.startfile(filepath)

    _notify("Screenshot saved — path copied")

    if tray_icon:
        try:
            tray_icon.update_menu()
        except Exception:
            pass


# ── preview thumbnail ─────────────────────────────────────────────────────────

def _show_preview(img: Image.Image, filepath: str):
    THUMB = 280
    img.thumbnail((THUMB, THUMB))
    photo = ImageTk.PhotoImage(img)

    win = tk.Toplevel(tk_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)

    pw, ph = photo.width(), photo.height()
    bar_h = 28
    sw = tk_root.winfo_screenwidth()
    sh = tk_root.winfo_screenheight()
    wx = sw - pw - 16
    wy = sh - ph - bar_h - 48  # above taskbar

    win.geometry(f"{pw}x{ph + bar_h}+{wx}+{wy}")
    win.configure(bg="#0f172a")

    canvas = tk.Canvas(win, width=pw, height=ph, highlightthickness=0, bg="#0f172a")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas._keep = photo

    bar = tk.Frame(win, bg="#1e293b", height=bar_h)
    bar.pack(fill=tk.X)
    tk.Label(bar, text="Click to open  •  path on clipboard",
             bg="#1e293b", fg="#94a3b8", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)

    def open_it(e=None):
        os.startfile(filepath)
        win.destroy()

    win.bind("<Button-1>", open_it)
    canvas.bind("<Button-1>", open_it)

    dur = int(float(config.get("preview_duration", 2.5)) * 1000)
    win.after(dur, lambda: win.destroy() if win.winfo_exists() else None)


# ── selection overlay ─────────────────────────────────────────────────────────

def show_overlay():
    global _overlay_open
    if _overlay_open:
        return
    _overlay_open = True

    bg = ImageGrab.grab()
    opacity = float(config.get("overlay_opacity", 0.45))
    dark_bg = ImageEnhance.Brightness(bg).enhance(1 - opacity)

    win = tk.Toplevel(tk_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)

    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{sw}x{sh}+0+0")

    dark_photo = ImageTk.PhotoImage(dark_bg)
    canvas = tk.Canvas(win, cursor="crosshair", highlightthickness=0,
                       width=sw, height=sh, bd=0, bg="black")
    canvas.pack(fill=tk.BOTH, expand=True)
    canvas.create_image(0, 0, image=dark_photo, anchor="nw")
    canvas._keep = dark_photo

    # Hint bar
    canvas.create_rectangle(0, 0, sw, 46, fill="#0f172a", outline="")
    canvas.create_text(
        sw // 2, 23,
        text="Drag to select region  •  Esc to cancel  •  Path will be copied for Claude Code",
        fill="#64748b", font=("Segoe UI", 11),
    )

    cc = config.get("crosshair_color", "#38bdf8")
    state = {"sx": None, "sy": None, "rect": None, "dim": None}
    show_mag = config.get("show_magnifier", True)

    # Magnifier elements (hidden off-screen initially)
    MAG_SRC = 40
    MAG_OUT = 160
    mag_img_id = canvas.create_image(-300, -300, anchor="nw")
    mag_box_id = canvas.create_rectangle(-300, -300, -140, -140, outline=cc, width=2)

    def _update_mag(x, y):
        if not show_mag:
            return
        x1 = max(0, x - MAG_SRC // 2)
        y1 = max(0, y - MAG_SRC // 2)
        x2 = min(bg.width, x1 + MAG_SRC)
        y2 = min(bg.height, y1 + MAG_SRC)
        crop = bg.crop((x1, y1, x2, y2))
        zoomed = crop.resize((MAG_OUT, MAG_OUT), Image.NEAREST)

        d = ImageDraw.Draw(zoomed)
        cx2, cy2 = MAG_OUT // 2, MAG_OUT // 2
        d.line([(0, cy2), (MAG_OUT, cy2)], fill="#ef4444", width=1)
        d.line([(cx2, 0), (cx2, MAG_OUT)], fill="#ef4444", width=1)
        d.rectangle([0, 0, MAG_OUT - 1, MAG_OUT - 1], outline=cc, width=2)
        d.text((4, 4), f"{x},{y}", fill="#f8fafc")

        photo = ImageTk.PhotoImage(zoomed)
        canvas.itemconfig(mag_img_id, image=photo)
        canvas._mag = photo  # prevent GC

        mx = x + 24 if x + 24 + MAG_OUT < sw else x - 24 - MAG_OUT
        my = max(54, y - MAG_OUT - 12) if y - MAG_OUT - 12 > 46 else y + 16
        canvas.coords(mag_img_id, mx, my)
        canvas.coords(mag_box_id, mx - 1, my - 1, mx + MAG_OUT + 1, my + MAG_OUT + 1)
        canvas.tag_raise(mag_img_id)
        canvas.tag_raise(mag_box_id)

    def on_press(e):
        state["sx"], state["sy"] = e.x, e.y
        for item in (state["rect"], state["dim"]):
            if item:
                canvas.delete(item)
        state["rect"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline=cc, width=2, fill="")
        state["dim"] = canvas.create_text(
            e.x + 10, e.y + 14, text="0 × 0",
            fill="#f8fafc", font=("Segoe UI", 9, "bold"), anchor="nw")

    def on_drag(e):
        if state["sx"] is None:
            return
        canvas.coords(state["rect"], state["sx"], state["sy"], e.x, e.y)
        w, h = abs(e.x - state["sx"]), abs(e.y - state["sy"])
        canvas.itemconfig(state["dim"], text=f"{w} × {h}")
        canvas.coords(state["dim"],
                      min(e.x + 10, sw - 80),
                      min(e.y + 14, sh - 20))
        _update_mag(e.x, e.y)

    def on_motion(e):
        _update_mag(e.x, e.y)

    def on_release(e):
        global _overlay_open
        if state["sx"] is None:
            return
        x1, y1 = min(state["sx"], e.x), min(state["sy"], e.y)
        x2, y2 = max(state["sx"], e.x), max(state["sy"], e.y)
        _overlay_open = False
        win.destroy()
        if x2 - x1 > 5 and y2 - y1 > 5:
            threading.Thread(target=_capture_and_save, args=(x1, y1, x2, y2), daemon=True).start()

    def on_cancel(e):
        global _overlay_open
        _overlay_open = False
        win.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<Motion>", on_motion)
    canvas.bind("<ButtonRelease-1>", on_release)
    win.bind("<Escape>", on_cancel)
    win.focus_force()


# ── settings window ───────────────────────────────────────────────────────────

def open_settings(icon=None, item=None):
    main_queue.put(_show_settings)


def _show_settings():
    win = tk.Toplevel(tk_root)
    win.title(f"CaptThat {VERSION} — Settings")
    win.geometry("480x420")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.focus_force()

    nb = ttk.Notebook(win)
    nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def tab(label):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text=f"  {label}  ")
        return f

    def row(parent, r, label, widget_fn, col_span=1):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=5)
        w = widget_fn(parent)
        w.grid(row=r, column=1, columnspan=col_span, sticky="w", padx=10)
        return w

    def entry(parent, var, width=26):
        return ttk.Entry(parent, textvariable=var, width=width)

    def check(parent, var, text=""):
        return ttk.Checkbutton(parent, variable=var, text=text)

    # ── Tab 1: Hotkeys ──────────────────────────────────────────────────────
    t1 = tab("Hotkeys")
    hk_region   = tk.StringVar(value=config.get("hotkey_region", ""))
    hk_full     = tk.StringVar(value=config.get("hotkey_fullscreen", ""))
    hk_window   = tk.StringVar(value=config.get("hotkey_window", ""))
    hk_repeat   = tk.StringVar(value=config.get("hotkey_repeat", ""))

    row(t1, 0, "Region capture:",      lambda p: entry(p, hk_region))
    row(t1, 1, "Full screen:",         lambda p: entry(p, hk_full))
    row(t1, 2, "Active window:",       lambda p: entry(p, hk_window))
    row(t1, 3, "Repeat last region:",  lambda p: entry(p, hk_repeat))
    ttk.Label(t1, text="Key names: print screen · ctrl+f9 · alt+shift+s",
              foreground="#888").grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))

    # ── Tab 2: Output ───────────────────────────────────────────────────────
    t2 = tab("Output")
    out_dir_var  = tk.StringVar(value=config.get("output_dir", ""))
    fmt_var      = tk.StringVar(value=config.get("format", "png"))
    quality_var  = tk.IntVar(value=int(config.get("jpeg_quality", 92)))
    unique_var   = tk.BooleanVar(value=config.get("unique_names", False))
    pattern_var  = tk.StringVar(value=config.get("name_pattern", "{date}_{time}"))
    history_var  = tk.StringVar(value=str(config.get("history_count", 10)))

    row(t2, 0, "Output folder:", lambda p: entry(p, out_dir_var, 30))

    row(t2, 1, "Format:", lambda p: ttk.Combobox(
        p, textvariable=fmt_var, values=["png", "jpeg", "webp"], width=10, state="readonly"))

    qual_frame = ttk.Frame(t2)
    qual_frame.grid(row=2, column=1, sticky="w", padx=10)
    ttk.Label(t2, text="JPEG/WebP quality:").grid(row=2, column=0, sticky="w", pady=5)
    ttk.Scale(qual_frame, variable=quality_var, from_=1, to=100,
              orient=tk.HORIZONTAL, length=160).pack(side=tk.LEFT)
    ttk.Label(qual_frame, textvariable=quality_var, width=3).pack(side=tk.LEFT, padx=4)

    row(t2, 3, "Filename mode:",
        lambda p: ttk.Checkbutton(p, variable=unique_var, text="Unique names (pattern below)"))
    row(t2, 4, "Name pattern:", lambda p: entry(p, pattern_var, 24))
    ttk.Label(t2, text="Tokens: {date} {time} {ms} {counter} {title}",
              foreground="#888").grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
    row(t2, 6, "History size:", lambda p: ttk.Spinbox(p, from_=1, to=50, textvariable=history_var, width=6))

    # ── Tab 3: Capture ──────────────────────────────────────────────────────
    t3 = tab("Capture")
    delay_var   = tk.StringVar(value=str(config.get("capture_delay", 0)))
    cursor_var  = tk.BooleanVar(value=config.get("include_cursor", False))
    magnif_var  = tk.BooleanVar(value=config.get("show_magnifier", True))
    opacity_var = tk.DoubleVar(value=float(config.get("overlay_opacity", 0.45)))
    cc_var      = tk.StringVar(value=config.get("crosshair_color", "#38bdf8"))

    row(t3, 0, "Capture delay (sec):",
        lambda p: ttk.Spinbox(p, from_=0, to=10, textvariable=delay_var, width=6))
    row(t3, 1, "Include cursor:", lambda p: check(p, cursor_var))
    row(t3, 2, "Show magnifier:", lambda p: check(p, magnif_var))

    ttk.Label(t3, text="Overlay opacity:").grid(row=3, column=0, sticky="w", pady=5)
    op_frame = ttk.Frame(t3)
    op_frame.grid(row=3, column=1, sticky="w", padx=10)
    ttk.Scale(op_frame, variable=opacity_var, from_=0.1, to=0.9,
              orient=tk.HORIZONTAL, length=160).pack(side=tk.LEFT)

    ttk.Label(t3, text="Crosshair color:").grid(row=4, column=0, sticky="w", pady=5)
    cc_frame = ttk.Frame(t3)
    cc_frame.grid(row=4, column=1, sticky="w", padx=10)
    cc_swatch = tk.Label(cc_frame, bg=cc_var.get(), width=4, relief="solid")
    cc_swatch.pack(side=tk.LEFT, padx=(0, 6))

    def pick_color():
        result = colorchooser.askcolor(color=cc_var.get(), parent=win, title="Crosshair Color")
        if result and result[1]:
            cc_var.set(result[1])
            cc_swatch.configure(bg=result[1])

    ttk.Button(cc_frame, text="Choose…", command=pick_color).pack(side=tk.LEFT)

    # ── Tab 4: After Capture ────────────────────────────────────────────────
    t4 = tab("After Capture")
    copy_img_var  = tk.BooleanVar(value=config.get("copy_image_to_clipboard", False))
    preview_var   = tk.BooleanVar(value=config.get("show_preview", True))
    prev_dur_var  = tk.StringVar(value=str(config.get("preview_duration", 2.5)))
    auto_open_var = tk.BooleanVar(value=config.get("auto_open", False))

    row(t4, 0, "Also copy image to clipboard:",
        lambda p: check(p, copy_img_var))
    ttk.Label(t4, text="(so you can also paste the image, not just the path)",
              foreground="#888").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
    row(t4, 2, "Show preview thumbnail:", lambda p: check(p, preview_var))
    row(t4, 3, "Preview duration (sec):",
        lambda p: ttk.Spinbox(p, from_=0.5, to=10, increment=0.5,
                              textvariable=prev_dur_var, width=6))
    row(t4, 4, "Auto-open after capture:", lambda p: check(p, auto_open_var))

    # ── Tab 5: System ───────────────────────────────────────────────────────
    t5 = tab("System")
    startup_var = tk.BooleanVar(value=_startup_enabled())

    row(t5, 0, "Start with Windows:", lambda p: check(p, startup_var))
    ttk.Label(t5, text=f"Version {VERSION}  •  github.com/eclipticprime558/capthat",
              foreground="#888").grid(row=5, column=0, columnspan=2, sticky="w", pady=(30, 0))

    # ── Save / Cancel ───────────────────────────────────────────────────────
    def save():
        config["hotkey_region"]           = hk_region.get().strip()
        config["hotkey_fullscreen"]       = hk_full.get().strip()
        config["hotkey_window"]           = hk_window.get().strip()
        config["hotkey_repeat"]           = hk_repeat.get().strip()
        config["output_dir"]              = out_dir_var.get().strip()
        config["format"]                  = fmt_var.get()
        config["jpeg_quality"]            = quality_var.get()
        config["unique_names"]            = unique_var.get()
        config["name_pattern"]            = pattern_var.get().strip()
        config["history_count"]           = int(history_var.get())
        config["capture_delay"]           = int(delay_var.get())
        config["include_cursor"]          = cursor_var.get()
        config["show_magnifier"]          = magnif_var.get()
        config["overlay_opacity"]         = round(opacity_var.get(), 2)
        config["crosshair_color"]         = cc_var.get()
        config["copy_image_to_clipboard"] = copy_img_var.get()
        config["show_preview"]            = preview_var.get()
        config["preview_duration"]        = float(prev_dur_var.get())
        config["auto_open"]               = auto_open_var.get()
        set_startup(startup_var.get())
        save_config()
        register_hotkeys()
        win.destroy()

    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=(0, 10))
    ttk.Button(btn_frame, text="Save", command=save).pack(side=tk.LEFT, padx=6)
    ttk.Button(btn_frame, text="Cancel", command=win.destroy).pack(side=tk.LEFT, padx=6)


# ── notification ──────────────────────────────────────────────────────────────

def _notify(msg: str):
    try:
        import plyer
        plyer.notification.notify(title=APP_NAME, message=msg,
                                  app_name=APP_NAME, timeout=3)
    except Exception:
        pass


# ── tray icon image ───────────────────────────────────────────────────────────

def _make_icon(size: int = 64) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(2, s // 12)
    top = s * 28 // 100
    bot = s * 88 // 100
    r = max(2, s // 10)
    d.rounded_rectangle([pad, top, s - pad, bot], radius=r,
                        fill="#1e293b", outline="#475569", width=max(1, s // 22))
    vw = s * 26 // 100
    d.rounded_rectangle([s // 2 - vw // 2, s * 14 // 100,
                         s // 2 + vw // 2, s * 30 // 100],
                        radius=max(2, s // 18), fill="#1e293b",
                        outline="#475569", width=max(1, s // 22))
    # Flash (orange — Claude accent)
    fx, fy, fr = s * 72 // 100, s * 35 // 100, max(3, s // 9)
    d.ellipse([fx - fr, fy - fr, fx + fr, fy + fr], fill="#f97316")
    # Lens
    cx, cy = s // 2, s * 58 // 100
    lr = s * 24 // 100
    d.ellipse([cx - lr, cy - lr, cx + lr, cy + lr],
              fill="#0ea5e9", outline="#38bdf8", width=max(1, s // 22))
    lr2 = s * 14 // 100
    d.ellipse([cx - lr2, cy - lr2, cx + lr2, cy + lr2], fill="#0284c7")
    hr = max(2, s // 12)
    hx, hy = cx - s // 12, cy - s // 12
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill="#bae6fd")
    return img


def save_icon_file(path: str):
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [_make_icon(s) for s in sizes]
    imgs[0].save(path, format="ICO",
                 sizes=[(s, s) for s in sizes],
                 append_images=imgs[1:])


# ── tray menu ─────────────────────────────────────────────────────────────────

def _history_items():
    if not _history:
        return (pystray.MenuItem("No captures yet", None, enabled=False),)
    items = []
    for path in _history[:5]:
        name = os.path.basename(path)
        items.append(
            pystray.MenuItem(name, lambda icon, item, fp=path: pyperclip.copy(fp))
        )
    return tuple(items)


def open_screenshots_folder(icon=None, item=None):
    folder = config.get("output_dir", r"C:\Screenshots")
    os.makedirs(folder, exist_ok=True)
    os.startfile(folder)


def capture_now(icon=None, item=None):
    _trigger_region()


def _toggle_startup(icon=None, item=None):
    set_startup(not _startup_enabled())


def _startup_label(icon=None, item=None):
    return ("✓ " if _startup_enabled() else "    ") + "Start with Windows"


def _run_tray():
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("Capture Region",       capture_now, default=True),
        pystray.MenuItem("Capture Full Screen",  lambda i, it: _trigger_fullscreen()),
        pystray.MenuItem("Capture Active Window",lambda i, it: _trigger_window()),
        pystray.MenuItem("Repeat Last Capture",  lambda i, it: _trigger_repeat()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Recent Captures",      pystray.Menu(_history_items)),
        pystray.MenuItem("Open Screenshots Folder", open_screenshots_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings",             open_settings),
        pystray.MenuItem(_startup_label,         _toggle_startup),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit",                 lambda icon, item: os._exit(0)),
    )
    tray_icon = pystray.Icon(APP_NAME, _make_icon(64), APP_NAME, menu)
    tray_icon.run()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global tk_root

    if not _ensure_single_instance():
        ctypes.windll.user32.MessageBoxW(
            0,
            "CaptThat is already running.\nFind it in the system tray.",
            APP_NAME,
            0x40,
        )
        sys.exit(0)

    load_config()
    config["start_with_windows"] = _startup_enabled()

    tk_root = tk.Tk()
    tk_root.withdraw()

    register_hotkeys()
    threading.Thread(target=_run_tray, daemon=True).start()

    def process_queue():
        try:
            while True:
                task = main_queue.get_nowait()
                task()
        except queue.Empty:
            pass
        tk_root.after(50, process_queue)

    tk_root.after(50, process_queue)
    tk_root.mainloop()


if __name__ == "__main__":
    main()
