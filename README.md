# CaptThat

**Screenshot tray app built for Claude Code on Windows.**

Claude Code can't read raw clipboard image data — it needs a file path. CaptThat solves this: press a hotkey, drag to select a region, and the file path lands on your clipboard so you paste it straight into Claude Code with `Ctrl+V`.

## How it works with Claude Code

```
1. Press PrtScn
2. Drag to select the region you want Claude to see
3. Switch to Claude Code
4. Ctrl+V  →  C:\Screenshots\latest.png
5. Claude reads the file and responds
```

That's the whole workflow. No manual saving, no hunting for file paths.

## Quick start

**Option A — Python (no build needed)**

```bat
pip install -r requirements.txt
python capthat.py
```

**Option B — Standalone exe**

```bat
pip install -r requirements.txt
build.bat          # creates dist\CaptThat.exe
```

Drop `CaptThat.exe` anywhere and run it. No installer required.

## Features

- **Global hotkey** — works even when Claude Code is in focus (default: `PrtScn`)
- **Region selection overlay** — semi-transparent freeze-frame, crosshair cursor, live dimensions while dragging
- **Path to clipboard** — saves `C:\Screenshots\latest.png` and copies the path, not the image
- **System tray** — lives quietly in the taskbar; right-click for options
- **Capture Now** — trigger a capture from the tray menu (no hotkey needed)
- **Open folder** — jump straight to your screenshots
- **Start with Windows** — one toggle in the tray menu, writes the registry key
- **Single instance** — re-running shows a reminder instead of opening twice
- **Settings** — change hotkey, output folder, and filename mode (always `latest.png` or timestamped)

## Tray menu

| Item | Action |
|------|--------|
| Capture Now | Open the selection overlay |
| Open Screenshots Folder | Open output folder in Explorer |
| Settings | Change hotkey, folder, filename mode |
| Start with Windows | Toggle auto-start on login |
| Exit | Quit |

## Settings

| Setting | Default | Notes |
|---------|---------|-------|
| Hotkey | `print screen` | Any key name the `keyboard` library accepts (e.g. `f9`, `ctrl+shift+s`) |
| Output folder | `C:\Screenshots` | Created automatically if it doesn't exist |
| Filename | `latest.png` (always overwrite) | Switch to timestamped for a permanent history |
| Start with Windows | Off | Writes `HKCU\...\Run` registry key |

## Dependencies

```
pillow       image capture and manipulation
keyboard     global hotkey registration
pyperclip    clipboard access
pystray      Windows system tray
plyer        toast notifications
```

Install all at once:

```bat
pip install -r requirements.txt
```

## Build a standalone exe

Requires PyInstaller (`pip install pyinstaller`):

```bat
build.bat
```

Output: `dist\CaptThat.exe` — single portable file, no Python required on the target machine.

## Tips

- **Always-overwrite mode** (`latest.png`) is ideal for Claude Code — the path never changes, so you can reference it repeatedly in a session.
- **Timestamped mode** builds a permanent screenshot history — useful for bug reports or design reviews.
- The overlay hint bar reminds you the path is for Claude Code, not the image itself.
- To suppress the Windows default PrtScn behavior (saves to OneDrive), CaptThat uses `suppress=True` on the hotkey — Windows won't see the keypress.

## License

MIT
