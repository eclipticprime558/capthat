#!/usr/bin/env python3
"""
CaptThat v1.1.0 — screenshot tray app built for Claude Code on Windows.

Press a hotkey, drag a region (or capture full-screen / active window),
and the file path lands on your clipboard — paste it straight into Claude Code.

Dependencies:  pip install pillow keyboard pyperclip pystray plyer
"""

import glob
import os
import subprocess
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
from tkinter import colorchooser

VERSION = "1.2.0"
APP_NAME = "CaptThat"

_C = {
    "bg":     "#1C1C1E",
    "bg2":    "#2C2C2E",
    "bg3":    "#3A3A3C",
    "fg":     "#FFFFFF",
    "fg2":    "#EBEBF5",
    "fg3":    "#8E8E93",
    "accent": "#0A84FF",
    "sep":    "#48484A",
}
_FF = "Segoe UI"
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
    win.configure(bg=_C["bg2"])
    sw = tk_root.winfo_screenwidth()
    win.geometry(f"300x44+{sw // 2 - 150}+20")

    lbl = tk.Label(win, bg=_C["bg2"], fg=_C["fg"], font=(_FF, 13))
    lbl.pack(expand=True)

    n = [seconds]

    def tick():
        lbl.config(text=f"Capturing in {n[0]}s  ·  Esc to cancel")
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
    win.configure(bg=_C["bg2"])

    pw, ph = photo.width(), photo.height()
    bar_h = 36
    sw = tk_root.winfo_screenwidth()
    sh = tk_root.winfo_screenheight()
    wx = sw - pw - 20
    wy = sh - ph - bar_h - 56

    win.geometry(f"{pw}x{ph + bar_h}+{wx}+{wy}")

    canvas = tk.Canvas(win, width=pw, height=ph, highlightthickness=0, bg="#000000")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas._keep = photo

    bar = tk.Frame(win, bg=_C["bg2"], height=bar_h)
    bar.pack(fill=tk.X)
    tk.Label(bar, text="Path copied  ·  Click to open",
             bg=_C["bg2"], fg=_C["fg3"], font=(_FF, 10)).pack(
        side=tk.LEFT, padx=12, pady=8)

    def open_it(e=None):
        os.startfile(filepath)
        win.destroy()

    def show_rc_menu(e):
        m = tk.Menu(win, tearoff=0, bg=_C["bg2"], fg=_C["fg"],
                    activebackground=_C["accent"], activeforeground="white",
                    relief="flat", bd=0, font=(_FF, 11))
        m.add_command(label="Open file", command=open_it)
        m.add_separator()
        m.add_command(label="Settings…", command=lambda: (win.destroy(), open_settings()))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    for w in (win, canvas, bar):
        w.bind("<Button-1>", open_it)
        w.bind("<Button-3>", show_rc_menu)

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

    canvas.create_rectangle(0, 0, sw, 46, fill=_C["bg"], outline="")
    canvas.create_text(
        sw // 2, 23,
        text="Drag to select a region  ·  Esc to cancel  ·  Path copies to clipboard",
        fill=_C["fg3"], font=(_FF, 11),
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

    def on_destroy(e=None):
        global _overlay_open
        if e and e.widget is win:
            _overlay_open = False

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<Motion>", on_motion)
    canvas.bind("<ButtonRelease-1>", on_release)
    win.bind("<Escape>", on_cancel)
    win.bind("<Destroy>", on_destroy)
    win.focus_force()


# ── settings window ───────────────────────────────────────────────────────────

def _dark_title_bar(win):
    try:
        win.update()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
    except Exception:
        pass


class _Toggle(tk.Canvas):
    W, H = 44, 26

    def __init__(self, parent, variable, row=None, col=None, bg_color=None, **kw):
        bg_color = bg_color or _C["bg2"]
        super().__init__(parent, width=self.W, height=self.H,
                         bg=bg_color, highlightthickness=0, cursor="hand2", **kw)
        self._var = variable
        variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Button-1>", lambda _: self._var.set(not self._var.get()))
        if row is not None:
            self.grid(row=row, column=col if col is not None else 1,
                      padx=12, pady=8, sticky="e")
        self._draw()

    def _draw(self):
        self.delete("all")
        on = bool(self._var.get())
        track = _C["accent"] if on else _C["bg3"]
        r = self.H // 2
        self.create_arc(0, 0, self.H, self.H, start=90, extent=180,
                        fill=track, outline="")
        self.create_arc(self.W - self.H, 0, self.W, self.H, start=270, extent=180,
                        fill=track, outline="")
        self.create_rectangle(r, 0, self.W - r, self.H, fill=track, outline="")
        p = 3
        x = self.W - self.H + p if on else p
        ks = self.H - p * 2
        self.create_oval(x, p, x + ks, p + ks, fill="white", outline="")


def open_settings(icon=None, item=None):
    main_queue.put(_show_settings)


def _show_settings():
    win = tk.Toplevel(tk_root)
    win.title("CaptThat  ·  Settings")
    win.geometry("520x490")
    win.resizable(False, False)
    win.configure(bg=_C["bg"])
    win.attributes("-topmost", True)
    win.focus_force()
    _dark_title_bar(win)

    TABS = ["Hotkeys", "Output", "Capture", "After Capture", "System"]

    # Tab bar
    tab_bar = tk.Frame(win, bg=_C["bg2"], height=48)
    tab_bar.pack(fill=tk.X)
    tab_bar.pack_propagate(False)

    # Content area
    content = tk.Frame(win, bg=_C["bg"])
    content.pack(fill=tk.BOTH, expand=True, padx=24, pady=16)

    # Button bar
    btn_bar = tk.Frame(win, bg=_C["bg"], height=60)
    btn_bar.pack(fill=tk.X, padx=24)
    btn_bar.pack_propagate(False)

    panels: dict = {}
    tab_btns: dict = {}

    def switch(name):
        for t, b in tab_btns.items():
            b.configure(bg=_C["accent"] if t == name else _C["bg2"],
                        fg="white" if t == name else _C["fg3"])
        for t, p in panels.items():
            if t == name:
                p.pack(fill=tk.BOTH, expand=True)
            else:
                p.pack_forget()

    for name in TABS:
        b = tk.Label(tab_bar, text=name, font=(_FF, 11),
                     bg=_C["bg2"], fg=_C["fg3"],
                     padx=14, cursor="hand2")
        b.pack(side=tk.LEFT, fill=tk.Y)
        b.bind("<Button-1>", lambda e, n=name: switch(n))
        tab_btns[name] = b

    # ── Shared helpers ────────────────────────────────────────────────────────
    def section_lbl(parent, row, text, span=2):
        tk.Label(parent, text=text, font=(_FF, 11), bg=_C["bg"],
                 fg=_C["fg3"], anchor="w").grid(
            row=row, column=0, columnspan=span, sticky="w", pady=(12, 3))

    def field_entry(parent, row, var, span=2):
        e = tk.Entry(parent, textvariable=var, font=(_FF, 12),
                     bg=_C["bg2"], fg=_C["fg"], insertbackground=_C["fg"],
                     relief="flat", highlightthickness=1,
                     highlightbackground=_C["sep"], highlightcolor=_C["accent"])
        e.grid(row=row, column=0, columnspan=span, sticky="ew", pady=(0, 2), ipady=7)
        return e

    def hint_lbl(parent, row, text):
        tk.Label(parent, text=text, font=(_FF, 10), bg=_C["bg"],
                 fg=_C["fg3"], anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))

    def toggle_row(parent, row, label, var):
        f = tk.Frame(parent, bg=_C["bg2"])
        f.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
        f.columnconfigure(0, weight=1)
        tk.Label(f, text=label, font=(_FF, 12), bg=_C["bg2"],
                 fg=_C["fg"], anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=8)
        t = _Toggle(f, var, bg_color=_C["bg2"])
        t.grid(row=0, column=1, padx=12, pady=8)
        f.bind("<Button-1>", lambda _: var.set(not var.get()))

    def spinbox_widget(parent, row, var, from_, to, col=1, inc=1.0):
        s = tk.Spinbox(parent, from_=from_, to=to, increment=inc,
                       textvariable=var, width=7,
                       font=(_FF, 12), bg=_C["bg2"], fg=_C["fg"],
                       buttonbackground=_C["bg3"], relief="flat",
                       highlightthickness=1, highlightbackground=_C["sep"],
                       highlightcolor=_C["accent"], insertbackground=_C["fg"])
        s.grid(row=row, column=col, sticky="w", padx=(10, 0), pady=4)
        return s

    def slider_row(parent, row, label, var, from_, to):
        tk.Label(parent, text=label, font=(_FF, 11), bg=_C["bg"],
                 fg=_C["fg3"], anchor="w").grid(row=row, column=0, sticky="w", pady=(10, 3))
        f = tk.Frame(parent, bg=_C["bg"])
        f.grid(row=row, column=1, sticky="w", padx=(10, 0), pady=(10, 3))
        tk.Scale(f, variable=var, from_=from_, to=to, orient=tk.HORIZONTAL,
                 length=150, bg=_C["bg"], fg=_C["fg"], troughcolor=_C["bg3"],
                 activebackground=_C["accent"], highlightthickness=0,
                 sliderrelief="flat", bd=0, sliderlength=18).pack(side=tk.LEFT)

    def option_menu(parent, row, var, values, col=1):
        om = tk.OptionMenu(parent, var, *values)
        om.configure(bg=_C["bg2"], fg=_C["fg"], relief="flat", bd=0,
                     font=(_FF, 12), highlightthickness=0,
                     activebackground=_C["accent"], activeforeground="white")
        om["menu"].configure(bg=_C["bg2"], fg=_C["fg"],
                             activebackground=_C["accent"], activeforeground="white", bd=0)
        om.grid(row=row, column=col, sticky="w", padx=(10, 0), pady=4)
        return om

    # ── Hotkeys panel ─────────────────────────────────────────────────────────
    p1 = tk.Frame(content, bg=_C["bg"])
    panels["Hotkeys"] = p1
    p1.columnconfigure(0, weight=1)

    hk_region = tk.StringVar(value=config.get("hotkey_region", ""))
    hk_full   = tk.StringVar(value=config.get("hotkey_fullscreen", ""))
    hk_window = tk.StringVar(value=config.get("hotkey_window", ""))
    hk_repeat = tk.StringVar(value=config.get("hotkey_repeat", ""))

    for i, (label, var) in enumerate([
        ("Region capture", hk_region),
        ("Full screen", hk_full),
        ("Active window", hk_window),
        ("Repeat last region", hk_repeat),
    ]):
        section_lbl(p1, i * 2, label)
        field_entry(p1, i * 2 + 1, var)
    hint_lbl(p1, 8, "Examples: print screen  ·  ctrl+f9  ·  alt+shift+s")

    # ── Output panel ──────────────────────────────────────────────────────────
    p2 = tk.Frame(content, bg=_C["bg"])
    panels["Output"] = p2
    p2.columnconfigure(0, weight=1)
    p2.columnconfigure(1, weight=0)

    out_dir_var = tk.StringVar(value=config.get("output_dir", ""))
    fmt_var     = tk.StringVar(value=config.get("format", "png"))
    quality_var = tk.IntVar(value=int(config.get("jpeg_quality", 92)))
    unique_var  = tk.BooleanVar(value=config.get("unique_names", False))
    pattern_var = tk.StringVar(value=config.get("name_pattern", "{date}_{time}"))
    history_var = tk.StringVar(value=str(config.get("history_count", 10)))

    section_lbl(p2, 0, "Output folder")
    field_entry(p2, 1, out_dir_var)
    section_lbl(p2, 2, "Format", span=1)
    option_menu(p2, 2, fmt_var, ["png", "jpeg", "webp"])
    slider_row(p2, 3, "JPEG/WebP quality", quality_var, 1, 100)
    toggle_row(p2, 4, "Unique filenames", unique_var)
    section_lbl(p2, 5, "Name pattern")
    field_entry(p2, 6, pattern_var)
    hint_lbl(p2, 7, "Tokens: {date}  {time}  {ms}  {counter}  {title}")
    section_lbl(p2, 8, "History size", span=1)
    spinbox_widget(p2, 8, history_var, 1, 50)

    # ── Capture panel ─────────────────────────────────────────────────────────
    p3 = tk.Frame(content, bg=_C["bg"])
    panels["Capture"] = p3
    p3.columnconfigure(0, weight=1)
    p3.columnconfigure(1, weight=0)

    delay_var   = tk.StringVar(value=str(config.get("capture_delay", 0)))
    cursor_var  = tk.BooleanVar(value=config.get("include_cursor", False))
    magnif_var  = tk.BooleanVar(value=config.get("show_magnifier", True))
    opacity_var = tk.DoubleVar(value=float(config.get("overlay_opacity", 0.45)))
    cc_var      = tk.StringVar(value=config.get("crosshair_color", "#38bdf8"))

    section_lbl(p3, 0, "Capture delay (sec)", span=1)
    spinbox_widget(p3, 0, delay_var, 0, 10)
    toggle_row(p3, 1, "Include cursor in capture", cursor_var)
    toggle_row(p3, 2, "Show magnifier overlay", magnif_var)
    slider_row(p3, 3, "Overlay opacity", opacity_var, 0.1, 0.9)
    section_lbl(p3, 4, "Crosshair color", span=1)

    cc_row = tk.Frame(p3, bg=_C["bg"])
    cc_row.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=(10, 3))
    cc_swatch = tk.Label(cc_row, bg=cc_var.get(), width=3, relief="flat")
    cc_swatch.pack(side=tk.LEFT, padx=(0, 8))

    def pick_color():
        result = colorchooser.askcolor(color=cc_var.get(), parent=win, title="Crosshair Color")
        if result and result[1]:
            cc_var.set(result[1])
            cc_swatch.configure(bg=result[1])

    tk.Button(cc_row, text="Choose…", command=pick_color,
              bg=_C["bg3"], fg=_C["fg"], relief="flat", bd=0,
              font=(_FF, 11), padx=10, pady=4, cursor="hand2",
              activebackground=_C["accent"], activeforeground="white").pack(side=tk.LEFT)

    # ── After Capture panel ───────────────────────────────────────────────────
    p4 = tk.Frame(content, bg=_C["bg"])
    panels["After Capture"] = p4
    p4.columnconfigure(0, weight=1)
    p4.columnconfigure(1, weight=0)

    copy_img_var  = tk.BooleanVar(value=config.get("copy_image_to_clipboard", False))
    preview_var   = tk.BooleanVar(value=config.get("show_preview", True))
    prev_dur_var  = tk.StringVar(value=str(config.get("preview_duration", 2.5)))
    auto_open_var = tk.BooleanVar(value=config.get("auto_open", False))

    toggle_row(p4, 0, "Also copy image to clipboard", copy_img_var)
    hint_lbl(p4, 1, "Lets you paste the image directly into Slack, Word, etc.")
    toggle_row(p4, 2, "Show preview thumbnail", preview_var)
    section_lbl(p4, 3, "Preview duration (sec)", span=1)
    spinbox_widget(p4, 3, prev_dur_var, 0.5, 10, inc=0.5)
    toggle_row(p4, 4, "Auto-open after capture", auto_open_var)

    # ── System panel ──────────────────────────────────────────────────────────
    p5 = tk.Frame(content, bg=_C["bg"])
    panels["System"] = p5
    p5.columnconfigure(0, weight=1)

    startup_var = tk.BooleanVar(value=_startup_enabled())
    toggle_row(p5, 0, "Start with Windows", startup_var)
    tk.Label(p5, text=f"Version {VERSION}", font=(_FF, 11),
             bg=_C["bg"], fg=_C["fg3"]).grid(row=1, column=0, sticky="w", pady=(28, 0))
    tk.Label(p5, text="github.com/eclipticprime558/capthat", font=(_FF, 10),
             bg=_C["bg"], fg=_C["fg3"]).grid(row=2, column=0, sticky="w", pady=(2, 0))

    # ── Save / Cancel ─────────────────────────────────────────────────────────
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

    tk.Button(btn_bar, text="Cancel", command=win.destroy,
              bg=_C["bg3"], fg=_C["fg"], relief="flat", bd=0,
              font=(_FF, 12), padx=20, pady=8, cursor="hand2",
              activebackground=_C["sep"], activeforeground="white").pack(side=tk.LEFT, pady=10)
    tk.Button(btn_bar, text="Save", command=save,
              bg=_C["accent"], fg="white", relief="flat", bd=0,
              font=(_FF, 12, "bold"), padx=24, pady=8, cursor="hand2",
              activebackground="#0070E0", activeforeground="white").pack(side=tk.RIGHT, pady=10)

    switch("Hotkeys")


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
    scale = 8  # 8× supersampling for crisp antialiasing at every output size
    s = size * scale

    # Background gradient #1085FF → #0055CC (vivid top, deep bottom)
    bg = Image.new("RGB", (s, s))
    dg = ImageDraw.Draw(bg)
    for y in range(s):
        t = y / max(s - 1, 1)
        dg.line([(0, y), (s - 1, y)], fill=(
            int(0x10 + (0x00 - 0x10) * t),
            int(0x85 + (0x55 - 0x85) * t),
            int(0xFF + (0xCC - 0xFF) * t),
        ))

    # Squircle mask
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=s * 22 // 100, fill=255)

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(bg, mask=mask)
    d = ImageDraw.Draw(img)

    # Camera body — bold white rounded rect
    bpad = s * 13 // 100
    btop = s * 32 // 100
    bbot = s * 76 // 100
    d.rounded_rectangle([bpad, btop, s - bpad, bbot],
                        radius=s * 10 // 100, fill="white")

    # Viewfinder bump — wider notch so it reads at 16px
    vw = s * 26 // 100
    d.rounded_rectangle(
        [s // 2 - vw // 2, btop - s * 10 // 100,
         s // 2 + vw // 2, btop + s * 5 // 100],
        radius=s * 6 // 100, fill="white")

    # Lens — 3 wide rings so they stay distinct after downsampling
    #  ring widths at 48 px: outer≈5 px, white≈4 px, center≈3 px
    cx = s // 2
    cy = (btop + bbot) // 2 + s * 2 // 100
    for r, color in [
        (s * 22 // 100, "#5AC8FA"),  # outer light-blue  (ring = 22-13 = 9%)
        (s * 13 // 100, "white"),    # white gap          (ring = 13- 7 = 6%)
        (s *  7 // 100, "#0A84FF"),  # blue center
    ]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    return img.resize((size, size), Image.LANCZOS)


def save_icon_file(path: str):
    import struct
    # Pillow 12.x only saves a single frame via its ICO plugin, so we write the
    # ICO file manually: modern ICO stores each size as a raw PNG blob.
    sizes = [16, 24, 32, 48, 64, 96, 128, 256]
    pngs = []
    for sz in sizes:
        buf = BytesIO()
        _make_icon(sz).save(buf, format="PNG")
        pngs.append(buf.getvalue())

    n = len(sizes)
    dir_offset = 6 + n * 16          # byte offset where image data starts
    offsets = []
    cur = dir_offset
    for data in pngs:
        offsets.append(cur)
        cur += len(data)

    out = BytesIO()
    out.write(struct.pack("<HHH", 0, 1, n))   # ICO header
    for sz, data, off in zip(sizes, pngs, offsets):
        w = h = sz if sz < 256 else 0         # 0 encodes 256 in ICO spec
        out.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), off))
    for data in pngs:
        out.write(data)

    with open(path, "wb") as f:
        f.write(out.getvalue())

    # Tell Windows Explorer to flush its icon cache for this file
    try:
        SHCNE_UPDATEITEM = 0x00002000
        SHCNF_PATHW      = 0x0005
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_UPDATEITEM, SHCNF_PATHW,
            ctypes.c_wchar_p(os.path.abspath(path)), None)
    except Exception:
        pass


def _sync_desktop_shortcuts(ico_path: str):
    """
    Ensure the desktop shows the freshly generated icon.

    * Existing .lnk shortcuts → update IconLocation.
    * CaptThat.exe sitting directly on the desktop (no shortcut yet) → create a
      .lnk beside it so the icon can be overridden without patching the EXE.
    """
    ico_abs = os.path.abspath(ico_path)
    desktops = [
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
    ]

    shortcuts: list = []
    for d in desktops:
        shortcuts += glob.glob(os.path.join(d, "*[Cc]apt[Tt]hat*.lnk"))

    # If the EXE sits bare on the desktop with no shortcut, create one
    for d in desktops:
        exe_on_desktop = os.path.join(d, "CaptThat.exe")
        lnk_on_desktop = os.path.join(d, "CaptThat.lnk")
        if os.path.exists(exe_on_desktop) and lnk_on_desktop not in shortcuts:
            env = os.environ.copy()
            env["CT_LNK"] = lnk_on_desktop
            env["CT_TGT"] = exe_on_desktop
            env["CT_ICO"] = ico_abs
            ps = (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CT_LNK);"
                "$s.TargetPath=$env:CT_TGT;"
                "$s.IconLocation=$env:CT_ICO+',0';"
                "$s.Save()"
            )
            try:
                subprocess.run(
                    ["powershell", "-NonInteractive", "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=8, env=env,
                )
                shortcuts.append(lnk_on_desktop)
            except Exception:
                pass

    if not shortcuts:
        return

    # Update IconLocation on all shortcuts
    env = os.environ.copy()
    env["CT_ICO"] = ico_abs
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CT_LNK);"
        "$s.IconLocation=$env:CT_ICO+',0';"
        "$s.Save()"
    )
    for lnk in shortcuts:
        env["CT_LNK"] = lnk
        try:
            subprocess.run(
                ["powershell", "-NonInteractive", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=8, env=env,
            )
        except Exception:
            pass

    # Notify Explorer to repaint immediately
    SHCNE_UPDATEITEM = 0x00002000
    SHCNF_PATHW = 0x0005
    for lnk in shortcuts:
        try:
            ctypes.windll.shell32.SHChangeNotify(
                SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(lnk), None)
        except Exception:
            pass


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
    # Build the menu without a callable submenu — pystray's Windows backend has a
    # bug where pystray.Menu(callable) as a submenu breaks right-click on the tray
    # icon entirely.  Recent-captures items are inlined and the menu is rebuilt on
    # tray_icon.update_menu() (called after each capture) instead.
    def _build_menu():
        items = [
            pystray.MenuItem("Capture Region",        capture_now, default=True),
            pystray.MenuItem("Capture Full Screen",   lambda i, it: _trigger_fullscreen()),
            pystray.MenuItem("Capture Active Window", lambda i, it: _trigger_window()),
            pystray.MenuItem("Repeat Last Capture",   lambda i, it: _trigger_repeat()),
            pystray.Menu.SEPARATOR,
        ]
        items += list(_history_items())
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Screenshots Folder", open_screenshots_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings",              open_settings),
            pystray.MenuItem(_startup_label,          _toggle_startup),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit",                  lambda icon, item: os._exit(0)),
        ]
        return pystray.Menu(*items)

    # 256px source → pystray/Windows scales to tray size; crisp at all DPI settings
    tray_icon = pystray.Icon(APP_NAME, _make_icon(256), APP_NAME, _build_menu())
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

    # Regenerate the .ico in AppData (never next to the EXE, which may be on the desktop)
    try:
        _ico_dir = os.path.join(os.environ.get("APPDATA", _BASE), "CaptThat")
        os.makedirs(_ico_dir, exist_ok=True)
        _ico_path = os.path.join(_ico_dir, "CaptThat.ico")
        save_icon_file(_ico_path)
        _sync_desktop_shortcuts(_ico_path)
    except Exception:
        pass

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
