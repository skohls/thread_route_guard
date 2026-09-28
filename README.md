# Thread Route Guard

Home Assistant app (formerly add-on) that keeps Matter-over-Thread devices reachable when one
of several Thread border routers fails.

Home Assistant OS keeps the route over a dead border router for up to 30 minutes and sends
part of the Thread traffic into the void. This app checks every border router actively and
routes around the dead ones until they are back. Details, options and limits:
[`thread_route_guard/DOCS.md`](thread_route_guard/DOCS.md).

## Installation

1. Settings → Apps → App store → ⋮ → Repositories, add
   `https://github.com/skohls/thread_route_guard`.
2. Install **Thread Route Guard** and start it. The defaults need no changes.

## License

[MIT](LICENSE)
