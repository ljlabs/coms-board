#!/bin/sh
set -eu

mkdir -p "$HOME/playwright-output" "$XDG_CACHE_HOME/ms-playwright/daemon"
exec "$@"
