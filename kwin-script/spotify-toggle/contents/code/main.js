/*
 * Spotify Toggle KWin Script
 *
 * Bridge for Spotify window minimize/restore on Plasma 6 Wayland.
 * Installed as a package (so KWin auto-runs it at startup) and enabled via
 * kwinrc [Plugins] spotify-toggleEnabled=true.
 *
 * Plasma 6 KWin scripting notes:
 *   - window.minimized is a settable property (setMinimized() does NOT exist
 *     on XdgToplevelWindow).
 *   - window.skipTaskbar hides the window from the taskbar (tray-only).
 *   - QTimer is the correct timer API; setInterval is not available.
 *   - callDBus without a callback silently fails to deliver on some setups;
 *     pass a no-op callback to force delivery.
 *
 * Polls the tray proxy daemon every 50ms. The daemon queues toggle commands
 * based on the reported state; this script pulls and executes them.
 *
 * Reload after edits without restarting KWin:
 *   gdbus call -e -d org.kde.KWin -o /Scripting -m org.kde.kwin.Scripting.unloadScript spotify-toggle
 *   gdbus call -e -d org.kde.KWin -o /Scripting -m org.kde.kwin.Scripting.start
 *
 * Daemon exposes org.feuerdevil.SpotifyToggle at /SpotifyToggle.
 */

var SpotifyToggle = (function () {
    var DAEMON = "org.feuerdevil.SpotifyToggle";
    var PATH = "/SpotifyToggle";
    var IFACE = "org.feuerdevil.SpotifyToggle";

    // Older Spotify builds report resourceClass/resourceName "spotify"; since
    // 1.2.95 the Flatpak sets the Wayland app_id to "com.spotify.Client"
    // (resourceClass and desktopFileName) with an empty resourceName.
    function isSpotify(c) {
        var ids = [c.resourceClass, c.resourceName, c.desktopFileName];
        for (var i = 0; i < ids.length; i++) {
            var id = String(ids[i] || "").toLowerCase();
            if (id === "spotify" || id === "com.spotify.client") return true;
        }
        return false;
    }

    function findSpotify() {
        var list = workspace.windowList ? workspace.windowList() : workspace.clientList();
        for (var i = 0; i < list.length; i++) {
            if (isSpotify(list[i])) return list[i];
        }
        return null;
    }

    function getState() {
        var c = findSpotify();
        if (!c) return "absent";
        return c.minimized ? "minimized" : "visible";
    }

    function done(result) {
        try { callDBus(DAEMON, PATH, IFACE, "CommandDone", result, function () {}); }
        catch (e) {}
    }

    function minimize(c) {
        c.minimized = true;
        c.skipTaskbar = true;
        done("minimized");
    }

    function restore(c) {
        c.skipTaskbar = false;
        c.minimized = false;
        try { c.desktops = [workspace.currentDesktop]; } catch (e) {}
        try { workspace.activeWindow = c; } catch (e) {}
        done("visible");
    }

    function handleCommand(cmd) {
        var c = findSpotify();
        if (cmd === "minimize") {
            if (c) minimize(c); else done("absent");
        } else if (cmd === "restore") {
            if (c) restore(c); else done("raise");
        } else if (cmd === "toggle") {
            if (!c) done("raise");
            else if (c.minimized) restore(c);
            else minimize(c);
        }
    }

    function poll() {
        var state = getState();
        try {
            callDBus(DAEMON, PATH, IFACE, "ReportState", "poll", state,
                function () {});
        } catch (e) { return; }
        try {
            callDBus(DAEMON, PATH, IFACE, "GetCommand",
                function (cmd) {
                    if (cmd && cmd !== "none" && cmd !== "") {
                        handleCommand(cmd);
                    }
                });
        } catch (e) {}
    }

    return { poll: poll };
})();

var spotifyToggleTimer = new QTimer();
spotifyToggleTimer.interval = 50;
spotifyToggleTimer.timeout.connect(SpotifyToggle.poll);
spotifyToggleTimer.start();
console.info("spotify-toggle: script started");
