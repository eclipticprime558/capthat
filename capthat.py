#!/usr/bin/env python3
"""
CaptThat v1.1.0 — screenshot tray app built for Claude Code on Windows.

Press a hotkey, drag a region (or capture full-screen / active window),
and the file path lands on your clipboard — paste it straight into Claude Code.

Dependencies:  pip install pillow keyboard pyperclip pystray plyer
"""

import base64
import glob
import os
import subprocess
import sys
import json
import math
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

from PIL import (
    Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont,
    ImageGrab, ImageStat, ImageTk,
)
import keyboard
import pyperclip
import pystray
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox
import customtkinter as ctk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware
except Exception:
    pass
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

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
    "hotkey_region":     "ctrl+alt+s",
    "hotkey_fullscreen": "ctrl+alt+f",
    "hotkey_window":     "ctrl+alt+w",
    "hotkey_repeat":     "ctrl+alt+r",
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
    "show_magnifier":    False,
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
                loaded = json.load(f)
            if "hotkey" in loaded and "hotkey_region" not in loaded:
                loaded["hotkey_region"] = loaded["hotkey"]  # migrate pre-v1.1 single-hotkey config
            config = {**DEFAULT_CONFIG, **loaded}
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


# ── startup (Startup folder shortcut) ────────────────────────────────────────

_STARTUP_FOLDER = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
)


def _startup_lnk() -> str:
    return os.path.join(_STARTUP_FOLDER, f"{APP_NAME}.lnk")


def _ps_str(s: str) -> str:
    """Escape a value for use inside a PowerShell double-quoted string."""
    return s.replace("`", "``").replace('"', '`"').replace("$", "`$")


def _startup_enabled() -> bool:
    if os.path.exists(_startup_lnk()):
        return True
    # Backward-compat: old registry entry counts as enabled
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


def set_startup(enabled: bool):
    lnk = _startup_lnk()
    try:
        if enabled:
            exe = os.path.join(_BASE, "CaptThat.exe")
            ico = os.path.join(
                os.environ.get("APPDATA", _BASE), "CaptThat", "CaptThat.ico"
            )
            if os.path.exists(exe):
                target, arguments = exe, ""
            else:
                pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                if not os.path.exists(pythonw):
                    pythonw = sys.executable
                target = pythonw
                arguments = f'"{os.path.join(_BASE, "capthat.py")}"'

            icon_line = (f'$s.IconLocation = "{_ps_str(ico)}"'
                         if os.path.exists(ico) else "")
            ps = "\n".join([
                "$ws = New-Object -ComObject WScript.Shell",
                f'$s = $ws.CreateShortcut("{_ps_str(lnk)}")',
                f'$s.TargetPath = "{_ps_str(target)}"',
                f'$s.Arguments = "{_ps_str(arguments)}"',
                f'$s.WorkingDirectory = "{_ps_str(_BASE)}"',
                icon_line,
                "$s.Save()",
            ])
            encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
            subprocess.run(
                ["powershell", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-EncodedCommand", encoded],
                capture_output=True, timeout=15,
            )
        else:
            try:
                os.remove(lnk)
            except FileNotFoundError:
                pass

        # Remove old registry entry on either toggle direction
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, APP_NAME)
            winreg.CloseKey(k)
        except (FileNotFoundError, OSError):
            pass

        config["start_with_windows"] = enabled
        save_config()
    except Exception as e:
        print(f"[CaptThat] startup error: {e}")


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
            hid = keyboard.add_hotkey(hk, fn)
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
    win.attributes("-alpha", 0.0)
    win.configure(bg=_C["bg2"])
    sw = tk_root.winfo_screenwidth()
    win.geometry(f"300x46+{sw // 2 - 150}+20")

    lbl = tk.Label(win, bg=_C["bg2"], fg=_C["fg"], font=(_FF, 13, "bold"))
    lbl.pack(expand=True)

    n = [seconds]

    def _fade_in(i=0, steps=6):
        if not win.winfo_exists():
            return
        win.attributes("-alpha", min(1.0, i / steps))
        if i < steps:
            win.after(15, lambda: _fade_in(i + 1, steps))

    def tick():
        lbl.config(text=f"Capturing in {n[0]}s  ·  Esc to cancel")
        if n[0] <= 0:
            win.destroy()
            callback()
        else:
            n[0] -= 1
            win.after(1000, tick)

    win.bind("<Escape>", lambda e: win.destroy())
    _fade_in()
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


def _force_foreground(hwnd: int):
    """Steal OS keyboard focus for hwnd, bypassing Windows' foreground-lock
    restriction — needed because this window is opened from a background
    hotkey thread while some other app still holds foreground/focus."""
    u32 = ctypes.windll.user32
    ASFW_ANY = 0xFFFFFFFF
    u32.AllowSetForegroundWindow(ASFW_ANY)
    fg = u32.GetForegroundWindow()
    fg_thread = u32.GetWindowThreadProcessId(fg, None)
    cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    u32.AttachThreadInput(fg_thread, cur_thread, True)
    u32.ShowWindow(hwnd, 5)  # SW_SHOW
    u32.BringWindowToTop(hwnd)
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(hwnd)
    u32.AttachThreadInput(fg_thread, cur_thread, False)


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
    full_res = img.copy()
    THUMB = 280
    img.thumbnail((THUMB, THUMB))
    photo = ImageTk.PhotoImage(img)

    win = tk.Toplevel(tk_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.0)
    win.configure(bg=_C["bg2"])

    pw, ph = photo.width(), photo.height()
    bar_h = 56
    sw = tk_root.winfo_screenwidth()
    sh = tk_root.winfo_screenheight()
    wx = sw - pw - 20
    wy = sh - ph - bar_h - 56

    win.geometry(f"{pw}x{ph + bar_h}+{wx}+{wy}")

    canvas = tk.Canvas(win, width=pw, height=ph, highlightthickness=0, bg="#000000")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas._keep = photo

    bar = tk.Frame(win, bg=_C["bg2"])
    bar.pack(fill=tk.X)
    tk.Label(bar, text="Path copied", bg=_C["bg2"], fg=_C["fg3"], font=(_FF, 10)).pack(
        anchor="w", padx=14, pady=(7, 3))

    btn_row = tk.Frame(bar, bg=_C["bg2"])
    btn_row.pack(fill=tk.X, padx=10, pady=(0, 9))

    def _fade_in(i=0, steps=8):
        if not win.winfo_exists():
            return
        win.attributes("-alpha", min(1.0, i / steps))
        if i < steps:
            win.after(15, lambda: _fade_in(i + 1, steps))

    def _fade_out(callback=None):
        def step(i=8):
            if not win.winfo_exists():
                return
            if i <= 0:
                win.destroy()
                if callback:
                    callback()
                return
            win.attributes("-alpha", i / 8)
            win.after(18, lambda: step(i - 1))
        step()

    def open_it(e=None):
        _fade_out(lambda: os.startfile(filepath))

    def edit_it(e=None):
        _fade_out(lambda: _open_studio(full_res, filepath))

    tk.Button(btn_row, text="Open File", command=open_it,
              bg=_C["bg3"], fg=_C["fg"], relief="flat", bd=0,
              font=(_FF, 10), padx=10, pady=5, cursor="hand2",
              activebackground=_C["sep"]).pack(side=tk.LEFT, padx=(4, 4))
    tk.Button(btn_row, text="Edit in CaptThat Studio", command=edit_it,
              bg=_C["accent"], fg="white", relief="flat", bd=0,
              font=(_FF, 10, "bold"), padx=10, pady=5, cursor="hand2",
              activebackground="#0070E0").pack(side=tk.LEFT, padx=(0, 4))

    def show_rc_menu(e):
        m = tk.Menu(win, tearoff=0, bg=_C["bg2"], fg=_C["fg"],
                    activebackground=_C["accent"], activeforeground="white",
                    relief="flat", bd=0, font=(_FF, 11))
        m.add_command(label="Open file", command=open_it)
        m.add_command(label="Edit in CaptThat Studio", command=edit_it)
        m.add_separator()
        m.add_command(label="Settings…", command=lambda: _fade_out(open_settings))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # bind only on the thumbnail, not `win` — a toplevel-level binding fires for
    # every descendant click (including the buttons below) since it's in their
    # bindtags too, and firing on press would beat the buttons' own release-triggered
    # commands to the punch
    canvas.bind("<Button-1>", open_it)
    canvas.bind("<Button-3>", show_rc_menu)

    _fade_in()
    dur = int(float(config.get("preview_duration", 2.5)) * 1000)
    win.after(dur, lambda: _fade_out() if win.winfo_exists() else None)


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
    canvas.bind("<ButtonPress-3>", on_cancel)   # right-click always cancels, focus or not
    canvas.bind("<Escape>", on_cancel)
    win.bind("<Escape>", on_cancel)
    win.bind("<Destroy>", on_destroy)

    win.update()
    try:
        _force_foreground(win.winfo_id())
    except Exception:
        pass
    win.lift()
    win.focus_force()
    canvas.focus_set()


# ── CaptThat Studio (image editor) ────────────────────────────────────────────

def _studio_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _pil_arrow(draw: ImageDraw.ImageDraw, x1, y1, x2, y2, color, width):
    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    ang = math.atan2(y2 - y1, x2 - x1)
    head = max(10, width * 4)
    for sign in (-1, 1):
        a = ang + math.pi - sign * 0.5
        draw.line([(x2, y2), (x2 + head * math.cos(a), y2 + head * math.sin(a))],
                  fill=color, width=width)


def _pil_bubble_rect(draw: ImageDraw.ImageDraw, rect, color):
    x1, y1, x2, y2 = rect
    draw.rounded_rectangle([x1, y1, x2, y2], radius=14, fill=color)
    draw.polygon([(x1 + 16, y2), (x1 + 34, y2), (x1 + 20, y2 + 16)], fill=color)


def _pixelate(img: Image.Image, rect, block: float):
    x1, y1, x2, y2 = [int(v) for v in rect]
    patch = img.crop((x1, y1, x2, y2))
    block = max(2, int(block))
    w, h = max(1, patch.width), max(1, patch.height)
    small = patch.resize((max(1, w // block), max(1, h // block)), Image.BILINEAR)
    return small.resize((w, h), Image.NEAREST)


def _blur_patch(img: Image.Image, rect, radius: float):
    x1, y1, x2, y2 = [int(v) for v in rect]
    return img.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(max(1, radius)))


def _smart_redact_scan(img: Image.Image, sensitivity=0.5, tile=16, max_boxes=12):
    """Heuristic (no-OCR) scan for text-like busy regions: edge-density per tile,
    merged into candidate boxes for the user to review before blurring."""
    gray = img.convert("L").filter(ImageFilter.FIND_EDGES)
    w, h = gray.size
    cols, rows = max(1, w // tile), max(1, h // tile)
    thresh = 60 - sensitivity * 40
    busy = [[ImageStat.Stat(gray.crop((c * tile, r * tile, min(w, (c + 1) * tile), min(h, (r + 1) * tile)))).mean[0] > thresh
             for c in range(cols)] for r in range(rows)]

    strips = []
    for r in range(rows):
        c = 0
        while c < cols:
            if not busy[r][c]:
                c += 1
                continue
            start = c
            while c < cols and busy[r][c]:
                c += 1
            strips.append([start * tile, r * tile, c * tile, (r + 1) * tile])

    strips.sort(key=lambda s: (s[1], s[0]))
    merged = []
    for s in strips:
        for m in merged:
            if s[0] < m[2] and s[2] > m[0] and s[1] - m[3] <= tile * 1.5 and (m[3] - m[1]) < tile * 6:
                m[0], m[2] = min(m[0], s[0]), max(m[2], s[2])
                m[3] = max(m[3], s[3])
                break
        else:
            merged.append(s[:])

    merged.sort(key=lambda m: (m[2] - m[0]) * (m[3] - m[1]), reverse=True)
    return [tuple(m) for m in merged[:max_boxes]]


def _apply_presentation_frame(img: Image.Image, opts: dict) -> Image.Image:
    img = img.convert("RGB")
    pad = opts.get("padding", 64)
    radius = opts.get("radius", 18)
    chrome_h = 34 if opts.get("chrome") else 0

    shot = img
    if chrome_h:
        bar = Image.new("RGB", (img.width, chrome_h), "#E5E5E7")
        d = ImageDraw.Draw(bar)
        for i, c in enumerate(["#FF5F57", "#FEBC2E", "#28C840"]):
            d.ellipse([16 + i * 22, chrome_h // 2 - 6, 28 + i * 22, chrome_h // 2 + 6], fill=c)
        combined = Image.new("RGB", (img.width, img.height + chrome_h))
        combined.paste(bar, (0, 0))
        combined.paste(img, (0, chrome_h))
        shot = combined

    mask = Image.new("L", shot.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, shot.width - 1, shot.height - 1], radius=radius, fill=255)
    rounded = Image.new("RGBA", shot.size, (0, 0, 0, 0))
    rounded.paste(shot, mask=mask)

    canvas_w, canvas_h = shot.width + pad * 2, shot.height + pad * 2
    bg_mode = opts.get("bg_mode", "ambient")
    bg_color = opts.get("bg_color", "#1C1C1E")

    if bg_mode == "solid":
        bg = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    elif bg_mode == "gradient":
        rgb = ImageColor.getrgb(bg_color)
        bottom = tuple(max(0, c - 60) for c in rgb)
        seed = Image.new("RGB", (1, 2))
        seed.putpixel((0, 0), rgb)
        seed.putpixel((0, 1), bottom)
        bg = seed.resize((canvas_w, canvas_h), Image.BILINEAR)
    else:  # ambient — blurred, darkened extend of the screenshot itself
        cover = max(canvas_w / shot.width, canvas_h / shot.height)
        big = shot.resize((max(1, int(shot.width * cover)), max(1, int(shot.height * cover))), Image.LANCZOS)
        left, top = (big.width - canvas_w) // 2, (big.height - canvas_h) // 2
        big = big.crop((left, top, left + canvas_w, top + canvas_h))
        bg = ImageEnhance.Brightness(big.filter(ImageFilter.GaussianBlur(50))).enhance(0.7)

    canvas = bg.convert("RGBA")

    if opts.get("shadow", True):
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        off = 12
        ImageDraw.Draw(shadow).rounded_rectangle(
            [pad + off, pad + off, pad + shot.width + off, pad + shot.height + off],
            radius=radius, fill=(0, 0, 0, 140))
        canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(24)))

    canvas.paste(rounded, (pad, pad), mask=rounded)
    return canvas.convert("RGB")


def _tool_icon(tool: str, size: int = 22, color: str = "#C9C9CC") -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(2, s // 14)
    pad = s // 6

    if tool == "select":
        d.polygon([(pad, pad), (pad, s - pad * 1.5), (pad + s * 0.28, s - pad * 2.1),
                  (pad + s * 0.42, s - pad * 0.95), (pad + s * 0.55, s - pad * 1.25),
                  (pad + s * 0.30, s - pad * 2.5), (s - pad * 1.15, s - pad * 2.5)], fill=color)
    elif tool == "pen":
        d.line([(pad, s - pad), (s - pad, pad)], fill=color, width=lw)
        d.polygon([(s - pad - lw * 2.2, pad), (s - pad, pad), (s - pad, pad + lw * 2.2)], fill=color)
    elif tool == "line":
        d.line([(pad, s - pad), (s - pad, pad)], fill=color, width=lw)
    elif tool == "arrow":
        d.line([(pad, s - pad), (s - pad, pad)], fill=color, width=lw)
        ang = math.atan2(pad - (s - pad), (s - pad) - pad)
        for sign in (-1, 1):
            a = ang + math.pi - sign * 0.5
            d.line([(s - pad, pad), (s - pad + s * 0.22 * math.cos(a), pad + s * 0.22 * math.sin(a))],
                  fill=color, width=lw)
    elif tool == "rect":
        d.rectangle([pad, pad, s - pad, s - pad], outline=color, width=lw)
    elif tool == "ellipse":
        d.ellipse([pad, pad, s - pad, s - pad], outline=color, width=lw)
    elif tool == "text":
        d.text((s / 2, s / 2), "T", fill=color, font=_studio_font(int(s * 0.62)), anchor="mm")
    elif tool == "callout":
        d.rounded_rectangle([pad, pad, s - pad, s - pad * 1.7], radius=s * 0.12, outline=color, width=lw)
        d.polygon([(pad + s * 0.15, s - pad * 1.7), (pad + s * 0.38, s - pad * 1.7),
                  (pad + s * 0.20, s - pad * 0.85)], fill=color)
    elif tool == "marker":
        d.ellipse([pad, pad, s - pad, s - pad], outline=color, width=lw)
        d.text((s / 2, s / 2), "1", fill=color, font=_studio_font(int(s * 0.42)), anchor="mm")
    elif tool == "blur":
        n = 3
        cell = (s - 2 * pad) / n
        for row in range(n):
            for col in range(n):
                if (row + col) % 2 == 0:
                    x0, y0 = pad + col * cell, pad + row * cell
                    d.rectangle([x0, y0, x0 + cell, y0 + cell], fill=color)
    elif tool == "smart":
        d.line([(pad, s - pad), (s - pad * 1.5, pad * 1.5)], fill=color, width=lw)
        for dx, dy, r in [(-0.14, -0.14, s * 0.09), (0.18, 0.05, s * 0.06)]:
            cx, cy = s - pad * 1.5 + dx * s, pad * 1.5 + dy * s
            d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=color)
    elif tool == "crop":
        arm = s * 0.28
        for cx, cy, dx, dy in [(pad, pad, 1, 0), (pad, pad, 0, 1),
                               (s - pad, s - pad, -1, 0), (s - pad, s - pad, 0, -1)]:
            d.line([(cx, cy), (cx + dx * arm, cy + dy * arm)], fill=color, width=lw)
    elif tool == "key":
        bow_d = s * 0.34
        bow_x, bow_y = pad, s * 0.5 - bow_d / 2
        d.ellipse([bow_x, bow_y, bow_x + bow_d, bow_y + bow_d], outline=color, width=lw)
        shaft_y = s * 0.5
        shaft_x2 = s - pad
        d.line([(bow_x + bow_d, shaft_y), (shaft_x2, shaft_y)], fill=color, width=lw)
        d.line([(shaft_x2 - s * 0.10, shaft_y), (shaft_x2 - s * 0.10, shaft_y + s * 0.16)], fill=color, width=lw)
        d.line([(shaft_x2, shaft_y), (shaft_x2, shaft_y + s * 0.22)], fill=color, width=lw)
    elif tool == "folder":
        top = s * 0.36
        tab_w = s * 0.30
        d.rounded_rectangle([pad, top - s * 0.09, pad + tab_w, top + s * 0.02],
                            radius=s * 0.03, outline=color, width=lw)
        d.rounded_rectangle([pad, top, s - pad, s - pad], radius=s * 0.06, outline=color, width=lw)
    elif tool == "camera":
        body_top = s * 0.40
        d.rounded_rectangle([pad, body_top, s - pad, s - pad], radius=s * 0.08, outline=color, width=lw)
        vw = s * 0.22
        d.rounded_rectangle([s / 2 - vw / 2, body_top - s * 0.10, s / 2 + vw / 2, body_top + s * 0.02],
                            radius=s * 0.03, outline=color, width=lw)
        r = s * 0.15
        cx, cy = s / 2, (body_top + s - pad) / 2 + s * 0.02
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=lw)
    elif tool == "bell":
        cx = s / 2
        dome_r = s * 0.22
        dome_cy = s * 0.34
        d.arc([cx - dome_r, dome_cy - dome_r, cx + dome_r, dome_cy + dome_r],
              start=180, end=360, fill=color, width=lw)
        left_bottom, right_bottom = (cx - s * 0.34, s * 0.64), (cx + s * 0.34, s * 0.64)
        d.line([(cx - dome_r, dome_cy), left_bottom], fill=color, width=lw)
        d.line([(cx + dome_r, dome_cy), right_bottom], fill=color, width=lw)
        d.line([left_bottom, right_bottom], fill=color, width=lw)
        r = s * 0.06
        d.ellipse([cx - r, s * 0.66, cx + r, s * 0.66 + 2 * r], fill=color)
    elif tool == "gear":
        cx, cy = s / 2, s / 2
        r_outer, r_inner = s * 0.30, s * 0.13
        for i in range(8):
            ang = i * (2 * math.pi / 8)
            tx, ty, tw = cx + r_outer * math.cos(ang), cy + r_outer * math.sin(ang), s * 0.06
            d.ellipse([tx - tw, ty - tw, tx + tw, ty + tw], fill=color)
        d.ellipse([cx - r_outer * 0.72, cy - r_outer * 0.72, cx + r_outer * 0.72, cy + r_outer * 0.72],
                  outline=color, width=lw)
        d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], outline=color, width=lw)

    return img.resize((size, size), Image.LANCZOS)


def _open_studio(img: Image.Image, filepath: str):
    main_queue.put(lambda: _Studio(tk_root, img, filepath))


class _Studio:
    TOOLS = ["select", "pen", "line", "arrow", "rect", "ellipse",
             "text", "callout", "marker", "blur", "smart", "crop"]
    TOOL_LABELS = {
        "select": "Select", "pen": "Pen", "line": "Line", "arrow": "Arrow",
        "rect": "Rectangle", "ellipse": "Ellipse", "text": "Text", "callout": "Callout",
        "marker": "Step #", "blur": "Blur", "smart": "Smart Redact", "crop": "Crop",
    }

    def __init__(self, root, img: Image.Image, filepath: str):
        self.filepath = filepath
        self.orig_img = img.convert("RGB")
        self.dirty = False
        self.tool = "select"
        self.color = "#FF3B30"
        self.width = 3
        self.candidates = []
        self.history = []      # [("op", op_dict) | ("snapshot", before, after)]
        self.redo_stack = []
        self.ops = []
        self._drag = None
        self._crop_rect_id = None
        self._editing_text = False

        self.win = ctk.CTkToplevel(root)
        self.win.title(f"CaptThat Studio  ·  {os.path.basename(filepath)}")
        self.win.configure(fg_color=self._P["canvas"])
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        _dark_title_bar(self.win)

        self._build_ui()
        self._load_image(self.orig_img)
        self.win.focus_force()

    # ── layout (Photoshop-style: icon tool rail, options bar, status bar) ─────
    _P = {
        "toolbar": "#1E1E1F", "toolbar_active": "#37373A",
        "top": "#1B1B1C", "opts": "#2B2B2D", "status": "#1B1B1C",
        "canvas": "#171718", "border": "#3F3F42",
        "text_dim": "#9A9A9E", "text_bright": "#EDEDED",
    }
    _CURSORS = {"select": "arrow", "text": "xterm", "crop": "cross",
                "blur": "cross", "smart": "arrow", "marker": "hand2"}

    def _build_ui(self):
        P = self._P
        self._icons = {t: (ctk.CTkImage(light_image=_tool_icon(t, 18, P["text_dim"]), size=(18, 18)),
                            ctk.CTkImage(light_image=_tool_icon(t, 18, P["text_bright"]), size=(18, 18)))
                       for t in self.TOOLS}

        # top action bar
        top = ctk.CTkFrame(self.win, fg_color=P["top"], corner_radius=0, height=48)
        top.pack(side=tk.TOP, fill=tk.X)

        def top_btn(text, cmd, side=tk.LEFT, accent=False, width=90):
            b = ctk.CTkButton(top, text=text, command=cmd, width=width, height=30, corner_radius=6,
                              fg_color=_C["accent"] if accent else "transparent",
                              hover_color="#0070E0" if accent else P["toolbar_active"],
                              text_color="white" if accent else P["text_bright"],
                              font=(_FF, 12, "bold" if accent else "normal"))
            b.pack(side=side, padx=6, pady=9)
            return b

        top_btn("↶ Undo", self._undo, width=78)
        top_btn("↷ Redo", self._redo, width=78)
        self.apply_btn = top_btn("Apply Crop", self._apply_crop, accent=True, width=110)
        self.apply_btn.pack_forget()
        self.smart_apply_btn = top_btn("Apply All Redactions", self._smart_apply_all, accent=True, width=170)
        self.smart_apply_btn.pack_forget()

        top_btn("Copy", self._copy, side=tk.RIGHT, width=78)
        top_btn("Save As…", self._save_as, side=tk.RIGHT, width=98)
        top_btn("Save", self._save, side=tk.RIGHT, accent=True, width=90)

        # contextual options bar
        opts = ctk.CTkFrame(self.win, fg_color=P["opts"], corner_radius=0, height=46)
        opts.pack(side=tk.TOP, fill=tk.X)

        ctk.CTkLabel(opts, text="Color", font=(_FF, 11), text_color=P["text_dim"]).pack(
            side=tk.LEFT, padx=(16, 8), pady=10)
        self.color_swatch = ctk.CTkButton(opts, text=" ", width=30, height=28, corner_radius=7,
                                          fg_color=self.color, hover_color=self.color,
                                          border_width=1, border_color=P["border"],
                                          command=self._pick_color)
        self.color_swatch.pack(side=tk.LEFT, pady=9)

        ctk.CTkFrame(opts, fg_color=P["border"], width=1, corner_radius=0).pack(
            side=tk.LEFT, fill=tk.Y, padx=16, pady=10)

        ctk.CTkLabel(opts, text="Size", font=(_FF, 11), text_color=P["text_dim"]).pack(
            side=tk.LEFT, padx=(0, 8))
        width_slider = ctk.CTkSlider(opts, from_=1, to=20, width=120,
                                     progress_color=_C["accent"], button_color=_C["accent"],
                                     button_hover_color="#0070E0",
                                     command=lambda v: setattr(self, "width", int(round(v))))
        width_slider.set(self.width)
        width_slider.pack(side=tk.LEFT, pady=10)

        ctk.CTkFrame(opts, fg_color=P["border"], width=1, corner_radius=0).pack(
            side=tk.LEFT, fill=tk.Y, padx=16, pady=10)

        ctk.CTkButton(opts, text="✨ Presentation Frame…", command=self._open_frame_dialog,
                     fg_color=_C["accent"], hover_color="#0070E0", text_color="white",
                     font=(_FF, 11, "bold"), corner_radius=6, width=180, height=30).pack(side=tk.LEFT, pady=8)

        # body: icon tool rail + canvas
        body = ctk.CTkFrame(self.win, fg_color=P["canvas"], corner_radius=0)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        rail = ctk.CTkFrame(body, fg_color=P["toolbar"], corner_radius=0, width=48)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        self.rail = rail

        self.tool_btns = {}
        for t in self.TOOLS:
            btn = ctk.CTkButton(rail, text=" ", image=self._icons[t][0], width=48, height=36,
                                corner_radius=0, fg_color=P["toolbar"], hover_color=P["toolbar_active"],
                                command=lambda tool=t: self._select_tool(tool))
            btn.pack(fill=tk.X)
            btn.bind("<Enter>", lambda e, tool=t: self.status_lbl.configure(text=self.TOOL_LABELS[tool]))
            btn.bind("<Leave>", lambda e: self.status_lbl.configure(text=self.TOOL_LABELS[self.tool]))
            self.tool_btns[t] = btn

        canvas_area = ctk.CTkFrame(body, fg_color=P["canvas"], corner_radius=0)
        canvas_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(canvas_area, bg=P["canvas"], highlightthickness=0, cursor="arrow")
        self.canvas.pack(expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Control-z>", lambda e: self._undo())
        self.win.bind("<Control-y>", lambda e: self._redo())

        # status bar
        status = ctk.CTkFrame(self.win, fg_color=P["status"], corner_radius=0, height=28)
        status.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_lbl = ctk.CTkLabel(status, text=self.TOOL_LABELS[self.tool], font=(_FF, 10),
                                       text_color=P["text_dim"])
        self.status_lbl.pack(side=tk.LEFT, padx=14)
        self.dim_lbl = ctk.CTkLabel(status, text="", font=(_FF, 10), text_color=P["text_dim"])
        self.dim_lbl.pack(side=tk.RIGHT, padx=14)

        self._select_tool(self.tool)

    def _load_image(self, img: Image.Image):
        sw = self.win.winfo_screenwidth() - 140
        sh = self.win.winfo_screenheight() - 320
        self.scale = min(1.0, sw / img.width, sh / img.height)
        disp_w = max(1, int(img.width * self.scale))
        disp_h = max(1, int(img.height * self.scale))
        self.disp_img = img.resize((disp_w, disp_h), Image.LANCZOS) if self.scale < 1 else img.copy()

        self.canvas.config(width=disp_w, height=disp_h)
        self.canvas.delete("all")
        self._base_photo = ImageTk.PhotoImage(self.disp_img)
        self.canvas.create_image(0, 0, image=self._base_photo, anchor="nw")
        self.candidates = []
        self.win.update_idletasks()
        if hasattr(self, "dim_lbl"):
            self.dim_lbl.configure(text=f"{img.width} × {img.height}px  ·  {round(self.scale * 100)}%")

    # ── tool selection ───────────────────────────────────────────────────────
    def _select_tool(self, tool):
        self.tool = tool
        P = self._P
        for t, btn in self.tool_btns.items():
            active = t == tool
            btn.configure(fg_color=_C["accent"] if active else P["toolbar"],
                          image=self._icons[t][1] if active else self._icons[t][0])
        self.status_lbl.configure(text=self.TOOL_LABELS[tool])
        self.canvas.configure(cursor=self._CURSORS.get(tool, "tcross"))
        self.apply_btn.pack_forget()
        if tool == "crop" and self._crop_rect_id:
            self.apply_btn.pack(side=tk.LEFT, padx=6, pady=9)
        if tool == "smart":
            self._scan_smart_redact()

    def _pick_color(self):
        result = colorchooser.askcolor(color=self.color, parent=self.win, title="Color")
        if result and result[1]:
            self.color = result[1]
            self.color_swatch.configure(fg_color=self.color, hover_color=self.color)

    # ── drawing events ───────────────────────────────────────────────────────
    def _on_press(self, e):
        if self._editing_text:
            return
        x, y, t = e.x, e.y, self.tool
        if t == "pen":
            iid = self.canvas.create_line(x, y, x, y, fill=self.color, width=self.width,
                                           capstyle=tk.ROUND, joinstyle=tk.ROUND)
            self._drag = {"pts": [(x, y)], "id": iid}
        elif t == "line":
            self._drag = {"start": (x, y), "id": self.canvas.create_line(
                x, y, x, y, fill=self.color, width=self.width)}
        elif t == "arrow":
            shape = (max(10, self.width * 3), max(12, self.width * 4), max(4, self.width))
            self._drag = {"start": (x, y), "id": self.canvas.create_line(
                x, y, x, y, fill=self.color, width=self.width, arrow=tk.LAST, arrowshape=shape)}
        elif t == "rect":
            self._drag = {"start": (x, y), "id": self.canvas.create_rectangle(
                x, y, x, y, outline=self.color, width=self.width)}
        elif t == "ellipse":
            self._drag = {"start": (x, y), "id": self.canvas.create_oval(
                x, y, x, y, outline=self.color, width=self.width)}
        elif t == "crop":
            if self._crop_rect_id:
                self.canvas.delete(self._crop_rect_id)
            self._crop_rect_id = self.canvas.create_rectangle(
                x, y, x, y, outline="#38bdf8", width=2, dash=(4, 3))
            self._drag = {"start": (x, y), "id": self._crop_rect_id}
        elif t == "blur":
            self._drag = {"start": (x, y), "id": self.canvas.create_rectangle(
                x, y, x, y, outline="#FF9F0A", width=2, dash=(4, 3))}
        elif t == "text":
            self._place_text(x, y, bubble=False)
        elif t == "callout":
            self._place_text(x, y, bubble=True)
        elif t == "marker":
            self._place_marker(x, y)

    def _on_drag(self, e):
        if not self._drag:
            return
        t = self.tool
        if t == "pen":
            last = self._drag["pts"][-1]
            if (e.x - last[0]) ** 2 + (e.y - last[1]) ** 2 >= 9:
                self._drag["pts"].append((e.x, e.y))
                self.canvas.coords(self._drag["id"], *[c for p in self._drag["pts"] for c in p])
        elif t in ("line", "arrow", "rect", "ellipse", "crop", "blur"):
            sx, sy = self._drag["start"]
            self.canvas.coords(self._drag["id"], sx, sy, e.x, e.y)

    def _on_release(self, e):
        if not self._drag:
            return
        t, d = self.tool, self._drag
        self._drag = None
        if t == "pen":
            if len(d["pts"]) < 2:
                self.canvas.delete(d["id"])
                return
            self._commit({"type": "pen", "points": d["pts"], "color": self.color,
                          "width": self.width, "ids": [d["id"]]})
        elif t in ("line", "arrow", "rect", "ellipse"):
            sx, sy = d["start"]
            if abs(e.x - sx) < 3 and abs(e.y - sy) < 3:
                self.canvas.delete(d["id"])
                return
            self._commit({"type": t, "x1": sx, "y1": sy, "x2": e.x, "y2": e.y,
                          "color": self.color, "width": self.width, "ids": [d["id"]]})
        elif t == "crop":
            self.apply_btn.pack(side=tk.LEFT)
        elif t == "blur":
            sx, sy = d["start"]
            rect = (min(sx, e.x), min(sy, e.y), max(sx, e.x), max(sy, e.y))
            self.canvas.delete(d["id"])
            if rect[2] - rect[0] >= 4 and rect[3] - rect[1] >= 4:
                self._apply_blur_rect(rect, mode="blur")

    def _place_text(self, x, y, bubble):
        self._editing_text = True
        entry = tk.Text(self.canvas, width=20, height=2, font=(_FF, 12), bg="#FFFFFF",
                        fg="#111111", relief="flat", insertbackground="#111111")
        win_id = self.canvas.create_window(x, y, window=entry, anchor="nw")
        entry.focus_force()

        def cancel(event=None):
            self._editing_text = False
            self.canvas.delete(win_id)
            entry.destroy()

        def commit(event=None):
            self._editing_text = False
            text = entry.get("1.0", "end").strip()
            self.canvas.delete(win_id)
            entry.destroy()
            if not text:
                return
            font = (_FF, max(10, self.width * 3 + 8))
            if bubble:
                pad = 10
                tmp_id = self.canvas.create_text(x + pad, y + pad, text=text, fill="#111111",
                                                  font=font, anchor="nw", width=240)
                bx1, by1, bx2, by2 = self.canvas.bbox(tmp_id)
                self.canvas.delete(tmp_id)
                rect_id = self.canvas.create_rectangle(x, y, bx2 + pad, by2 + pad, fill=self.color, outline="")
                tail_id = self.canvas.create_polygon(x + 16, by2 + pad, x + 34, by2 + pad,
                                                      x + 20, by2 + pad + 16, fill=self.color, outline="")
                txt_id = self.canvas.create_text(bx1, by1, text=text, fill="#111111",
                                                  font=font, anchor="nw", width=240)
                op = {"type": "callout", "x": x, "y": y, "text": text, "color": self.color,
                      "font_size": font[1], "ids": [rect_id, tail_id, txt_id]}
            else:
                txt_id = self.canvas.create_text(x, y, text=text, fill=self.color, font=font, anchor="nw")
                op = {"type": "text", "x": x, "y": y, "text": text, "color": self.color,
                      "font_size": font[1], "ids": [txt_id]}
            self._commit(op)

        entry.bind("<Return>", lambda e: (commit(), "break")[1])
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", commit)

    def _place_marker(self, x, y):
        n = 1 + sum(1 for op in self.ops if op["type"] == "marker")
        r = 14
        oval_id = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=self.color, outline="white", width=2)
        txt_id = self.canvas.create_text(x, y, text=str(n), fill="white", font=(_FF, 12, "bold"))
        self._commit({"type": "marker", "x": x, "y": y, "n": n, "color": self.color,
                      "ids": [oval_id, txt_id]})

    def _apply_blur_rect(self, rect, mode="blur"):
        patch = _blur_patch(self.disp_img, rect, 6) if mode == "blur" else _pixelate(self.disp_img, rect, 10)
        photo = ImageTk.PhotoImage(patch)
        img_id = self.canvas.create_image(rect[0], rect[1], image=photo, anchor="nw")
        self._commit({"type": "blur", "rect": rect, "mode": mode, "ids": [img_id], "_photo": photo})

    # ── undo / redo ──────────────────────────────────────────────────────────
    def _commit(self, op):
        self.ops.append(op)
        self.history.append(("op", op))
        self.redo_stack.clear()
        self.dirty = True

    def _op_export_data(self, op):
        return {k: v for k, v in op.items() if k not in ("ids", "_photo")}

    def _hide_ids(self, ids):
        for i in ids:
            self.canvas.itemconfigure(i, state="hidden")

    def _show_ids(self, ids):
        for i in ids:
            self.canvas.itemconfigure(i, state="normal")

    def _undo(self):
        if self._editing_text or not self.history:
            return
        kind, *rest = self.history.pop()
        if kind == "op":
            op = rest[0]
            self._hide_ids(op["ids"])
            self.ops.remove(op)
            self.redo_stack.append(("op", op))
        else:
            before, after = rest
            self.redo_stack.append(("snapshot", before, after))
            self._restore_snapshot(before)
        self.dirty = True

    def _redo(self):
        if self._editing_text or not self.redo_stack:
            return
        kind, *rest = self.redo_stack.pop()
        if kind == "op":
            op = rest[0]
            self._show_ids(op["ids"])
            self.ops.append(op)
            self.history.append(("op", op))
        else:
            before, after = rest
            self.history.append(("snapshot", before, after))
            self._restore_snapshot(after)

    def _restore_snapshot(self, state):
        self.orig_img = state["img"].copy()
        self._load_image(self.orig_img)
        self.ops = []
        for data in state["ops_data"]:
            self._replay_op(data)

    def _replay_op(self, data):
        t = data["type"]
        if t == "pen":
            iid = self.canvas.create_line(*[c for p in data["points"] for c in p],
                                           fill=data["color"], width=data["width"],
                                           capstyle=tk.ROUND, joinstyle=tk.ROUND)
            op = {**data, "ids": [iid]}
        elif t in ("line", "arrow", "rect", "ellipse"):
            x1, y1, x2, y2 = data["x1"], data["y1"], data["x2"], data["y2"]
            if t == "line":
                iid = self.canvas.create_line(x1, y1, x2, y2, fill=data["color"], width=data["width"])
            elif t == "arrow":
                iid = self.canvas.create_line(x1, y1, x2, y2, fill=data["color"],
                                               width=data["width"], arrow=tk.LAST)
            elif t == "rect":
                iid = self.canvas.create_rectangle(x1, y1, x2, y2, outline=data["color"], width=data["width"])
            else:
                iid = self.canvas.create_oval(x1, y1, x2, y2, outline=data["color"], width=data["width"])
            op = {**data, "ids": [iid]}
        elif t == "text":
            iid = self.canvas.create_text(data["x"], data["y"], text=data["text"],
                                           fill=data["color"], font=(_FF, data["font_size"]), anchor="nw")
            op = {**data, "ids": [iid]}
        elif t == "marker":
            x, y, r = data["x"], data["y"], 14
            oval_id = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=data["color"], outline="white", width=2)
            txt_id = self.canvas.create_text(x, y, text=str(data["n"]), fill="white", font=(_FF, 12, "bold"))
            op = {**data, "ids": [oval_id, txt_id]}
        elif t == "callout":
            font = (_FF, data["font_size"])
            pad, x, y = 10, data["x"], data["y"]
            tmp_id = self.canvas.create_text(x + pad, y + pad, text=data["text"], fill="#111111",
                                              font=font, anchor="nw", width=240)
            bx1, by1, bx2, by2 = self.canvas.bbox(tmp_id)
            self.canvas.delete(tmp_id)
            rect_id = self.canvas.create_rectangle(x, y, bx2 + pad, by2 + pad, fill=data["color"], outline="")
            tail_id = self.canvas.create_polygon(x + 16, by2 + pad, x + 34, by2 + pad,
                                                  x + 20, by2 + pad + 16, fill=data["color"], outline="")
            txt_id = self.canvas.create_text(bx1, by1, text=data["text"], fill="#111111", font=font, anchor="nw", width=240)
            op = {**data, "ids": [rect_id, tail_id, txt_id]}
        elif t == "blur":
            mode = data["mode"]
            patch = _blur_patch(self.disp_img, data["rect"], 6) if mode == "blur" else _pixelate(self.disp_img, data["rect"], 10)
            photo = ImageTk.PhotoImage(patch)
            img_id = self.canvas.create_image(data["rect"][0], data["rect"][1], image=photo, anchor="nw")
            op = {**data, "ids": [img_id], "_photo": photo}
        else:
            return
        self.ops.append(op)

    # ── smart redact ─────────────────────────────────────────────────────────
    def _scan_smart_redact(self):
        for c in self.candidates:
            self.canvas.delete(c["rect_id"])
            self.canvas.delete(c["x_id"])
        self.candidates = []
        boxes = _smart_redact_scan(self.disp_img)
        for rect in boxes:
            rid = self.canvas.create_rectangle(*rect, outline="#FFCC00", width=2, dash=(5, 3))
            xid = self.canvas.create_text(rect[2] - 8, rect[1] + 8, text="✕", fill="#FFCC00", font=(_FF, 11, "bold"))
            self.canvas.tag_bind(xid, "<Button-1>", lambda e, rid=rid, xid=xid: self._reject_candidate(rid, xid))
            self.candidates.append({"rect": rect, "rect_id": rid, "x_id": xid})
        if self.candidates:
            self.smart_apply_btn.pack(side=tk.LEFT, padx=6, pady=9)
        else:
            self.smart_apply_btn.pack_forget()
        self.status_lbl.configure(
            text=f"{len(boxes)} region(s) flagged as text-like (heuristic, no OCR) — "
                 "click ✕ to skip one, or Apply All." if boxes else
                 "No busy/text-like regions found.")

    def _reject_candidate(self, rid, xid):
        self.canvas.delete(rid)
        self.canvas.delete(xid)
        self.candidates = [c for c in self.candidates if c["rect_id"] != rid]
        if not self.candidates:
            self.smart_apply_btn.pack_forget()

    def _smart_apply_all(self):
        for c in self.candidates:
            self.canvas.delete(c["rect_id"])
            self.canvas.delete(c["x_id"])
            self._apply_blur_rect(c["rect"], mode="blur")
        self.candidates = []
        self.smart_apply_btn.pack_forget()
        self.status_lbl.configure(text=self.TOOL_LABELS[self.tool])

    # ── crop ─────────────────────────────────────────────────────────────────
    def _apply_crop(self):
        if not self._crop_rect_id:
            return
        coords = self.canvas.coords(self._crop_rect_id)
        if len(coords) < 4:
            return
        x1, x2 = sorted((coords[0], coords[2]))
        y1, y2 = sorted((coords[1], coords[3]))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return

        before = {"img": self.orig_img.copy(),
                  "ops_data": [self._op_export_data(o) for o in self.ops], "scale": self.scale}
        full_rect = tuple(int(v / self.scale) for v in (x1, y1, x2, y2))
        cropped_full = self._flatten_full().crop(full_rect)
        after = {"img": cropped_full, "ops_data": [], "scale": 1.0}

        self.history.append(("snapshot", before, after))
        self.redo_stack.clear()
        self._crop_rect_id = None
        self.orig_img = cropped_full
        self._load_image(self.orig_img)
        self.dirty = True

    # ── presentation frame ───────────────────────────────────────────────────
    def _open_frame_dialog(self):
        P = self._P
        dlg = ctk.CTkToplevel(self.win)
        dlg.title("Presentation Frame")
        dlg.configure(fg_color=P["opts"])
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        _dark_title_bar(dlg)

        pad_var = tk.IntVar(value=64)
        radius_var = tk.IntVar(value=18)
        shadow_var = tk.BooleanVar(value=True)
        bg_mode_var = tk.StringVar(value="ambient")
        bg_color_var = tk.StringVar(value="#1C1C1E")
        chrome_var = tk.BooleanVar(value=False)

        frm = ctk.CTkFrame(dlg, fg_color="transparent")
        frm.pack(padx=22, pady=18)

        def row(r, label, widget):
            ctk.CTkLabel(frm, text=label, font=(_FF, 12), text_color=P["text_dim"]).grid(
                row=r, column=0, sticky="w", pady=8)
            widget.grid(row=r, column=1, sticky="w", padx=(16, 0), pady=8)

        pad_slider = ctk.CTkSlider(frm, from_=0, to=200, width=150,
                                   progress_color=_C["accent"], button_color=_C["accent"],
                                   button_hover_color="#0070E0",
                                   command=lambda v: pad_var.set(round(v)))
        pad_slider.set(64)
        row(0, "Padding", pad_slider)

        radius_slider = ctk.CTkSlider(frm, from_=0, to=48, width=150,
                                      progress_color=_C["accent"], button_color=_C["accent"],
                                      button_hover_color="#0070E0",
                                      command=lambda v: radius_var.set(round(v)))
        radius_slider.set(18)
        row(1, "Corner radius", radius_slider)

        bg_menu = ctk.CTkOptionMenu(frm, values=["ambient", "gradient", "solid"], width=150,
                                    fg_color=P["toolbar"], button_color=P["toolbar_active"],
                                    button_hover_color=_C["accent"],
                                    command=lambda v: bg_mode_var.set(v))
        bg_menu.set("ambient")
        row(2, "Background", bg_menu)

        swatch = ctk.CTkButton(frm, text=" ", width=34, height=28, corner_radius=6,
                               fg_color=bg_color_var.get(), hover_color=bg_color_var.get(),
                               border_width=1, border_color=P["border"])

        def pick():
            r = colorchooser.askcolor(color=bg_color_var.get(), parent=dlg)
            if r and r[1]:
                bg_color_var.set(r[1])
                swatch.configure(fg_color=r[1], hover_color=r[1])

        swatch.configure(command=pick)
        row(3, "Solid/gradient color", swatch)

        chk_frame = ctk.CTkFrame(frm, fg_color="transparent")
        chk_frame.grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ctk.CTkCheckBox(chk_frame, text="Drop shadow", variable=shadow_var, fg_color=_C["accent"],
                        hover_color="#0070E0", text_color=P["text_bright"]).pack(side=tk.LEFT, padx=(0, 16))
        ctk.CTkCheckBox(chk_frame, text="Browser chrome", variable=chrome_var, fg_color=_C["accent"],
                        hover_color="#0070E0", text_color=P["text_bright"]).pack(side=tk.LEFT)

        def apply():
            opts = {"padding": pad_var.get(), "radius": radius_var.get(), "shadow": shadow_var.get(),
                    "bg_mode": bg_mode_var.get(), "bg_color": bg_color_var.get(), "chrome": chrome_var.get()}
            self._apply_frame(opts)
            dlg.destroy()

        ctk.CTkButton(frm, text="Apply", command=apply, fg_color=_C["accent"], hover_color="#0070E0",
                     text_color="white", font=(_FF, 12, "bold"), corner_radius=6, width=150, height=32).grid(
            row=5, column=0, columnspan=2, pady=(18, 0))

    def _apply_frame(self, opts):
        before = {"img": self.orig_img.copy(),
                  "ops_data": [self._op_export_data(o) for o in self.ops], "scale": self.scale}
        framed = _apply_presentation_frame(self._flatten_full(), opts)
        after = {"img": framed, "ops_data": [], "scale": 1.0}
        self.history.append(("snapshot", before, after))
        self.redo_stack.clear()
        self.orig_img = framed
        self._load_image(self.orig_img)
        self.dirty = True

    # ── export / save ────────────────────────────────────────────────────────
    def _flatten_full(self) -> Image.Image:
        img = self.orig_img.copy().convert("RGB")
        draw = ImageDraw.Draw(img)
        factor = 1.0 / self.scale if self.scale else 1.0
        for op in self.ops:
            t = op["type"]
            if t == "pen":
                pts = [(px * factor, py * factor) for px, py in op["points"]]
                draw.line(pts, fill=op["color"], width=max(1, round(op["width"] * factor)), joint="curve")
            elif t == "line":
                draw.line([(op["x1"] * factor, op["y1"] * factor), (op["x2"] * factor, op["y2"] * factor)],
                          fill=op["color"], width=max(1, round(op["width"] * factor)))
            elif t == "arrow":
                _pil_arrow(draw, op["x1"] * factor, op["y1"] * factor, op["x2"] * factor, op["y2"] * factor,
                          op["color"], max(2, round(op["width"] * factor)))
            elif t == "rect":
                draw.rectangle([op["x1"] * factor, op["y1"] * factor, op["x2"] * factor, op["y2"] * factor],
                               outline=op["color"], width=max(1, round(op["width"] * factor)))
            elif t == "ellipse":
                draw.ellipse([op["x1"] * factor, op["y1"] * factor, op["x2"] * factor, op["y2"] * factor],
                            outline=op["color"], width=max(1, round(op["width"] * factor)))
            elif t == "text":
                font = _studio_font(max(10, round(op["font_size"] * factor)))
                draw.text((op["x"] * factor, op["y"] * factor), op["text"], fill=op["color"], font=font)
            elif t == "callout":
                font = _studio_font(max(10, round(op["font_size"] * factor)))
                pad = 10 * factor
                x, y = op["x"] * factor, op["y"] * factor
                bbox = draw.textbbox((x + pad, y + pad), op["text"], font=font)
                _pil_bubble_rect(draw, (x, y, bbox[2] + pad, bbox[3] + pad), op["color"])
                draw.multiline_text((bbox[0], bbox[1]), op["text"], fill="#111111", font=font)
            elif t == "marker":
                x, y, r = op["x"] * factor, op["y"] * factor, 14 * factor
                draw.ellipse([x - r, y - r, x + r, y + r], fill=op["color"], outline="white",
                            width=max(2, round(2 * factor)))
                font = _studio_font(max(12, round(12 * factor)))
                draw.text((x, y), str(op["n"]), fill="white", font=font, anchor="mm")
            elif t == "blur":
                full_rect = tuple(int(v * factor) for v in op["rect"])
                if full_rect[2] - full_rect[0] > 0 and full_rect[3] - full_rect[1] > 0:
                    patch = (_blur_patch(img, full_rect, 6 * factor) if op["mode"] == "blur"
                             else _pixelate(img, full_rect, 10 * factor))
                    img.paste(patch, (full_rect[0], full_rect[1]))
        return img

    def _save(self):
        try:
            self._flatten_full().save(self.filepath)
            self.dirty = False
            pyperclip.copy(self.filepath)
            _notify("Saved from CaptThat Studio")
        except Exception as e:
            print(f"[CaptThat] Studio save failed: {e}")

    def _save_as(self):
        path = filedialog.asksaveasfilename(
            parent=self.win, defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("All files", "*.*")],
            initialfile=os.path.basename(self.filepath))
        if not path:
            return
        img = self._flatten_full()
        if path.lower().endswith((".jpg", ".jpeg")):
            img.convert("RGB").save(path, quality=92, optimize=True)
        else:
            img.save(path)
        self.filepath = path
        self.dirty = False
        pyperclip.copy(path)

    def _copy(self):
        try:
            _copy_image_to_clipboard(self._flatten_full())
            _notify("Copied to clipboard")
        except Exception as e:
            print(f"[CaptThat] Studio copy failed: {e}")

    def _on_close(self):
        if self.dirty and not messagebox.askyesno(
                "Discard changes?", "You have unsaved edits. Close without saving?", parent=self.win):
            return
        self.win.destroy()


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
        track = _C["accent"] if on else _C["sep"]
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
    win = ctk.CTkToplevel(tk_root)
    win.title("CaptThat  ·  Settings")
    win.geometry("640x520")
    win.resizable(False, False)
    win.configure(fg_color=_C["bg"])
    win.attributes("-topmost", True)
    win.focus_force()
    _dark_title_bar(win)

    TABS = ["Hotkeys", "Output", "Capture", "After Capture", "System"]
    TAB_ICONS = {"Hotkeys": "key", "Output": "folder", "Capture": "camera",
                 "After Capture": "bell", "System": "gear"}
    icons = {n: (ctk.CTkImage(light_image=_tool_icon(TAB_ICONS[n], 18, _C["fg3"]), size=(18, 18)),
                 ctk.CTkImage(light_image=_tool_icon(TAB_ICONS[n], 18, "white"), size=(18, 18)))
             for n in TABS}

    # Root layout: icon sidebar (left) + content/button column (right)
    body = ctk.CTkFrame(win, fg_color=_C["bg"], corner_radius=0)
    body.pack(fill=tk.BOTH, expand=True)

    sidebar = ctk.CTkFrame(body, fg_color=_C["bg2"], corner_radius=0, width=184)
    sidebar.pack(side=tk.LEFT, fill=tk.Y)
    sidebar.pack_propagate(False)

    right = ctk.CTkFrame(body, fg_color=_C["bg"], corner_radius=0)
    right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    content = ctk.CTkFrame(right, fg_color=_C["bg"], corner_radius=0)
    content.pack(fill=tk.BOTH, expand=True, padx=22, pady=(20, 10))

    btn_bar = ctk.CTkFrame(right, fg_color=_C["bg"], corner_radius=0, height=64)
    btn_bar.pack(fill=tk.X, padx=22, pady=(0, 18))
    btn_bar.pack_propagate(False)

    ctk.CTkLabel(sidebar, text="Settings", font=(_FF, 16, "bold"), text_color=_C["fg"]).pack(
        anchor="w", padx=20, pady=(22, 16))

    panels: dict = {}
    tab_btns: dict = {}

    def switch(name):
        for t, b in tab_btns.items():
            active = t == name
            b.configure(fg_color=_C["accent"] if active else "transparent",
                        text_color="white" if active else _C["fg3"],
                        image=icons[t][1] if active else icons[t][0])
        for t, p in panels.items():
            if t == name:
                p.pack(fill=tk.BOTH, expand=True)
            else:
                p.pack_forget()

    for name in TABS:
        b = ctk.CTkButton(sidebar, text=f"  {name}", image=icons[name][0], anchor="w",
                          compound="left", width=152, height=38, corner_radius=8,
                          fg_color="transparent", hover_color=_C["bg3"],
                          text_color=_C["fg3"], font=(_FF, 12),
                          command=lambda n=name: switch(n))
        b.pack(padx=16, pady=2)
        tab_btns[name] = b

    # ── Shared helpers (all themed for a card's bg2 interior) ─────────────────
    def card(parent):
        c = ctk.CTkFrame(parent, fg_color=_C["bg2"], corner_radius=14)
        inner = tk.Frame(c, bg=_C["bg2"])
        inner.pack(fill=tk.BOTH, expand=True, padx=22, pady=20)
        return c, inner

    def section_lbl(parent, row, text, span=2):
        tk.Label(parent, text=text, font=(_FF, 11), bg=_C["bg2"],
                 fg=_C["fg3"], anchor="w").grid(
            row=row, column=0, columnspan=span, sticky="w", pady=(12, 3))

    def field_entry(parent, row, var, span=2):
        e = ctk.CTkEntry(parent, textvariable=var, font=(_FF, 12), height=34, corner_radius=8,
                         fg_color=_C["bg3"], border_width=1, border_color=_C["sep"],
                         text_color=_C["fg"])
        e.grid(row=row, column=0, columnspan=span, sticky="ew", pady=(0, 4))
        return e

    def hint_lbl(parent, row, text):
        tk.Label(parent, text=text, font=(_FF, 10), bg=_C["bg2"],
                 fg=_C["fg3"], anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))

    def toggle_row(parent, row, label, var):
        f = ctk.CTkFrame(parent, fg_color=_C["bg3"], corner_radius=8)
        f.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        f.grid_columnconfigure(0, weight=1)
        tk.Label(f, text=label, font=(_FF, 12), bg=_C["bg3"],
                 fg=_C["fg"], anchor="w").grid(
            row=0, column=0, sticky="w", padx=14, pady=9)
        t = _Toggle(f, var, bg_color=_C["bg3"])
        t.grid(row=0, column=1, padx=14, pady=9)
        f.bind("<Button-1>", lambda _: var.set(not var.get()))

    def spinbox_widget(parent, row, var, from_, to, col=1, inc=1.0):
        s = tk.Spinbox(parent, from_=from_, to=to, increment=inc,
                       textvariable=var, width=7,
                       font=(_FF, 12), bg=_C["bg3"], fg=_C["fg"],
                       buttonbackground=_C["bg3"], relief="flat",
                       highlightthickness=1, highlightbackground=_C["sep"],
                       highlightcolor=_C["accent"], insertbackground=_C["fg"])
        s.grid(row=row, column=col, sticky="w", padx=(10, 0), pady=4)
        return s

    def slider_row(parent, row, label, var, from_, to):
        tk.Label(parent, text=label, font=(_FF, 11), bg=_C["bg2"],
                 fg=_C["fg3"], anchor="w").grid(row=row, column=0, sticky="w", pady=(10, 3))
        f = tk.Frame(parent, bg=_C["bg2"])
        f.grid(row=row, column=1, sticky="w", padx=(10, 0), pady=(10, 3))
        is_float = isinstance(var, tk.DoubleVar)
        val_lbl = tk.Label(f, text=(f"{var.get():.2f}" if is_float else str(var.get())),
                           font=(_FF, 10), bg=_C["bg2"], fg=_C["fg3"], width=4, anchor="w")

        def on_change(v):
            v = round(float(v), 2) if is_float else int(round(float(v)))
            var.set(v)
            val_lbl.configure(text=f"{v:.2f}" if is_float else str(v))

        slider = ctk.CTkSlider(f, from_=from_, to=to, width=130,
                               progress_color=_C["accent"], button_color=_C["accent"],
                               button_hover_color="#0070E0", command=on_change)
        slider.set(var.get())
        slider.pack(side=tk.LEFT, padx=(0, 8))
        val_lbl.pack(side=tk.LEFT)

    def option_menu(parent, row, var, values, col=1):
        om = ctk.CTkOptionMenu(parent, values=values, width=130, height=32, corner_radius=8,
                               fg_color=_C["bg3"], button_color=_C["bg3"],
                               button_hover_color=_C["accent"], text_color=_C["fg"],
                               dropdown_fg_color=_C["bg2"], dropdown_text_color=_C["fg"],
                               dropdown_hover_color=_C["accent"], font=(_FF, 12),
                               command=lambda v: var.set(v))
        om.set(var.get())
        om.grid(row=row, column=col, sticky="w", padx=(10, 0), pady=4)
        return om

    # ── Hotkeys panel ─────────────────────────────────────────────────────────
    p1_card, p1 = card(content)
    panels["Hotkeys"] = p1_card
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
    p2_card, p2 = card(content)
    panels["Output"] = p2_card
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
    p3_card, p3 = card(content)
    panels["Capture"] = p3_card
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

    cc_row = tk.Frame(p3, bg=_C["bg2"])
    cc_row.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=(10, 3))
    cc_swatch = tk.Label(cc_row, bg=cc_var.get(), width=3, relief="flat")
    cc_swatch.pack(side=tk.LEFT, padx=(0, 8))

    def pick_color():
        result = colorchooser.askcolor(color=cc_var.get(), parent=win, title="Crosshair Color")
        if result and result[1]:
            cc_var.set(result[1])
            cc_swatch.configure(bg=result[1])

    ctk.CTkButton(cc_row, text="Choose…", command=pick_color, width=78, height=28, corner_radius=6,
                 fg_color=_C["bg3"], hover_color=_C["accent"], text_color=_C["fg"],
                 font=(_FF, 11)).pack(side=tk.LEFT)

    # ── After Capture panel ───────────────────────────────────────────────────
    p4_card, p4 = card(content)
    panels["After Capture"] = p4_card
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
    p5_card, p5 = card(content)
    panels["System"] = p5_card
    p5.columnconfigure(0, weight=1)

    startup_var = tk.BooleanVar(value=_startup_enabled())
    toggle_row(p5, 0, "Start with Windows", startup_var)
    tk.Label(p5, text=f"Version {VERSION}", font=(_FF, 11),
             bg=_C["bg2"], fg=_C["fg3"]).grid(row=1, column=0, sticky="w", pady=(28, 0))
    tk.Label(p5, text="github.com/eclipticprime558/capthat", font=(_FF, 10),
             bg=_C["bg2"], fg=_C["fg3"]).grid(row=2, column=0, sticky="w", pady=(2, 0))

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

    ctk.CTkButton(btn_bar, text="Cancel", command=win.destroy, width=90, height=34, corner_radius=8,
                 fg_color="transparent", hover_color=_C["bg3"], text_color=_C["fg"],
                 font=(_FF, 12)).pack(side=tk.LEFT, pady=14)
    ctk.CTkButton(btn_bar, text="Save", command=save, width=100, height=34, corner_radius=8,
                 fg_color=_C["accent"], hover_color="#0070E0", text_color="white",
                 font=(_FF, 12, "bold")).pack(side=tk.RIGHT, pady=14)

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
