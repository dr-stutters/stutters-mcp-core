"""Sender allow-lists and rate limits for the inbound receivers."""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict
from collections.abc import Iterable

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(spec: str | Iterable[str] | None) -> list[IPNetwork]:
    """'10.0.0.0/8, 198.18.128.11' (comma/space separated) or an iterable -> networks.
    Raises ValueError naming the bad entry."""
    if spec is None:
        return []
    items = spec.replace(",", " ").split() if isinstance(spec, str) else [str(s).strip() for s in spec]
    nets: list[IPNetwork] = []
    for item in items:
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"Not an IP address or network: {item!r}") from None
    return nets


class AllowList:
    """Source-address allow-list. Empty means everyone is allowed (`open` is True)."""

    def __init__(self, spec: str | Iterable[str] | None = None) -> None:
        self.networks = parse_networks(spec)

    @property
    def open(self) -> bool:
        return not self.networks

    def allows(self, address: str | None) -> bool:
        if self.open:
            return True
        if not address:
            return False
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip.version == n.version and ip in n for n in self.networks)

    def __str__(self) -> str:
        return ", ".join(str(n) for n in self.networks) or "any"


class TokenBucket:
    """`rate` events per second on average, bursts up to `burst`."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = burst if burst is not None else max(1.0, rate * 2)
        self._tokens = self.burst
        self._stamp = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    def take(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def wait_time(self) -> float:
        """Seconds until a token is available (0 if one is)."""
        self._refill()
        return 0.0 if self._tokens >= 1 else (1 - self._tokens) / self.rate


class PerKeyLimiter:
    """One TokenBucket per key (sender address), keeping at most `max_keys` buckets (LRU)."""

    def __init__(self, rate: float | None, burst: float | None = None, max_keys: int = 4096) -> None:
        self.rate = rate
        self.burst = burst
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def bucket(self, key: str) -> TokenBucket | None:
        if self.rate is None:
            return None
        b = self._buckets.get(key)
        if b is None:
            b = self._buckets[key] = TokenBucket(self.rate, self.burst)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        return b

    def allow(self, key: str) -> bool:
        b = self.bucket(key)
        return True if b is None else b.take()
