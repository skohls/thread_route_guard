# Thread Route Guard

Keeps Matter-over-Thread devices reachable from Home Assistant OS when one of several Thread
border routers fails.

## The problem

Every Thread border router announces the route to its Thread network (the OMR prefix) to
your LAN in a Router Advertisement (Route Information Option). With two or more border
routers, NetworkManager on the Home Assistant OS host installs **one route with several next
hops**:

```
fd9f:bee8:12d4:1::/64 proto ra metric 105
    nexthop via fe80::d405:92ff:fe70:d80c dev enp6s18 weight 1
    nexthop via fe80::d405:92ff:fe70:daf8 dev enp6s18 weight 1
```

When a border router disappears without withdrawing its route (power loss, cable pulled,
crash), its next hop stays in that route until the announced lifetime runs out. OpenThread
uses a fixed lifetime of 1800 s, so this lasts up to **30 minutes**. During that time the
kernel keeps hashing connections onto the dead next hop: devices whose address lands on it
don't respond, all others work. Restarting the Matter server doesn't help.

The kernel does know the neighbour is gone (`ip -6 neigh` shows `FAILED`), but it only
takes neighbour state into account for multipath routes when IPv6 forwarding is off.
Home Assistant OS needs forwarding on for Docker. Deleting the dead next hop by hand doesn't
last either: NetworkManager writes it back from its Router Advertisement cache within about
90 seconds.

## What the app does

Every few seconds it checks each next hop of every watched route with a Neighbor
Solicitation, the same check OpenThread's own routing manager does for other border
routers. When at least one next hop is dead and at least one is healthy, it adds its own
route over the healthy next hops with a metric one below NetworkManager's. That route wins
immediately. When all next hops are healthy again, or when NetworkManager's route no longer
has several next hops (the lifetime ran out and NetworkManager dropped the dead one), the app
removes its route again.

- **Watched routes** are found automatically: routes learned from Router Advertisements
  (`proto ra`) with at least two next hops, except the default route. The interface is taken
  from the route. Nothing needs to be configured for a typical setup.
- **Only its own routes are ever changed.** They are marked with routing protocol number 197
  (`ip -6 route show proto 197`). NetworkManager's routes are never modified or deleted.
- **Why metric − 1:** NetworkManager uses the interface metric for a prefix announced by one
  router and the interface metric + 5 for a prefix announced by several. One below the
  multipath metric never collides with either, so NetworkManager never takes the app's route
  for one of its own.
- **All next hops dead:** nothing to route around, the app removes its route and waits.
- **Stopping the app** removes its routes.

## Options

| Option | Default | Meaning |
|---|---|---|
| `prefixes` | empty | Only watch routes inside these prefixes, e.g. `fd00::/8`. Empty watches every RA-learned route with several next hops. OMR prefixes are usually ULA (`fd…`), but can be global when the border router gets a delegated prefix, so the default doesn't filter. |
| `probe_interval` | 5 | Seconds between two checks of every next hop. |
| `dead_after` | 30 | Seconds of failed checks before a next hop counts as dead. |
| `healthy_after` | 60 | Seconds of successful checks before a dead next hop counts as healthy again. |
| `dry_run` | false | Only log what would change, never touch a route. |
| `log_level` | info | `debug` logs every single check; `info` logs only state changes. |

## Permissions

The app runs in the host network namespace (`host_network`) and needs `NET_ADMIN` to change
routes. It uses neither full access nor the Docker API.

## Supported installations

- **Home Assistant OS**: supported (amd64, aarch64).
- **Home Assistant Supervised**: should work if the host uses NetworkManager, untested.
- **Home Assistant Container / Core**: apps are not available there. On a host with
  `net.ipv6.conf.all.forwarding=0`, which is the usual default, the kernel skips dead next
  hops by itself and the problem doesn't occur.

## Checking what happens

```
ip -6 route show proto 197        # routes set by the app
ip -6 route get <device address>  # which next hop a device uses
ip -6 neigh show dev <interface>  # neighbour state of the border routers
```

## Background

- OpenThread discussion about the fixed route lifetime:
  [openthread/openthread#13656](https://github.com/openthread/openthread/issues/13656)
- OpenThread's routing manager does the same active check for peer border routers
  (`OPENTHREAD_CONFIG_BORDER_ROUTING_ROUTER_ACTIVE_CHECK_TIMEOUT`).
