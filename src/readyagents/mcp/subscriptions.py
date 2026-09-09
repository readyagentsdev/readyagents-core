"""Opt-in ``subscriptions/listen`` with acknowledgement and a hard cap.

Streams live only for the lifetime of the foreground command. Tools do not
change at runtime unless a pack is loaded after listen; clients that did not
opt into ``toolsListChanged`` receive no such notifications.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Mapping
from typing import Any

from readyagents.errors import HttpRequestError
from readyagents.mcp.protocol import META_SUBSCRIPTION_ID, RESULT_TYPE_COMPLETE

_MAX_SUBSCRIPTION_ID_LEN = 64


class SubscriptionRegistry:
    def __init__(self, *, max_streams: int) -> None:
        self.max_streams = max(1, int(max_streams))
        self._lock = threading.Lock()
        self._subs: dict[str, frozenset[str]] = {}

    def listen(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        wanted = _requested_types(params)
        if not wanted:
            raise HttpRequestError("subscriptions/listen requires an explicit notifications opt-in")
        with self._lock:
            if len(self._subs) >= self.max_streams:
                raise HttpRequestError("subscription cap exceeded")
            sub_id = secrets.token_hex(16)
            self._subs[sub_id] = wanted
        return {
            "resultType": RESULT_TYPE_COMPLETE,
            "_meta": {META_SUBSCRIPTION_ID: sub_id},
            "ttlMs": 0,
            "cacheScope": "private",
        }

    def acknowledged(self, subscription_id: str) -> dict[str, Any]:
        with self._lock:
            types = self._subs.get(subscription_id, frozenset())
        return {
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "_meta": {META_SUBSCRIPTION_ID: subscription_id},
                "notifications": {name: True for name in sorted(types)},
            },
        }

    def drop(self, subscription_id: str) -> None:
        with self._lock:
            self._subs.pop(subscription_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)


def _requested_types(params: Mapping[str, Any] | None) -> frozenset[str]:
    if not isinstance(params, Mapping):
        return frozenset()
    notifications = params.get("notifications")
    names: set[str] = set()
    if isinstance(notifications, Mapping):
        if notifications.get("toolsListChanged") is True:
            names.add("toolsListChanged")
        extra = notifications.get("types")
        if isinstance(extra, list):
            for item in extra:
                if isinstance(item, str) and item.strip():
                    names.add(item.strip())
    elif isinstance(notifications, list):
        for item in notifications:
            if isinstance(item, str) and item.strip():
                names.add(item.strip())
    return frozenset(names)
