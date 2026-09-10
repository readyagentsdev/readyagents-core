"""Process-level socket guard. Floor on top of SSRF pinning; never widens."""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from readyagents.errors import EgressDenied

_current_node: ContextVar[str | None] = ContextVar("sovereign_node", default=None)
_lock = threading.RLock()
_guard: EgressGuard | None = None
_orig_connect: Any = None
_orig_connect_ex: Any = None
_orig_create: Any = None
_orig_getaddrinfo: Any = None


def set_node(node_id: str | None) -> Any:
    """Bind the current node id for refusal records. Returns a reset token."""
    return _current_node.set(node_id)


def reset_node(token: Any) -> None:
    _current_node.reset(token)


def current_node() -> str | None:
    return _current_node.get()


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_loopback_host(host: str) -> bool:
    name = (host or "").strip().lower().rstrip(".")
    if name in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback)


def is_loopback_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(str(url).strip())
    host = parsed.hostname or ""
    if not host and "://" not in str(url):
        host = str(url).split("/", 1)[0].split(":", 1)[0]
    return is_loopback_host(host)


def is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if ip.is_global:
        return False
    return bool(ip.is_private)


def parse_allow_spec(spec: str) -> str:
    raw = spec.strip()
    if not raw:
        return ""
    if "://" in raw:
        host = urlparse(raw).hostname or ""
        return host.strip().lower().rstrip(".")
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")].lower()
    return raw.split("/", 1)[0].split(":", 1)[0].strip().lower().rstrip(".")


def is_keyless_compat_url(
    url: str | None,
    extra_allow: Sequence[str] | None = None,
) -> bool:
    """Loopback, or a parsed private allow spec (active guard, else extra_allow)."""
    if is_loopback_url(url):
        return True
    if not url:
        return False
    host = (urlparse(str(url).strip()).hostname or "").lower().rstrip(".")
    if not host:
        return False
    guard = active_guard()
    if guard is not None:
        if host in guard.allow_hosts:
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return str(ip) in guard.allow_ips
    parsed = {parse_allow_spec(item) for item in (extra_allow or []) if str(item).strip()}
    parsed.discard("")
    if host not in parsed:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return is_private_ip(ip)


class EgressGuard:
    """Installed socket wrappers. Call ``close()`` to restore originals."""

    def __init__(self, allowlist: list[str] | None = None) -> None:
        self.allow_specs = [item.strip() for item in (allowlist or []) if item and item.strip()]
        self.allow_hosts: set[str] = set()
        self.allow_ips: set[str] = set()
        self.attempts: list[dict[str, Any]] = []
        self.dns: list[dict[str, Any]] = []
        self._closed = False
        for spec in self.allow_specs:
            host = parse_allow_spec(spec)
            if host:
                self.allow_hosts.add(host)
                self._resolve_allow(host)

    def _resolve_allow(self, host: str) -> None:
        if is_loopback_host(host):
            return
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if not is_private_ip(ip):
                raise EgressDenied(
                    host,
                    node_id=current_node(),
                )
            self.allow_ips.add(str(ip))
            return
        resolver = _orig_getaddrinfo or socket.getaddrinfo
        try:
            infos = resolver(host, None, type=socket.SOCK_STREAM)
        except OSError as extra:
            self.dns.append({"host": host, "ok": False, "at": utc_stamp()})
            raise EgressDenied(host, node_id=current_node()) from extra
        ips: list[str] = []
        for info in infos:
            addr = info[4][0]
            try:
                parsed = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if parsed.version == 6 and parsed.ipv4_mapped is not None:
                parsed = parsed.ipv4_mapped
            if not is_private_ip(parsed) and not parsed.is_loopback:
                raise EgressDenied(f"{host}->{addr}", node_id=current_node())
            text = str(parsed)
            self.allow_ips.add(text)
            ips.append(text)
        if not ips:
            self.dns.append({"host": host, "ok": False, "at": utc_stamp()})
            raise EgressDenied(host, node_id=current_node())
        self.dns.append({"host": host, "ips": ips, "ok": True, "at": utc_stamp()})

    def snapshot(self) -> dict[str, Any]:
        return {
            "egress_attempts": list(self.attempts),
            "allowed_endpoints": list(self.allow_specs),
            "dns": list(self.dns),
        }

    def record(
        self, destination: str, *, allowed: bool, reason: str, host: str | None = None
    ) -> None:
        self.attempts.append(
            {
                "at": utc_stamp(),
                "destination": destination,
                "host": host,
                "allowed": allowed,
                "reason": reason,
                "node_id": current_node(),
            }
        )

    def check(self, address: Any) -> None:
        host, port = _split_address(address)
        dest = f"{host}:{port}" if port is not None else str(host)
        if host is None:
            return
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        host_s = str(host).strip().lower().rstrip(".")
        if host_s.startswith("/"):
            return
        try:
            ip = ipaddress.ip_address(host_s)
        except ValueError:
            ip = None
        if ip is None:
            if is_loopback_host(host_s):
                self.record(dest, allowed=True, reason="loopback", host=host_s)
                return
            # Re-resolve every connect: allow_hosts must not skip a public IP flip.
            resolver = _orig_getaddrinfo or socket.getaddrinfo
            try:
                infos = resolver(host_s, port, type=socket.SOCK_STREAM)
            except OSError as extra:
                self.dns.append({"host": host_s, "ok": False, "at": utc_stamp()})
                self.record(dest, allowed=False, reason="unresolved", host=host_s)
                raise EgressDenied(dest, node_id=current_node()) from extra
            ips = []
            for info in infos:
                addr = str(info[4][0])
                ips.append(addr)
                self._check_ip(addr, dest, host=host_s)
            if not ips:
                self.record(dest, allowed=False, reason="unresolved", host=host_s)
                raise EgressDenied(dest, node_id=current_node())
            self.dns.append({"host": host_s, "ips": ips, "ok": True, "at": utc_stamp()})
            return
        self._check_ip(host_s, dest, host=host_s)

    def _check_ip(self, ip_text: str, dest: str, *, host: str) -> None:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            self.record(dest, allowed=False, reason="invalid", host=host)
            raise EgressDenied(dest, node_id=current_node()) from None
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            self.record(dest, allowed=True, reason="loopback", host=host)
            return
        if str(ip) in self.allow_ips or host in self.allow_hosts:
            if is_private_ip(ip):
                self.record(dest, allowed=True, reason="allowlist", host=host)
                return
            self.record(dest, allowed=False, reason="allowlist_not_private", host=host)
            raise EgressDenied(dest, node_id=current_node())
        self.record(dest, allowed=False, reason="public", host=host)
        raise EgressDenied(dest, node_id=current_node())

    def close(self) -> None:
        global _guard
        with _lock:
            if self._closed:
                return
            self._closed = True
            _uninstall(self)


def _split_address(address: Any) -> tuple[Any, int | None]:
    if isinstance(address, tuple) and address:
        host = address[0]
        port = address[1] if len(address) > 1 else None
        try:
            port_i = int(port) if port is not None else None
        except (TypeError, ValueError):
            port_i = None
        return host, port_i
    return address, None


def install_guard(allowlist: list[str] | None = None) -> EgressGuard:
    """Install process-wide connect wrappers. Caller must ``close()``."""
    global _guard, _orig_connect, _orig_connect_ex, _orig_create, _orig_getaddrinfo
    with _lock:
        if _guard is not None:
            raise RuntimeError("sovereign egress guard already installed")
        guard = EgressGuard(allowlist)
        _orig_connect = socket.socket.connect
        _orig_connect_ex = socket.socket.connect_ex
        _orig_create = socket.create_connection
        _orig_getaddrinfo = socket.getaddrinfo

        def _connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
            if self.family in {socket.AF_INET, socket.AF_INET6}:
                guard.check(address)
            return _orig_connect(self, address, *args, **kwargs)

        def _connect_ex(self: socket.socket, address: Any) -> int:
            if self.family in {socket.AF_INET, socket.AF_INET6}:
                guard.check(address)
            return _orig_connect_ex(self, address)

        def _create(*args: Any, **kwargs: Any) -> Any:
            address = args[0] if args else kwargs.get("address")
            guard.check(address)
            return _orig_create(*args, **kwargs)

        def _gai(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            if host:
                guard.dns.append({"host": str(host), "at": utc_stamp(), "event": "getaddrinfo"})
            return _orig_getaddrinfo(host, port, *args, **kwargs)

        socket.socket.connect = _connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
        socket.create_connection = _create  # type: ignore[assignment]
        socket.getaddrinfo = _gai  # type: ignore[assignment]
        _guard = guard
        return guard


def _uninstall(guard: EgressGuard) -> None:
    global _guard, _orig_connect, _orig_connect_ex, _orig_create, _orig_getaddrinfo
    if _guard is not guard:
        return
    if _orig_connect is not None:
        socket.socket.connect = _orig_connect
    if _orig_connect_ex is not None:
        socket.socket.connect_ex = _orig_connect_ex
    if _orig_create is not None:
        socket.create_connection = _orig_create
    if _orig_getaddrinfo is not None:
        socket.getaddrinfo = _orig_getaddrinfo
    _orig_connect = None
    _orig_connect_ex = None
    _orig_create = None
    _orig_getaddrinfo = None
    _guard = None


def active_guard() -> EgressGuard | None:
    return _guard
