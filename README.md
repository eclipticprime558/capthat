# CaptThat

**Screenshot tray app built for Claude Code on Windows.**

Claude Code can't read raw clipboard image data — it needs a file path. CaptThat solves this: press a hotkey, drag a region (or capture a full screen / active window), and the file path lands on your clipboard so you paste it straight into Claude Code with `Ctrl+V`.

## Claude Code workflow

```
1. Press PrtScn
2. Drag to select the region you want Claude to see
3. Switch to Claude Code
4. Ctrl+V  →  C:\Screenshots\latest.png
5. Claude reads the file and responds
```

No manual saving, no hunting for file paths. The path is always the same (`latest.png` by default) so you can reference it repeatedly in a session.

## Quick start

**Python (no build needed)**
```bat
pip install -r requirements.txt
python capthat.py
```

**Standalone exe**
```bat
pip install -r requirements.txt
build.bat        ← creates dist\CaptThat.exe
```

Drop `CaptThat.exe` anywhere and run it — no installer required.

## Capture modes

| Mode | Default hotkey | Description |
|------|---------------|-------------|
| Region | `PrtScn` | Drag to select any area |
| Full screen | `Ctrl+PrtScn` | Entire screen instantly |
| Active window | `Alt+PrtScn` | Foreground window bounds |
| Repeat last | `Shift+PrtScn` | Re-capture the previous region |

All hotkeys are configurable. Leave one blank to disable it.

## Features

### Capture
- **Capture delay** — 1–10 second countdown before capture, so you can set up tooltips, menus, or hover states
- **Include cursor** — draws the mouse cursor into the captured image
- **Magnifier** — zoomed pixel-perfect view follows your cursor during region selection; shows exact coordinates

### Output
- **Formats** — PNG, JPEG, or WebP
- **Quality slider** — for JPEG and WebP
- **Always-overwrite** — saves as `latest.png` every time (ideal for Claude Code — the path never changes)
- **Unique filenames** — timestamp or custom pattern: `{date}`, `{time}`, `{ms}`, `{counter}`, `{title}`

### After capture
- **Path to clipboard** — always copies the file path (paste into Claude Code)
- **Also copy image** — optionally also copies the raw image data (for pasting into other apps)
- **Preview thumbnail** — small pop-up in the bottom-right corner; click to open the file, right-click for Settings
- **Auto-open** — immediately opens the file in your default image viewer

### Overlay
- **Adjustable opacity** — 10%–90% overlay darkness
- **Crosshair color** — color picker for the selection rectangle

### System
- **Recent captures** — tray menu shows last 5 captures; click any to copy its path again
- **History size** — keep 1–50 paths in the recent list
- **Start with Windows** — one toggle writes the registry Run key
- **Single instance** — re-running pops a reminder instead of opening twice

## Tray menu

```
Capture Region           ← default double-click
Capture Full Screen
Capture Active Window
Repeat Last Capture
────────────────────
latest.png
20250508_142301.png
…
────────────────────
Open Screenshots Folder
────────────────────
Settings
Start with Windows
────────────────────
Exit
```

## Settings tabs

| Tab | Controls |
|-----|----------|
| **Hotkeys** | All 4 hotkeys (region, full, window, repeat) |
| **Output** | Folder, format, quality, filename mode, pattern, history size |
| **Capture** | Delay, include cursor, show magnifier, overlay opacity, crosshair color |
| **After Capture** | Copy image, show preview, preview duration, auto-open |
| **System** | Start with Windows, version info |

## Dependencies

```
pillow       image capture, processing, icon rendering
keyboard     global hotkey registration
pyperclip    clipboard (path string)
pystray      Windows system tray
plyer        toast notifications
```

```bat
pip install -r requirements.txt
```

## Build standalone exe

```bat
build.bat
```

Runs PyInstaller with `CaptThat.spec`, outputs `dist\CaptThat.exe` — single portable file, no Python needed on the target machine.

## Tips

- **Always-overwrite mode** is best for Claude Code: the path `C:\Screenshots\latest.png` never changes, so you can say "look at the same file" repeatedly in a session.
- Use **capture delay** to screenshot tooltips, context menus, or hover states that disappear when you move the mouse.
- **Shift+PrtScn** (repeat last) lets you re-capture the same region after changing something on screen — no re-selecting.
- With **copy image to clipboard** on, one screenshot gives you both the path (for Claude Code) and the image data (for pasting into Slack, Word, etc.).
- The **magnifier** uses nearest-neighbor zoom so you can see individual pixels — useful for aligning edges precisely.
- **Right-click the preview thumbnail** to open Settings without going to the tray.

## Changelog

### v1.2.0
- **Icon redesign** — 8× supersampled rendering, 3-ring lens with wide gaps (readable at 16 px), richer gradient; multi-size ICO (8 sizes, 16–256 px including 96 px for HiDPI); icon file stored in `%AppData%\CaptThat\` so it never lands next to the EXE
- **Tray right-click fixed** — removed callable submenu that broke the Windows tray context menu; recent captures now listed inline
- **Preview right-click** — right-clicking the post-capture thumbnail opens a context menu with "Open file" and "Settings…"
- **Overlay guard** — `<Destroy>` binding on the selection overlay resets the open-flag if the window is closed externally, preventing a stuck state where PrtScn appeared to do nothing
- **Dark title bar** — Settings window now uses the Windows dark-mode title bar API

### v1.1.0
- Multiple capture modes (region, fullscreen, active window, repeat last)
- Configurable hotkeys, output formats, capture delay, magnifier, preview thumbnail
- Full settings UI with tabbed panels and toggle switches
- Recent captures history in tray menu
- Start with Windows toggle

### v1.0.0
- Initial release — region capture, path to clipboard, system tray

## License

MIT
