#!/bin/sh
# Copy the app to /local_apps on a Home Assistant host for testing and rebuild it there.
# The Supervisor builds local apps from the Dockerfile only without an `image:` key,
# so that key is removed from the copy.
#   scripts/deploy-local.sh <ssh host with the Terminal & SSH app>
set -eu
host=${1:?usage: $0 <ssh host>}
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp -R thread_route_guard "$tmp/"
find "$tmp" -name __pycache__ -prune -exec rm -rf {} +
sed -i.bak '/^image:/d' "$tmp/thread_route_guard/config.yaml" && rm "$tmp/thread_route_guard/config.yaml.bak"
tar -C "$tmp" -cf - thread_route_guard | ssh "$host" 'tar -C /local_apps -xf - && ha store reload && ha apps rebuild local_thread_route_guard'
