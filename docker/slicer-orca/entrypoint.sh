#!/bin/sh
# Entrypoint for the PrintForge OrcaSlicer sandbox image.
#
# OrcaSlicer is a wxWidgets/OpenGL application: even the headless CLI creates a
# GUI toolkit and GL context, so it needs an X display. `xvfb-run -a` starts a
# throwaway Xvfb on the first free display number (safe for parallel jobs) and
# tears it down when OrcaSlicer exits. Its exit status is passed through.
#
# The container root filesystem is read-only, so every writable path has to live
# under the /tmp tmpfs: HOME, XDG config/cache and the XDG runtime dir.
set -eu

export HOME="${HOME:-/tmp}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$HOME/xdg-runtime}"

mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true

# OrcaSlicer writes a per-run log (e.g. 00000.log) into the *current working
# directory*. The image WORKDIR (/work) is mounted read-only, so move to the
# writable tmpfs first; input/output paths are always passed as absolutes.
cd "$HOME"

# -nolisten tcp: belt-and-braces even though the sandbox runs --network none.
#
# NOTE: do NOT `exec` xvfb-run. The sandbox has no init during the slice
# (the worker's `docker run` has no --init), so this script is PID 1. OrcaSlicer
# leaves a short-lived helper process behind; if xvfb-run were PID 1 that orphan
# would be reparented to it and its bare `wait` would block forever. Keeping
# this shell as PID 1 means the orphan reparents here and xvfb-run exits cleanly;
# Docker reaps anything still alive when this script exits.
xvfb-run -a --server-args="-screen 0 1600x1200x24 -nolisten tcp" \
    /opt/orcaslicer/AppRun "$@"
