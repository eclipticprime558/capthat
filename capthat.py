#!/usr/bin/env python3
"""
CaptThat v1.0.0
Screenshot tray app built for Claude Code on Windows.

Press the hotkey, drag to select, and the file path lands on your clipboard —
ready to Ctrl+V straight into Claude Code.

Dependencies:  pip install pillow keyboard pyperclip pystray plyer
"""

import os
import sys
import json
import time
import queue
import socket
import threading
import winreg
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from PIL import Image, ImageDraw, ImageEnhance, ImageGrab, ImageTk
import keyboard
import pyperclip
import pystray

VERSION = "1.0.0"

# ── paths ─────────────────────────────────────────────────────────────────────

_BASE = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(_BASE, "config.json")
REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "CaptThat"

DEFAULT_CONFIG = {
    "hotkey": "print screen",
    "output_dir": r"C:\Screenshots",
    "unique_names": False,   # True → timestamp filename; False → always latest.png
    "start_with_windows": False,
}

# ── state ─────────────────────────────────────────────────────────────────────

config: dict = {}
tray_icon: pystray.Icon | None = None
hotkey_id = None
main_queue: queue.Queue = queue.Queue()
tk_root: tk.Tk | None = None
_overlay_open = False
_lock_sock: socket.socket | None = None


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


# ── hotkey ────────────────────────────────────────────────────────────────────

def register_hotkey():
    global hotkey_id
    try:
        if hotkey_id is not None:
            keyboard.remove_hotkey(hotkey_id)
            hotkey_id = None
    except Exception:
        pass
    try:
        hotkey_id = keyboard.add_hotkey(
            config["hotkey"],
            lambda: main_queue.put(show_overlay),
            suppress=True,
        )
    except Exception as e:
        print(f"[CaptThat] hotkey '{config['hotkey']}' failed: {e}")


# ── capture + clipboard ───────────────────────────────────────────────────────

def _output_path() -> str:
    out_dir = config["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    if config.get("unique_names"):
        name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"
    else:
        name = "latest.png"
    return os.path.join(out_dir, name)


def save_screenshot(x1: int, y1: int, x2: int, y2: int):
    time.sleep(0.08)
    img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
    filepath = _output_path()
    img.save(filepath)
    pyperclip.copy(filepath)
    _notify("Screenshot saved — path copied", filepath)


def _notify(title_or_msg: str, detail: str = ""):
    try:
        import plyer
        plyer.notification.notify(
            title=APP_NAME,
            message=title_or_msg,
            app_name=APP_NAME,
            timeout=3,
        )
    except Exception:
        # Silent fallback — path is on clipboard, user will see it on paste
        pass


def open_screenshots_folder(icon=None, item=None):
    folder = config["output_dir"]
    os.makedirs(folder, exist_ok=True)
    os.startfile(folder)


def capture_now(icon=None, item=None):
    main_queue.put(show_overlay)


# ── selection overlay ─────────────────────────────────────────────────────────

def show_overlay():
    global _overlay_open
    if _overlay_open:
        return
    _overlay_open = True

    bg = ImageGrab.grab()
    dark_bg = ImageEnhance.Brightness(bg).enhance(0.42)

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

    # Top hint bar
    canvas.create_rectangle(0, 0, sw, 48, fill="#0f172a", outline="")
    canvas.create_text(
        sw // 2, 24,
        text="Drag to select region  •  Esc to cancel  •  Path will be copied for Claude Code",
        fill="#94a3b8",
        font=("Segoe UI", 11),
    )

    state: dict = {"sx": None, "sy": None, "rect": None, "corners": [], "dim": None}

    def _clear_selection():
        if state["rect"]:
            canvas.delete(state["rect"])
        for c in state["corners"]:
            canvas.delete(c)
        if state["dim"]:
            canvas.delete(state["dim"])
        state["rect"] = state["dim"] = None
        state["corners"] = []

    def on_press(e):
        state["sx"], state["sy"] = e.x, e.y
        _clear_selection()
        state["rect"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y,
            outline="#38bdf8", width=2, fill="#38bdf808",
        )
        state["dim"] = canvas.create_text(
            e.x + 10, e.y + 14, text="0 × 0",
            fill="#f8fafc", font=("Segoe UI", 9, "bold"), anchor="nw",
        )

    def on_drag(e):
        if state["sx"] is None:
            return
        sx, sy = state["sx"], state["sy"]
        canvas.coords(state["rect"], sx, sy, e.x, e.y)
        w, h = abs(e.x - sx), abs(e.y - sy)
        canvas.itemconfig(state["dim"], text=f"{w} × {h}")
        # Keep label near cursor but inside screen
        lx = min(e.x + 10, sw - 60)
        ly = min(e.y + 14, sh - 20)
        canvas.coords(state["dim"], lx, ly)

    def on_release(e):
        global _overlay_open
        if state["sx"] is None:
            return
        x1 = min(state["sx"], e.x)
        y1 = min(state["sy"], e.y)
        x2 = max(state["sx"], e.x)
        y2 = max(state["sy"], e.y)
        _overlay_open = False
        win.destroy()
        if x2 - x1 > 5 and y2 - y1 > 5:
            threading.Thread(
                target=save_screenshot, args=(x1, y1, x2, y2), daemon=True
            ).start()

    def on_cancel(e):
        global _overlay_open
        _overlay_open = False
        win.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    win.bind("<Escape>", on_cancel)
    win.focus_force()


# ── settings window ───────────────────────────────────────────────────────────

def open_settings(icon=None, item=None):
    main_queue.put(_show_settings)


def _show_settings():
    win = tk.Toplevel(tk_root)
    win.title(f"CaptThat {VERSION} — Settings")
    win.geometry("420x300")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.focus_force()

    frame = ttk.Frame(win, padding=20)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text="Hotkey:").grid(row=0, column=0, sticky="w", pady=7)
    hotkey_var = tk.StringVar(value=config["hotkey"])
    ttk.Entry(frame, textvariable=hotkey_var, width=28).grid(row=0, column=1, padx=10)

    ttk.Label(frame, text="Output folder:").grid(row=1, column=0, sticky="w", pady=7)
    dir_var = tk.StringVar(value=config["output_dir"])
    ttk.Entry(frame, textvariable=dir_var, width=28).grid(row=1, column=1, padx=10)

    ttk.Label(frame, text="Filenames:").grid(row=2, column=0, sticky="w", pady=7)
    unique_var = tk.BooleanVar(value=config.get("unique_names", False))
    mode_frame = ttk.Frame(frame)
    mode_frame.grid(row=2, column=1, sticky="w", padx=10)
    ttk.Radiobutton(mode_frame, text="Always latest.png", variable=unique_var, value=False).pack(anchor="w")
    ttk.Radiobutton(mode_frame, text="Timestamped  (20250508_142301.png)", variable=unique_var, value=True).pack(anchor="w")

    startup_var = tk.BooleanVar(value=_startup_enabled())
    ttk.Checkbutton(frame, text="Start with Windows", variable=startup_var).grid(
        row=3, column=0, columnspan=2, sticky="w", pady=10
    )

    ttk.Label(frame, text="Hotkey names: print screen, f9, ctrl+shift+s …",
              foreground="#888").grid(row=4, column=0, columnspan=2, sticky="w")

    def save():
        config["hotkey"] = hotkey_var.get().strip()
        config["output_dir"] = dir_var.get().strip()
        config["unique_names"] = unique_var.get()
        set_startup(startup_var.get())
        save_config()
        register_hotkey()
        win.destroy()

    btn = ttk.Frame(frame)
    btn.grid(row=5, column=0, columnspan=2, pady=16)
    ttk.Button(btn, text="Save", command=save).pack(side=tk.LEFT, padx=4)
    ttk.Button(btn, text="Cancel", command=win.destroy).pack(side=tk.LEFT, padx=4)


# ── tray icon image ───────────────────────────────────────────────────────────

def _make_icon(size: int = 64) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Camera body
    pad = int(s * 0.08)
    top = int(s * 0.28)
    bot = int(s * 0.88)
    d.rounded_rectangle(
        [pad, top, s - pad, bot],
        radius=int(s * 0.12),
        fill="#1e293b",
        outline="#475569",
        width=max(1, int(s * 0.03)),
    )

    # Viewfinder bump
    vw = int(s * 0.26)
    d.rounded_rectangle(
        [s // 2 - vw // 2, int(s * 0.14), s // 2 + vw // 2, int(s * 0.30)],
        radius=int(s * 0.05),
        fill="#1e293b",
        outline="#475569",
        width=max(1, int(s * 0.03)),
    )

    # Flash (orange accent — nod to Claude)
    fx = int(s * 0.72)
    fy = int(s * 0.35)
    fr = int(s * 0.09)
    d.ellipse([fx - fr, fy - fr, fx + fr, fy + fr], fill="#f97316")

    # Lens outer
    cx, cy = s // 2, int(s * 0.58)
    lr = int(s * 0.24)
    d.ellipse([cx - lr, cy - lr, cx + lr, cy + lr], fill="#0ea5e9", outline="#38bdf8",
              width=max(1, int(s * 0.03)))
    # Lens inner
    lr2 = int(s * 0.14)
    d.ellipse([cx - lr2, cy - lr2, cx + lr2, cy + lr2], fill="#0284c7")
    # Lens highlight
    hr = int(s * 0.06)
    hx, hy = cx - int(s * 0.08), cy - int(s * 0.08)
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill="#bae6fd")

    return img


def save_icon_file(path: str):
    """Save multi-resolution .ico for PyInstaller."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_make_icon(s) for s in sizes]
    images[0].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:])


# ── tray menu ─────────────────────────────────────────────────────────────────

def _startup_menu_label(icon=None, item=None):
    return "✓ Start with Windows" if _startup_enabled() else "  Start with Windows"


def _toggle_startup(icon=None, item=None):
    set_startup(not _startup_enabled())


def _run_tray():
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("Capture Now", capture_now, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Screenshots Folder", open_screenshots_folder),
        pystray.MenuItem("Settings", open_settings),
        pystray.MenuItem(_startup_menu_label, _toggle_startup),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda icon, item: os._exit(0)),
    )
    tray_icon = pystray.Icon(APP_NAME, _make_icon(64), APP_NAME, menu)
    tray_icon.run()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global tk_root

    if not _ensure_single_instance():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "CaptThat is already running.\nFind it in the system tray.",
            APP_NAME,
            0x40,
        )
        sys.exit(0)

    load_config()

    # Sync startup flag with actual registry state on launch
    config["start_with_windows"] = _startup_enabled()

    tk_root = tk.Tk()
    tk_root.withdraw()

    register_hotkey()
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
