# Spotify KDE Tray Proxy

Makes the Spotify Flatpak's tray icon **left-click to collapse/restore the window** on KDE Plasma (Wayland), instead of opening its context menu — plus a small media-player controller on right-click.

## Why this exists

Spotify's Linux Flatpak registers its tray icon as an **Ayatana StatusNotifierItem (SNI)** that does **not** implement the `Activate()` method. KDE Plasma's system tray, finding no `Activate` handler, falls back to opening Spotify's context menu on left-click, instead of toggling the window. Secondary-activate (middle-click) is also a no-op. The only way to actually show/hide the window is to open that menu and click "Show Spotify" / "Minimize to Tray".

This proxy fixes that by registering its own KDE-spec SNI with the same `Id` (`spotify-client`), so it replaces Spotify's icon in the tray. It **implements `Activate`** (left-click) and `SecondaryActivate` (middle-click) to toggle the window, and serves its own DBus menu on right-click.

## How it works

Two components cooperate over D-Bus:

1. **`spotify-tray-proxy.py`** — A Python D-Bus daemon that registers as a `StatusNotifierItem` with `Id=spotify-client` (last-registered wins in Plasma, so it replaces Spotify's own icon). On left/middle-click it queues a toggle command. Its right-click `com.canonical.dbusmenu` menu is a media controller driven by MPRIS (`org.mpris.MediaPlayer2.spotify`): now-playing, previous/play-pause/next, shuffle, loop, show/hide, quit.

2. **KWin script `spotify-toggle`** — A Plasma 6 KWin JavaScript package that polls the daemon every 100 ms. The daemon cannot manipulate Wayland-native windows directly (no KWin D-Bus method for minimize/restore by class), so this script runs *inside* KWin where it has `workspace.windowList()` access. It reports the Spotify window's live state (visible / minimized / absent) and pulls queued commands to `window.minimized = true/false` and `window.skipTaskbar` (tray-only when minimized), moving the window to the current virtual desktop on restore.

### Why the polling bridge (and the small delay)

On Plasma 6 Wayland, KWin's scripting `loadScript`/`start` D-Bus path does **not** execute ad-hoc scripts in a running session, and KWin has no D-Bus method to minimize a specific window. The only reliable, no-recompile mechanism is a *persistent* KWin script (installed package, auto-run at KWin start) that polls the daemon. This adds up to ~100 ms of latency on click — imperceptible in practice. Installed KWin scripts auto-run reliably on login; hot-reloading them in a running session is flaky.

## Requirements

- KDE Plasma 6 (Wayland) — tested on Plasma 6.7 / Fedora 44
- Spotify Flatpak (`com.spotify.Client`)
- `python3-dbus`, `python3-gobject` (PyGObject) — present by default on KDE installs
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

# Start it on login
cp autostart/spotify-tray-proxy.desktop ~/.config/autostart/

# Replace the Spotify launcher so launching Spotify also starts the proxy
cp applications/com.spotify.Client.desktop ~/.local/share/applications/

# KWin script package (window minimize/restore bridge)
mkdir -p ~/.local/share/kwin/scripts/spotify-toggle/contents/code
cp kwin-script/spotify-toggle/metadata.json ~/.local/share/kwin/scripts/spotify-toggle/
cp kwin-script/spotify-toggle/contents/code/main.js ~/.local/share/kwin/scripts/spotify-toggle/contents/code/

# Enable the KWin script
kwriteconfig6 --file kwinrc --group Plugins --key spotify-toggleEnabled true
```

Then **log out and back in** (or reboot). KWin only auto-runs enabled script packages at startup; hot-reloading a script in a running session is unreliable.

After login, launch Spotify normally. A single Spotify tray icon should appear; left-clicking it toggle-minimizes the window (hidden from the taskbar while minimized), and right-click opens the media controller.

## Files

| File | Target | Purpose |
|---|---|---|
| `spotify-tray-proxy.py` | `~/.local/bin/` | Tray SNI daemon + DBus menu (MPRIS-driven) |
| `kwin-script/spotify-toggle/` | `~/.local/share/kwin/scripts/spotify-toggle/` | KWin script: window toggle/minimize/restore |
| `autostart/spotify-tray-proxy.desktop` | `~/.config/autostart/` | Launch the daemon at login |
| `applications/com.spotify.Client.desktop` | `~/.local/share/applications/` | Overlay: launching Spotify also starts the proxy |
| `bin/spotify-launch-with-proxy.sh` | `~/.local/bin/` | Shell wrapper used by the launcher overlay |

## Notes

- **Dedup:** the proxy registers with `Id=spotify-client`, matching Spotify's own Ayatana SNI. Plasma keys tray items by `Id` (last-registered wins), so only the proxy's icon shows.
- **Tray-only minimize:** while minimized, the window is hidden from the taskbar (`skipTaskbar`), mirroring Spotify's own "Minimize to Tray" semantics. On restore, `skipTaskbar` is cleared and the window moves to the current virtual desktop.
- **Loop cycle order** matches the Spotify UI: Off → Album → Track.
- **MPRIS `Raise()`** is the fallback when the daemon sees no window state (e.g. before the KWin script's first poll arrives).

## Known limitations

- Album art in the menu (`icon-data`) is not yet rendering — the dbusmenu RGBA struct format needs verification. The now-playing row carries the slot for it.
- KWin scripts cannot be reliably hot-reloaded in a running session; changes to `main.js` require a relog to take effect.
