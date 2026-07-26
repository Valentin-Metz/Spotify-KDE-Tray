#!/usr/bin/env python3
"""
Spotify Tray Proxy — a KDE StatusNotifierItem that replaces Spotify's Ayatana
tray icon and turns left/middle-click into an instant window toggle.

Right-click exposes a small media-player controller (Now Playing, transport,
shuffle/loop, show/minimize, quit) driven by MPRIS.

Root cause it fixes: Spotify's Flatpak registers an Ayatana SNI that does NOT
implement Activate(); KDE Plasma's system tray therefore falls back to opening
Spotify's context menu on left-click, instead of toggling the window.
"""

import os
import sys
import time
import urllib.request
import threading
import tempfile

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SPOTIFY_MPRIS_NAME = "org.mpris.MediaPlayer2.spotify"
SPOTIFY_MPRIS_PATH = "/org/mpris/MediaPlayer2"
SPOTIFY_RESOURCE_CLASS = "spotify"

SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"

PROPS_IFACE = "org.freedesktop.DBus.Properties"
DBUSMENU_IFACE = "com.canonical.dbusmenu"

ICON_NAME = "com.spotify.Client-symbolic"

# dbusmenu item type constants
TYPE_SEPARATOR = "separator"

# dbusmenu property keys
P_LABEL = "label"
P_TYPE = "type"
P_ICON_NAME = "icon-name"
P_ICON_DATA = "icon-data"
P_TOGGLE_TYPE = "toggle-type"
P_TOGGLE_STATE = "toggle-state"
P_ENABLED = "enabled"
P_CHILDREN_DISPLAY = "children-display"

# Menu item ids
M_ROOT = 0
M_ART = 1
M_NOWPLAYING = 2
M_PREV = 4
M_PLAYPAUSE = 5
M_NEXT = 6
M_SEP2 = 7
M_SHUFFLE = 8
M_LOOP = 9
M_SEP3 = 10
M_SHOWHIDE = 11
M_QUIT = 12

LOOP_LABELS = {
    "None": "Loop: Off",
    "Track": "Loop: Track",
    "Playlist": "Loop: Album",
}

SPOTIFY_TOGGLE_BUS_NAME = "org.feuerdevil.SpotifyToggle"
TRAY_BUS_NAME = "org.feuerdevil.SpotifyTray"

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
def log(msg):
    print(f"[spotify-tray-proxy] {msg}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# MPRIS helpers
# ----------------------------------------------------------------------------
def _get_mpris():
    bus = dbus.SessionBus()
    return bus.get_object(SPOTIFY_MPRIS_NAME, SPOTIFY_MPRIS_PATH)


def mpris_get_all():
    """Return (root_props, player_props) dicts, or (None, None) if absent."""
    try:
        obj = _get_mpris()
        root = dbus.Interface(obj, PROPS_IFACE).GetAll(
            "org.mpris.MediaPlayer2")
        player = dbus.Interface(obj, PROPS_IFACE).GetAll(
            "org.mpris.MediaPlayer2.Player")
        return dict(root), dict(player)
    except dbus.exceptions.DBusException:
        return None, None


def mpris_call(method, *args):
    try:
        obj = _get_mpris()
        getattr(dbus.Interface(obj, "org.mpris.MediaPlayer2.Player"), method)(*args)
    except dbus.exceptions.DBusException as e:
        log(f"mpris_call {method} failed: {e}")


def mpris_raise():
    try:
        dbus.Interface(_get_mpris(), "org.mpris.MediaPlayer2").Raise()
    except dbus.exceptions.DBusException as e:
        log(f"mpris Raise failed: {e}")


def mpris_quit():
    try:
        dbus.Interface(_get_mpris(), "org.mpris.MediaPlayer2").Quit()
    except dbus.exceptions.DBusException as e:
        log(f"mpris Quit failed: {e}")


def mpris_set_prop(prop, value):
    try:
        dbus.Interface(_get_mpris(), PROPS_IFACE).Set(
            "org.mpris.MediaPlayer2.Player", prop, value)
    except dbus.exceptions.DBusException as e:
        log(f"mpris set {prop} failed: {e}")


# ----------------------------------------------------------------------------
# Album-art caching → raw RGBA bytes for dbusmenu icon-data
# ----------------------------------------------------------------------------
_ART_CACHE = {}


def fetch_art_argb32(url, size=64):
    """Fetch album art and return (w, h, bytes) in Qt ARGB32 (little-endian
    BGRA) form, suitable for the SNI IconPixmap property (a(iiay))."""
    if url in _ART_CACHE:
        return _ART_CACHE[url]
    try:
        from PIL import Image
        import io
    except Exception:
        _ART_CACHE[url] = None
        return None
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            data = r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img = img.resize((size, size), Image.LANCZOS)
        # SNI IconPixmap bytes are network-order (big-endian) ARGB:
        # Plasma applies ntohl() then reads as QImage::Format_ARGB32,
        # so we must emit [A, R, G, B] per pixel.
        rgba = img.tobytes("raw", "RGBA")
        argb = bytearray(len(rgba))
        argb[0::4] = rgba[3::4]   # A
        argb[1::4] = rgba[0::4]   # R
        argb[2::4] = rgba[1::4]   # G
        argb[3::4] = rgba[2::4]   # B
        _ART_CACHE[url] = (size, size, bytes(argb))
        return _ART_CACHE[url]
    except Exception as e:
        log(f"art fetch {url} failed: {e}")
        _ART_CACHE[url] = None
        return None



# ----------------------------------------------------------------------------
# Window management: the daemon exposes a D-Bus object that the installed
# KWin script (spotify-toggle) polls. The daemon queues commands and reads
# back reported state. This is the only reliable mechanism on Plasma 6
# Wayland, where KWin's Scripting.loadScript/start DBus path does not
# execute ad-hoc scripts in a running session.
# ----------------------------------------------------------------------------
_pending_command = None      # queued command string: minimize|restore|toggle
_last_reported_state = "absent"
_command_result = None       # last CommandDone result


def spotify_window_state():
    """Return the most recently reported window state."""
    return _last_reported_state


def spotify_window_minimize():
    global _pending_command
    _pending_command = "minimize"


def spotify_window_restore():
    global _pending_command
    _pending_command = "restore"


def spotify_window_toggle():
    """Toggle based on the live (poll-reported) window state."""
    global _pending_command
    state = _last_reported_state
    if state == "visible":
        _pending_command = "minimize"
    elif state == "minimized":
        _pending_command = "restore"
    else:
        # No window known — surface via MPRIS Raise.
        mpris_raise()


class SpotifyToggleCallback(dbus.service.Object):
    """Provides org.feuerdevil.SpotifyToggle at /SpotifyToggle for the KWin
    script to poll state and pull/complete commands."""

    def __init__(self, bus):
        super().__init__(bus, "/SpotifyToggle")

    @dbus.service.method("org.feuerdevil.SpotifyToggle",
                         in_signature="ss", out_signature="")
    def ReportState(self, tag, state):
        global _last_reported_state
        _last_reported_state = str(state)

    @dbus.service.method("org.feuerdevil.SpotifyToggle",
                         in_signature="", out_signature="s")
    def GetCommand(self):
        global _pending_command
        cmd = _pending_command
        _pending_command = None
        return cmd if cmd else "none"

    @dbus.service.method("org.feuerdevil.SpotifyToggle",
                         in_signature="s", out_signature="")
    def CommandDone(self, result):
        global _command_result
        _command_result = str(result)
        # If restore reported "raise", no window existed; surface via MPRIS.
        if result == "raise":
            mpris_raise()


# ----------------------------------------------------------------------------
# Small shared helper
# ----------------------------------------------------------------------------
def _as_str(v):
    if v is None:
        return ""
    return str(v)


# ----------------------------------------------------------------------------
# The DBusMenu (com.canonical.dbusmenu)
# ----------------------------------------------------------------------------
class SpotifyMenu(dbus.service.Object):
    OBJ_PATH = "/StatusNotifierItem/Menu"

    def __init__(self, bus, tray):
        super().__init__(bus, self.OBJ_PATH)
        self.tray = tray
        self._revision = 0

    def bump(self):
        self._revision += 1
        self.LayoutUpdated(self._revision, 0)

    # ---- com.canonical.dbusmenu methods ----
    @dbus.service.method(DBUSMENU_IFACE, in_signature="iias",
                         out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        layout = self._build_layout()
        node = layout.get(parent_id, ({}, []))
        return dbus.UInt32(self._revision), self._serialize_node(
            layout, parent_id, recursion_depth)

    @dbus.service.method(DBUSMENU_IFACE, in_signature="aias",
                         out_signature="a(ia{sv})")
    def GetGroupProps(self, ids, property_names):
        layout = self._build_layout()
        out = []
        for nid in ids:
            props = layout.get(nid, ({}, []))[0]
            out.append((dbus.Int32(nid),
                        dbus.Dictionary(props, signature="sv")))
        return out

    @dbus.service.method(DBUSMENU_IFACE, in_signature="is",
                         out_signature="v")
    def GetProperty(self, id, property_name):
        layout = self._build_layout()
        props = layout.get(id, ({}, []))[0]
        return props.get(property_name)

    @dbus.service.method(DBUSMENU_IFACE, in_signature="isvu",
                         out_signature="")
    def Event(self, id, event_id, data, timestamp):
        if event_id != "clicked":
            return
        t = self.tray
        if id == M_PREV:
            mpris_call("Previous")
        elif id == M_PLAYPAUSE:
            mpris_call("PlayPause")
        elif id == M_NEXT:
            mpris_call("Next")
        elif id == M_SHUFFLE:
            mpris_set_prop("Shuffle", not t._shuffle)
        elif id == M_LOOP:
            order = ["None", "Playlist", "Track"]
            cur = t._loop if t._loop in order else "None"
            new = order[(order.index(cur) + 1) % len(order)]
            mpris_set_prop("LoopStatus", new)
        elif id == M_SHOWHIDE:
            spotify_window_toggle()
        elif id == M_QUIT:
            mpris_quit()
            # Exit the proxy cleanly so the tray icon disappears and D-Bus
            # bus names are released. loop.quit() lets the MainLoop unwind
            # and Python exit normally (vs os._exit which leaks the names).
            mainloop = self.tray._mainloop
            GLib.idle_add(lambda: mainloop.quit())
        # MPRIS PropertiesChanged (or the state poll) drives menu refreshes;
        # do NOT bump here — it causes re-render loops that swallow clicks.

    @dbus.service.method(DBUSMENU_IFACE, in_signature="i",
                         out_signature="b")
    def AboutToShow(self, id):
        # Do not claim the layout changed; GetLayout already returns it.
        return False

    @dbus.service.signal(DBUSMENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass

    # ---- layout construction ----
    def _build_layout(self):
        t = self.tray
        playing = t._playback == "Playing"
        playpause_label = "Pause" if playing else "Play"
        playpause_icon = ("media-playback-pause-symbolic" if playing
                          else "media-playback-start-symbolic")


        if t._meta.get("title"):
            np = t._meta["title"]
            if t._meta.get("artist"):
                np = f"{t._meta['title']} — {t._meta['artist']}"
        elif t._playback == "Stopped":
            np = "Spotify (not playing)"
        else:
            np = "Spotify"

        showhide_label = "Toggle Tray"

        loop_label = LOOP_LABELS.get(t._loop, "Loop: Off")

        return {
            M_ROOT: ({P_CHILDREN_DISPLAY: "submenu"}, [
                M_NOWPLAYING, M_PREV, M_PLAYPAUSE, M_NEXT,
                M_SEP2, M_SHUFFLE, M_LOOP, M_SEP3, M_SHOWHIDE, M_QUIT]),
            M_NOWPLAYING: ({P_LABEL: np, P_ICON_NAME: "view-media-playlist",
                            P_ENABLED: False}, []),
            M_PREV: ({P_LABEL: "Previous",
                      P_ICON_NAME: "media-skip-backward-symbolic",
                      P_ENABLED: t._can_control}, []),
            M_PLAYPAUSE: ({P_LABEL: playpause_label,
                           P_ICON_NAME: playpause_icon,
                           P_ENABLED: t._can_control}, []),
            M_NEXT: ({P_LABEL: "Next",
                      P_ICON_NAME: "media-skip-forward-symbolic",
                      P_ENABLED: t._can_control}, []),
            M_SEP2: ({P_TYPE: TYPE_SEPARATOR}, []),
            M_SHUFFLE: ({P_LABEL: "Shuffle",
                         P_ICON_NAME: "media-playlist-shuffle-symbolic",
                         P_TOGGLE_TYPE: "checkmark",
                         P_TOGGLE_STATE: dbus.Int32(1 if t._shuffle else 0),
                         P_ENABLED: t._can_control}, []),
            M_LOOP: ({P_LABEL: loop_label,
                      P_ICON_NAME: "media-playlist-repeat-symbolic",
                      P_TOGGLE_TYPE: "checkmark",
                      P_TOGGLE_STATE: dbus.Int32(1 if t._loop != "None" else 0),
                      P_ENABLED: t._can_control}, []),
            M_SEP3: ({P_TYPE: TYPE_SEPARATOR}, []),
            M_SHOWHIDE: ({P_LABEL: showhide_label,
                          P_ICON_NAME: "go-jump-symbolic"}, []),
            M_QUIT: ({P_LABEL: "Quit Spotify",
                      P_ICON_NAME: "application-exit-symbolic"}, []),
        }

    def _serialize_node(self, layout, id, depth):
        props, children = layout.get(id, ({}, []))
        clean = dbus.Dictionary(
            {k: v for k, v in props.items() if v is not None},
            signature="sv")
        child_arr = dbus.Array(signature="v")
        # depth: -1 = unlimited, 0 = this node only
        if depth != 0:
            d = depth - 1 if depth > 0 else depth
            for cid in children:
                child_arr.append(self._serialize_node(layout, cid, d))
        return dbus.Struct(
            (dbus.Int32(id), clean, child_arr), signature="ia{sv}av")

# ----------------------------------------------------------------------------
# The SNI object (org.kde.StatusNotifierItem)
# ----------------------------------------------------------------------------
class SpotifyTray(dbus.service.Object):
    OBJ_PATH = "/StatusNotifierItem"
    def __init__(self, bus, mainloop=None):
        self.bus = bus
        self._mainloop = mainloop
        # Cached MPRIS state
        self._meta = {}
        self._playback = "Stopped"
        self._shuffle = False
        self._loop = "None"
        self._can_control = False
        self._window_state = "absent"
        self._art_bytes = None
        self._menu_obj = None  # set below
        super().__init__(bus, self.OBJ_PATH)
        self._menu_obj = SpotifyMenu(bus, self)
        self._register()
        # Refresh window state in the background periodically for the menu.
        self._refresh_window_state()

    def _register(self):
        try:
            watcher = self.bus.get_object(SNI_WATCHER_NAME,
                                          SNI_WATCHER_PATH)
            dbus.Interface(watcher, SNI_WATCHER_NAME).RegisterStatusNotifierItem(
                TRAY_BUS_NAME)
            log("Registered as StatusNotifierItem")
        except dbus.exceptions.DBusException as e:
            log(f"RegisterStatusNotifierItem failed: {e}")

    # ---- SNI methods ----
    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        spotify_window_toggle()

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        mpris_call("PlayPause")

    @dbus.service.method(SNI_IFACE, in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    # ---- SNI signals ----
    @dbus.service.signal(SNI_IFACE, signature="")
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewToolTip(self):
        pass
    @dbus.service.signal(SNI_IFACE, signature="s")
    def NewStatus(self, status):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewTitle(self):
        pass

    def _icon_name(self):
        # Fall back to the themed icon when no album art is available so
        # the tray still shows something (e.g. when paused/stopped).
        return "" if self._art_bytes else ICON_NAME

    def _icon_pixmap(self):
        # SNI IconPixmap: a(iiay) — (width, height, ARGB32 bytes).
        if not self._art_bytes:
            return dbus.Array(signature="(iiay)")
        w, h, raw = self._art_bytes
        barr = dbus.Array([dbus.Byte(b) for b in raw], signature="y")
        return dbus.Array(
            [dbus.Struct((dbus.Int32(w), dbus.Int32(h), barr))],
            signature="(iiay)")

    def _tooltip(self):
        # SNI ToolTip: (icon_name, image_pixmap, title, subtitle) —
        # Plasma renders this on hover. Title is "Spotify" (stable sort
        # key); subtitle carries the now-playing info.
        if t := self._meta.get("title"):
            artist = self._meta.get("artist") or ""
            sub = f"{t} — {artist}" if artist else t
        else:
            sub = "Not playing"
        return dbus.Struct(("",
                            dbus.Array(signature="(iiay)"),
                            "Spotify",
                            sub),
                           signature="sa(iiay)ss")

    def _notify_tooltip_change(self):
        def _notify():
            self.NewToolTip()
            self.PropertiesChanged(
                SNI_IFACE, {"ToolTip": self._tooltip()}, [])
            return False
        GLib.idle_add(_notify)

    def _notify_icon_change(self):
        def _notify():
            self.NewIcon()
            self.NewToolTip()
            self.PropertiesChanged(
                SNI_IFACE,
                {"IconPixmap": self._icon_pixmap(),
                 "IconName": self._icon_name(),
                 "ToolTip": self._tooltip()},
                [])
            return False
        GLib.idle_add(_notify)
    # ---- Properties ----
    def _props(self):
        return {
            "Id": "spotify-client",
            "Category": "ApplicationStatus",
            "IconName": self._icon_name(),
            "IconPixmap": self._icon_pixmap(),
            "Status": "Active",
            "IconAccessibleDesc": "",
            "ToolTip": self._tooltip(),
            "AttentionIconName": "",
            "AttentionAccessibleDesc": "",
            "Title": "Spotify",
            "IconThemePath": "",
            "Menu": dbus.ObjectPath(SpotifyMenu.OBJ_PATH),
            "XAyatanaLabel": "",
            "XAyatanaLabelGuide": "",
            "XAyatanaOrderingIndex": dbus.UInt32(0),
        }

    @dbus.service.method(PROPS_IFACE, in_signature="s",
                         out_signature="a{sv}")
    def GetAll(self, interface):
        if interface in ("org.kde.StatusNotifierItem", ""):
            return dbus.Dictionary(self._props(), signature="sv")
        return dbus.Dictionary({}, signature="sv")

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self._props().get(prop)

    @dbus.service.method(PROPS_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface, prop, value):
        pass

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    # ---- Refresh MPRIS state ----
    def refresh_mpris(self):
        root, player = mpris_get_all()
        if not player:
            self._playback = "Stopped"
            self._can_control = False
            self._meta = {}
            self._art_bytes = None
            self._notify_icon_change()
            return
        self._shuffle = bool(player.get("Shuffle", False))
        self._loop = str(player.get("LoopStatus", "None"))
        self._can_control = bool(player.get("CanControl", False))
        meta = dict(player.get("Metadata", {}))
        self._playback = str(player.get("PlaybackStatus", "Stopped"))
        title = _as_str(meta.get("xesam:title", ""))
        art = meta.get("xesam:artist", [])
        artist = ", ".join(_as_str(a) for a in art) if art else ""
        album = _as_str(meta.get("xesam:album", ""))
        arturl = _as_str(meta.get("mpris:artUrl", ""))
        self._meta = {"title": title, "artist": artist,
                      "album": album, "artUrl": arturl}
        self._notify_tooltip_change()
        if arturl:
            threading.Thread(target=self._fetch_art_thread,
                             args=(arturl,), daemon=True).start()
        else:
            self._art_bytes = None
            self._notify_icon_change()

    def _fetch_art_thread(self, url):
        self._art_bytes = fetch_art_argb32(url)
        self._notify_icon_change()

    def _refresh_window_state(self):
        new_state = spotify_window_state()
        if new_state != self._window_state:
            self._window_state = new_state
            if self._menu_obj:
                self._menu_obj.bump()
# ----------------------------------------------------------------------------
# MPRIS PropertiesChanged listener
# ----------------------------------------------------------------------------
class MprisWatcher:
    def __init__(self, tray):
        self.tray = tray
        bus = dbus.SessionBus()
        bus.add_signal_receiver(
            self._on_props_changed,
            dbus_interface=PROPS_IFACE,
            signal_name="PropertiesChanged",
            path=SPOTIFY_MPRIS_PATH)
    def _on_props_changed(self, iface, changed, invalidated):
        # Only react to changes that affect what the menu shows; Spotify
        # emits PropertiesChanged frequently (e.g. Position), and bumping
        # on each would re-render the popup and swallow clicks.
        try:
            keys = set(str(k) for k in changed.keys()) if changed else set()
        except Exception:
            keys = set()
        relevant = keys & {"PlaybackStatus", "Metadata", "Shuffle",
                           "LoopStatus", "Volume"}
        if not relevant:
            return
        self.tray.refresh_mpris()
        if self.tray._menu_obj:
            self.tray._menu_obj.bump()

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    # Single-instance guard: exit immediately if another proxy is already
    # running. Without this, a failed BusName acquisition leaves a ghost
    # process spinning in the MainLoop (invisible on D-Bus, eating CPU).
    try:
        name_owner = bus.call_blocking(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "GetNameOwner",
            "s", [TRAY_BUS_NAME])
    except dbus.exceptions.DBusException as e:
        if e.get_dbus_name() == "org.freedesktop.DBus.Error.NameHasNoOwner":
            name_owner = None
        else:
            raise
    if name_owner:
        log(f"{TRAY_BUS_NAME} already owned by {name_owner}; exiting")
        return

    loop = GLib.MainLoop()

    # Retain references: BusName releases the name when its Python object
    # is garbage-collected, so the wrappers must live for the process lifetime.
    name_tray = dbus.service.BusName(TRAY_BUS_NAME, bus,
                                     replace_existing=True,
                                     allow_replacement=True,
                                     do_not_queue=True)
    name_toggle = dbus.service.BusName(SPOTIFY_TOGGLE_BUS_NAME, bus,
                                       replace_existing=True,
                                       allow_replacement=True,
                                       do_not_queue=True)
    SpotifyToggleCallback(bus)
    tray = SpotifyTray(bus, loop)
    MprisWatcher(tray)
    tray.refresh_mpris()
    # Periodic window-state refresh so the Show/Minimize label stays current.
    def _tick():
        tray._refresh_window_state()
    GLib.timeout_add_seconds(1, _tick)

    # Clean exit on SIGTERM/SIGINT so pkill (without -9) releases bus names.
    import signal
    def _on_signal(sig, frame):
        loop.quit()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log("Spotify Tray Proxy running")
    loop.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
