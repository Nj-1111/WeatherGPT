"""Per-client fixed-window rate limiting, keyed on the socket peer address — behind a proxy every caller shares that IP, and trusting X-Forwarded-For instead would let a client spoof past the limit (accepted tradeoff for the current deployment, see CLAUDE.md's Known limitations)."""
from __future__ import annotations

import time
from collections import OrderedDict


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int, max_clients: int = 10000) -> None:
        self._per_minute = per_minute
        self._per_day = per_day
        self._max_clients = max_clients
        self._clients: OrderedDict[str, tuple[int, int, int, int]] = OrderedDict()

    def check(self, client: str, now: float | None = None) -> int | None:
        """Return seconds to wait when the client is over budget, else None."""
        now = now if now is not None else time.time()
        minute, day = int(now // 60), int(now // 86400)
        last_minute, minute_count, last_day, day_count = self._clients.get(client, (minute, 0, day, 0))
        if last_minute != minute:
            last_minute, minute_count = minute, 0
        if last_day != day:
            last_day, day_count = day, 0

        # Written back on every path, refused or not — returning early used to skip the write, so a new day's counter only took effect on the next allowed request.
        retry_after: int | None = None
        if minute_count >= self._per_minute:
            retry_after = int((minute + 1) * 60 - now) or 1
        elif day_count >= self._per_day:
            retry_after = int((day + 1) * 86400 - now) or 1
        else:
            minute_count, day_count = minute_count + 1, day_count + 1

        self._clients[client] = (last_minute, minute_count, last_day, day_count)
        self._clients.move_to_end(client)
        while len(self._clients) > self._max_clients:
            self._clients.popitem(last=False)
        return retry_after
