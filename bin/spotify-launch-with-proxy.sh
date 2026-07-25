#!/bin/sh
# Launches Spotify (Flatpak) and the tray proxy alongside it.
set -e
/usr/bin/flatpak run --branch=stable --arch=x86_64 \
    --command=spotify --file-forwarding com.spotify.Client "$@" &
"$HOME/.local/bin/spotify-tray-proxy.py" &
