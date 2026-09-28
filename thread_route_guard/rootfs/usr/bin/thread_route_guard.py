#!/usr/bin/env python3
"""Thread Route Guard: route around dead next hops in RA-learned multipath routes.

Every Thread border router announces its OMR prefix to the LAN in a Route Information
Option. With several border routers, NetworkManager on the host installs one route with
several next hops. When a border router disappears without withdrawing its prefix, its next
hop stays in that route until the RIO lifetime runs out (1800 s with OpenThread). The kernel
keeps hashing flows onto the dead next hop: it only honours neighbour state for multipath
routes with forwarding=0, and Home Assistant OS runs with forwarding=1.

This guard probes every next hop with Neighbor Solicitations. While at least one next hop is
dead and at least one is healthy, it installs its own route over the healthy ones with a
metric one below NetworkManager's. It removes that route as soon as all next hops are
healthy again or NetworkManager's route no longer has several next hops.

Own routes carry the routing protocol number PROTO, so they can be recognised after a restart.
NetworkManager uses <device metric> for a prefix announced by one router and
<device metric> + 5 for a prefix announced by several. The guard's <NM metric> - 1 never
collides with either, so NetworkManager never mistakes it for one of its own routes.
"""
import ipaddress
import json
import logging
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PROTO = 197
OPTIONS_FILE = "/data/options.json"
DEFAULTS = {
    "prefixes": [],
    "probe_interval": 5,
    "dead_after": 30,
    "healthy_after": 60,
    "dry_run": False,
    "log_level": "info",
}

log = logging.getLogger("thread_route_guard")


class Hop:
    """Health of one next hop, switching only after a sustained contrary result."""

    def __init__(self):
        self.healthy = True
        self.contrary_since = None

    def update(self, ok, now, dead_after, healthy_after):
        if ok == self.healthy:
            self.contrary_since = None
            return False
        if self.contrary_since is None:
            self.contrary_since = now
        if now - self.contrary_since >= (healthy_after if ok else dead_after):
            self.healthy, self.contrary_since = ok, None
            return True
        return False


def ip(*args):
    result = subprocess.run(["ip", *args], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f"ip {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def hops_of(route):
    """Sorted (gateway, device) pairs of a route, single or multipath."""
    entries = route.get("nexthops") or [route]
    return tuple(sorted((h["gateway"], h["dev"]) for h in entries if "gateway" in h and "dev" in h))


def describe(hops):
    devs = {dev for _, dev in hops}
    via = ", ".join(gw for gw, _ in hops)
    return f"via {via} dev {devs.pop()}" if len(devs) == 1 else via


def read_routes(filters):
    """Return (watched NetworkManager routes, own routes, metrics used by other routes).

    watched: dst -> {"metric", "hops"} for RA routes with at least two next hops.
    own:     dst -> list of {"metric", "hops"} carrying PROTO.
    taken:   dst -> set of metrics used by any route that is not ours.
    """
    out = ip("-j", "-6", "route", "show", "table", "main")
    watched, own, taken = {}, {}, {}
    for route in json.loads(out) if out.strip() else []:
        dst = route.get("dst")
        if not dst or dst == "default":
            continue
        try:
            net = ipaddress.IPv6Network(dst)
        except ValueError:
            continue
        key, metric, hops = str(net), route.get("metric", 0), hops_of(route)
        protocol = str(route.get("protocol", "boot"))
        if protocol == str(PROTO):
            own.setdefault(key, []).append({"metric": metric, "hops": hops})
            continue
        taken.setdefault(key, set()).add(metric)
        if protocol != "ra" or len(hops) < 2 or net.prefixlen == 0:
            continue
        if filters and not any(net.subnet_of(f) for f in filters):
            continue
        if key not in watched or metric < watched[key]["metric"]:
            watched[key] = {"metric": metric, "hops": hops}
    return watched, own, taken


def probe(hop):
    """True if the next hop answers a Neighbor Solicitation (3 tries, 1 s each)."""
    gateway, dev = hop
    try:
        result = subprocess.run(["ndisc6", "-q", "-1", "-r", "3", "-w", "1000", gateway, dev],
                                capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return result.returncode == 0, result.stderr.strip()


def route_args(dst, route):
    args = [dst, "proto", str(PROTO), "metric", str(route["metric"])]
    if len(route["hops"]) == 1:
        gateway, dev = route["hops"][0]
        return args + ["via", gateway, "dev", dev]
    for gateway, dev in route["hops"]:
        args += ["nexthop", "via", gateway, "dev", dev]
    return args


class Guard:
    def __init__(self, options):
        self.opt = options
        self.filters = [ipaddress.IPv6Network(p, strict=False) for p in options["prefixes"]]
        self.dry_run = options["dry_run"]
        self.hops = {}
        self.watched = {}
        self.simulated = {}  # own routes in dry-run mode, dst -> list of routes
        self.blocked = set()  # prefixes whose metric below NetworkManager's is taken
        self.failing = None
        self.pool = ThreadPoolExecutor(max_workers=8)

    def own_routes(self, kernel_own):
        return self.simulated if self.dry_run else kernel_own

    def add(self, dst, route, reason):
        prefix = "[dry run] would add" if self.dry_run else "Adding"
        log.info("%s route %s %s metric %d (%s)", prefix, dst, describe(route["hops"]),
                 route["metric"], reason)
        if self.dry_run:
            self.simulated.setdefault(dst, []).append(route)
        else:
            ip("-6", "route", "add", *route_args(dst, route))

    def delete(self, dst, route, reason):
        prefix = "[dry run] would remove" if self.dry_run else "Removing"
        log.info("%s route %s %s metric %d (%s)", prefix, dst, describe(route["hops"]),
                 route["metric"], reason)
        if self.dry_run:
            self.simulated[dst].remove(route)
            if not self.simulated[dst]:
                del self.simulated[dst]
        else:
            ip("-6", "route", "del", dst, "proto", str(PROTO), "metric", str(route["metric"]))

    def log_watched(self, watched):
        for dst in self.watched.keys() - watched.keys():
            log.info("No longer watching %s", dst)
        for dst, route in watched.items():
            if self.watched.get(dst) != route:
                log.info("Watching %s %s (NetworkManager metric %d)", dst,
                         describe(route["hops"]), route["metric"])
        self.watched = watched

    def check_hops(self, watched):
        keys = {hop for route in watched.values() for hop in route["hops"]}
        for hop in self.hops.keys() - keys:
            del self.hops[hop]
        results = dict(zip(keys, self.pool.map(probe, keys)))
        now = time.monotonic()
        for hop, (ok, detail) in results.items():
            state = self.hops.setdefault(hop, Hop())
            log.debug("Probe %s%%%s: %s %s", hop[0], hop[1], "ok" if ok else "failed", detail)
            if state.update(ok, now, self.opt["dead_after"], self.opt["healthy_after"]):
                log.info("Next hop %s dev %s is %s", hop[0], hop[1],
                         "healthy again" if state.healthy else "dead")

    def desired(self, watched, taken):
        result = {}
        for dst, route in watched.items():
            healthy = tuple(h for h in route["hops"] if self.hops[h].healthy)
            if not healthy or len(healthy) == len(route["hops"]):
                continue
            metric = route["metric"] - 1
            if metric < 0 or metric in taken.get(dst, set()):
                if dst not in self.blocked:
                    log.error("Cannot route around the dead next hop for %s: metric %d is "
                              "already used by another route", dst, metric)
                    self.blocked.add(dst)
                continue
            self.blocked.discard(dst)
            result[dst] = {"metric": metric, "hops": healthy}
        return result

    def reconcile(self, desired, own):
        for dst, routes in list(own.items()):
            for route in list(routes):
                want = desired.get(dst)
                if want == route:
                    continue
                if want:
                    reason = "healthy next hops changed"
                elif dst in self.watched:
                    healthy = [self.hops[h].healthy for h in self.watched[dst]["hops"]]
                    reason = ("all next hops healthy" if all(healthy) else
                              "no healthy next hop left" if not any(healthy) else
                              "metric not available")
                else:
                    reason = "NetworkManager route no longer has several next hops"
                self.delete(dst, route, reason)
        for dst, route in desired.items():
            if route not in own.get(dst, []):
                dead = [h[0] for h in self.watched[dst]["hops"] if not self.hops[h].healthy]
                self.add(dst, route, f"dead next hop {', '.join(dead)}")

    def step(self):
        watched, kernel_own, taken = read_routes(self.filters)
        self.log_watched(watched)
        self.check_hops(watched)
        self.reconcile(self.desired(watched, taken), self.own_routes(kernel_own))

    def cleanup(self):
        if self.dry_run:
            return
        _, own, _ = read_routes(self.filters)
        for dst, routes in own.items():
            for route in routes:
                self.delete(dst, route, "shutting down")

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.step()
                if self.failing:
                    log.info("Recovered from: %s", self.failing)
                    self.failing = None
            except Exception as exc:  # keep guarding through transient failures
                message = f"{type(exc).__name__}: {exc}"
                if message != self.failing:
                    log.error(message)
                self.failing = message
            time.sleep(max(0.5, self.opt["probe_interval"] - (time.monotonic() - started)))


def load_options():
    options = dict(DEFAULTS)
    try:
        with open(OPTIONS_FILE) as f:
            options.update(json.load(f))
    except FileNotFoundError:
        pass
    return options


def main():
    options = load_options()
    logging.basicConfig(level=options["log_level"].upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        guard = Guard(options)
    except ValueError as exc:
        log.error("Invalid prefix filter: %s", exc)
        sys.exit(1)

    def stop(signum, _frame):
        log.info("Received %s, removing own routes", signal.Signals(signum).name)
        try:
            guard.cleanup()
        except Exception as exc:
            log.error("Cleanup failed: %s: %s", type(exc).__name__, exc)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("Thread Route Guard started: prefixes=%s, probe every %ds, dead after %ds, "
             "healthy after %ds%s", options["prefixes"] or "all", options["probe_interval"],
             options["dead_after"], options["healthy_after"],
             ", DRY RUN (no route changes)" if guard.dry_run else "")
    guard.run()


if __name__ == "__main__":
    main()
