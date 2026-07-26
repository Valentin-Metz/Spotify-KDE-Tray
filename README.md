# Spotify KDE Tray Proxy

Makes the Spotify Flatpak's tray icon **left-click to collapse/restore the window** on KDE Plasma (Wayland), instead of opening its context menu. The tray icon shows the current track's album cover; hovering shows "Spotify | track — artist". Middle-click toggles play/pause. Right-click opens a small media-player controller.

## Why this exists

Spotify's Linux Flatpak registers its tray icon as an **Ayatana StatusNotifierItem (SNI)** that does **not** implement the `Activate()` method. KDE Plasma's system tray, finding no `Activate` handler, falls back to opening Spotify's context menu on left-click, instead of toggling the window. The only way to actually show/hide the window is to open that menu and click "Show Spotify" / "Minimize to Tray".

This proxy fixes that by registering its own KDE-spec SNI. It **implements `Activate`** (left-click → toggle window) and `SecondaryActivate` (middle-click → play/pause), and serves its own DBus menu on right-click. Spotify's own Ayatana SNI is suppressed via a Flatpak override so only the proxy's icon shows.

## Mouse actions

| Button | Action |
|---|---|
| Left-click | Toggle (minimize to tray / restore to current virtual desktop) |
| Middle-click | Play / Pause the current track |
| Right-click | Open the media controller menu |

## How it works

Two components cooperate over D-Bus:

1. **`spotify-tray-proxy.py`** — A Python D-Bus daemon that registers as a `StatusNotifierItem` (well-known bus name `org.feuerdevil.SpotifyTray`, object path `/StatusNotifierItem`, `Id=spotify-client`, `Category=ApplicationStatus`). On left-click it queues a toggle command to the KWin script; on middle-click it calls MPRIS `PlayPause`. Its right-click `com.canonical.dbusmenu` menu (at `/StatusNotifierItem/Menu`) is a media controller driven by MPRIS (`org.mpris.MediaPlayer2.spotify`): now-playing (with `view-media-playlist` icon), previous/play-pause/next, shuffle (`media-playlist-shuffle-symbolic`), loop (`media-playlist-repeat-symbolic`), toggle tray, quit. The tray icon itself is the current track's album cover, fetched from `mpris:artUrl`, resized to 64×64, and served as the SNI `IconPixmap` (ARGB32, big-endian per the KDE SNI spec — Plasma applies `ntohl()` then reads as `QImage::Format_ARGB32`). The `ToolTip` property carries `title="Spotify"` and `subtitle="track — artist"`, updated live via `NewToolTip`/`PropertiesChanged` on metadata change.

2. **KWin script `spotify-toggle`** — A Plasma 6 KWin JavaScript package that polls the daemon every 50 ms via a `QTimer` (setInterval is unavailable in KWin's QtScript engine). The daemon cannot manipulate Wayland-native windows directly (KWin has no D-Bus method to minimize/restore by window class), so this script runs *inside* KWin where it has `workspace.windowList()` access. It reports the Spotify window's live state (visible / minimized / absent) and pulls queued commands to set `window.minimized` and `window.skipTaskbar` (tray-only when minimized), moving the window to the current virtual desktop on restore.

### Bridge protocol

The KWin script calls the daemon on `org.feuerdevil.SpotifyToggle` at `/SpotifyToggle`:

| Method | Signature | Purpose |
|---|---|---|
| `ReportState` | `(tag:s, state:s) → ()` | Script pushes current window state each poll |
| `GetCommand` | `() → s` | Script pulls a pending command (`minimize`/`restore`/`toggle`/`none`) |
| `CommandDone` | `(result:s) → ()` | Script reports the action it took |

### Why the polling bridge (and the small delay)

On Plasma 6 Wayland, KWin's scripting `loadScript`/`start` D-Bus path does **not** execute ad-hoc scripts in a running session, and KWin has no D-Bus method to minimize a specific window. The only reliable, no-recompile mechanism is a *persistent* KWin script (installed package, auto-run at KWin start) that polls the daemon. At 50 ms this adds up to ~50 ms of latency on click — imperceptible in practice. Installed KWin scripts auto-run reliably on login; hot-reloading them in a running session is flaky (the `QTimer` often dies).

## Requirements

- KDE Plasma 6 (Wayland) — tested on Plasma 6.7 / Fedora 44
- Spotify Flatpak (`com.spotify.Client`)
- `python3-dbus`, `python3-gobject` (PyGObject) — present by default on KDE installs
- `python3-pillow` (PIL) — for album art resizing; `pip install pillow` or `dnf install python3-pillow`
- `kdotool`/`wmctrl` are **not** required (Spotify is Wayland-native, invisible to X11 tools)

## Install

Copy the files to their target locations:

```sh
# The tray proxy daemon
cp spotify-tray-proxy.py ~/.local/bin/spotify-tray-proxy.py
chmod +x ~/.local/bin/spotify-tray-proxy.py

# Wrapper that launches Spotify + the proxy together (used by the launcher overlay)
cp bin/spotify-launch-with-proxy.sh ~/.local/bin/spotify-launch-with-proxy.sh
chmod +x ~/.local/bin/spotify-launch-with-proxy.sh

# Start the proxy on login
cp autostart/spotify-tray-proxy.desktop ~/.config/autostart/

# Replace the Spotify launcher so launching Spotify also starts the proxy
cp applications/com.spotify.Client.desktop ~/.local/share/applications/

# KWin script package (window minimize/restore bridge)
mkdir -p ~/.local/share/kwin/scripts/spotify-toggle/contents/code
cp kwin-script/spotify-toggle/metadata.json ~/.local/share/kwin/scripts/spotify-toggle/
cp kwin-script/spotify-toggle/contents/code/main.js ~/.local/share/kwin/scripts/spotify-toggle/contents/code/

# Enable the KWin script
kwriteconfig6 --file kwinrc --group Plugins --key spotify-toggleEnabled true

# Suppress Spotify's own tray icon (Plasma does not dedup by Id)
flatpak override --user --no-talk-name=org.kde.StatusNotifierWatcher com.spotify.Client
```

Then **log out and back in** (or reboot). KWin only auto-runs enabled script packages at startup; hot-reloading a script in a running session is unreliable.

After login, launch Spotify normally. A single Spotify tray icon should appear; left-clicking it toggle-minimizes the window (hidden from the taskbar while minimized), middle-click plays/pauses, and right-click opens the media controller.

## Files

| File | Target | Purpose |
|---|---|---|
| `spotify-tray-proxy.py` | `~/.local/bin/spotify-tray-proxy.py` | Tray SNI daemon + DBus menu (MPRIS-driven) |
| `kwin-script/spotify-toggle/` | `~/.local/share/kwin/scripts/spotify-toggle/` | KWin script: window toggle/minimize/restore |
| `autostart/spotify-tray-proxy.desktop` | `~/.config/autostart/` | Launch the daemon at login |
| `applications/com.spotify.Client.desktop` | `~/.local/share/applications/` | Overlay: launching Spotify also starts the proxy |
| `bin/spotify-launch-with-proxy.sh` | `~/.local/bin/spotify-launch-with-proxy.sh` | Shell wrapper used by the launcher overlay |

## Development

### Editing the daemon

Edit `spotify-tray-proxy.py` in the repo, then copy out and restart:

```sh
cp spotify-tray-proxy.py ~/.local/bin/spotify-tray-proxy.py
# Kill the old instance (pkill sends SIGTERM, which the daemon handles
# cleanly now — it releases its D-Bus bus names and exits). A
# single-instance guard also prevents duplicate ghost processes.
pkill -f spotify-tray-proxy.py
# Relaunch (or log out/in; autostart brings it up at session start):
python3 ~/.local/bin/spotify-tray-proxy.py &

The daemon does not need KWin or a relog; a process restart picks up changes immediately. Check the logs via `journalctl --user` or run it in a terminal for stderr.

### Editing the KWin script

Edit `kwin-script/spotify-toggle/contents/code/main.js`, then copy out and attempt a hot-reload (flaky — the `QTimer` that drives polling often dies on reload):

```sh
cp kwin-script/spotify-toggle/contents/code/main.js \
   ~/.local/share/kwin/scripts/spotify-toggle/contents/code/main.js
gdbus call -e -d org.kde.KWin -o /Scripting \
   -m org.kde.kwin.Scripting.unloadScript spotify-toggle
sleep 1
gdbus call -e -d org.kde.KWin -o /Scripting \
   -m org.kde.kwin.Scripting.start
```

If the hot-reload doesn't take (the daemon stops receiving `ReportState` polls — check `journalctl --user -t kwin_wayland | grep spotify-toggle`), **relog** to get a clean auto-load.

### Diagnostics

- KWin script errors: `journalctl --user -t kwin_wayland --since "5 min ago" | grep spotify-toggle`
- Daemon owns its bus names: `gdbus call -e -d org.freedesktop.DBus -o /org/freedesktop/DBus -m org.freedesktop.DBus.NameHasOwner org.feuerdevil.SpotifyTray` (and `org.feuerdevil.SpotifyToggle`)
- Registered tray icons: `gdbus call -e -d org.kde.StatusNotifierWatcher -o /StatusNotifierWatcher -m org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems`
- Menu layout: call `com.canonical.dbusmenu.GetLayout` on `org.feuerdevil.SpotifyTray` at `/StatusNotifierItem/Menu`

### Key APIs

- **KWin scripting (Plasma 6):** `window.minimized` is a settable property (`setMinimized()` does **not** exist on `XdgToplevelWindow`); `window.skipTaskbar` hides from the taskbar; `window.desktops = [workspace.currentDesktop]` moves to a virtual desktop; `QTimer` is the timer API (no `setInterval`); `callDBus` needs a no-op callback to reliably deliver fire-and-forget calls.
- **dbusmenu:** emit `LayoutUpdated(revision, parent)` only when the layout actually changes — re-emitting on every `GetLayout`/`AboutToShow` call causes Plasma to re-render the popup in a loop, which flickers and swallows clicks. `AboutToShow` should return `False` (no forced change). `Event("clicked")` should not bump the revision (MPRIS `PropertiesChanged` drives updates).
- **MPRIS:** `org.mpris.MediaPlayer2.spotify` at `/org/mpris/MediaPlayer2` — `Raise()` (show window), `Quit()`, `Player.PlayPause`/`Next`/`Previous`, settable `Shuffle` and `LoopStatus`. Filter `PropertiesChanged` to only the keys that affect the menu (`PlaybackStatus`, `Metadata`, `Shuffle`, `LoopStatus`, `Volume`) — Spotify emits `Position` updates frequently which would otherwise flicker.

## Notes

- **Single icon:** Spotify's Flatpak normally registers its own Ayatana SNI (the one lacking `Activate`). The `flatpak override --no-talk-name=org.kde.StatusNotifierWatcher` step revokes Spotify's session-bus access to the SNI watcher, so it can no longer register its icon — leaving only the proxy's. Plasma does not dedup by `Id`; the override is required to avoid a duplicate.
- **Tray-only minimize:** while minimized, the window is hidden from the taskbar (`skipTaskbar`), mirroring Spotify's own "Minimize to Tray" semantics. On restore, `skipTaskbar` is cleared and the window moves to the current virtual desktop.
- **Loop cycle order** matches the Spotify UI: Off → Album → Track.
- **MPRIS `Raise()`** is the fallback when the daemon sees no window state (e.g. before the KWin script's first poll arrives).
- **Tray icon position:** Plasma sorts tray icons by `Category` (fixed enum order: `UnknownCategory` → `ApplicationStatus` → `Communications` → ...) then alphabetically by `Title` within category. `XAyatanaOrderingIndex` is **ignored** by Plasma 6. Our `Category=ApplicationStatus` and `Title="Spotify"` pin the position. If `Category` is omitted, the icon falls into `UnknownCategory` and jumps to the first tray slot.

## Known limitations
- Album art is served only as the tray `IconPixmap` (cover art as the icon), not inside the right-click menu — Plasma's `libdbusmenuqt` renderer is a plain `QMenu` with no cover-art banner concept. The now-playing row uses a `view-media-playlist` icon instead.
- KWin scripts cannot be reliably hot-reloaded in a running session; changes to `main.js` may require a relog to take effect.
