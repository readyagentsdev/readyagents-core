"""In-process stub driver. Not a browser engine; used by tests and the example pack."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from readyagents.browser.protocol import DownloadResult, PageSnapshot
from readyagents.errors import BrowserExtract, BrowserRefused

BANK_STATEMENTS = "https://bank.example.com/statements"


def canned_bank_page() -> PageSnapshot:
    return PageSnapshot(
        url=BANK_STATEMENTS,
        title="Statements",
        text="Latest statement 2026-01-01 amount 12.00",
        fields={
            "table.statements": "ok",
            "a.download-latest": "Download",
            "td:nth-child(1)": "2026-01-01",
            "td:nth-child(2)": "12.00",
            "input#user": "",
            "input#pass": "",
        },
        rows=[{"td:nth-child(1)": "2026-01-01", "td:nth-child(2)": "12.00"}],
        links=["https://bank.example.com/statements/latest.pdf"],
        screenshot=b"PNG-PLACEHOLDER",
        download_bytes=b"%PDF-1.4 fake statement",
        download_name="statement.pdf",
        memory_bytes=1024,
    )


class FakeDriver:
    """Deterministic page map. Core policy still runs around every call."""

    constructions = 0

    def __init__(self, pages: Mapping[str, PageSnapshot] | None = None) -> None:
        type(self).constructions += 1
        self.pages: dict[str, PageSnapshot] = dict(pages or {})
        self.current: PageSnapshot | None = None
        self.credentials: dict[str, str] = {}
        self.filled_host: str | None = None
        self.scrubbed = False
        self.closed = False
        self.calls: list[tuple[Any, ...]] = []
        self.link_targets: dict[str, str] = {}
        self.action_ms = 0
        self.require_selectors = True

    @classmethod
    def canned(cls) -> FakeDriver:
        page = canned_bank_page()
        driver = cls({page.url: page, "https://bank.example.com/": page})
        driver.require_selectors = True
        return driver

    def navigate(self, url: str) -> PageSnapshot:
        self.calls.append(("navigate", url))
        page = self.pages.get(url)
        if page is None:
            page = PageSnapshot(url=url, elapsed_ms=self.action_ms)
        else:
            page = _copy(page)
            page.elapsed_ms = page.elapsed_ms or self.action_ms
        self.current = page
        return page

    def read(self, selector: str | None = None) -> PageSnapshot:
        self.calls.append(("read", selector))
        page = self._need_page()
        if selector:
            self._need_selector(selector)
        return page

    def click(self, selector: str) -> PageSnapshot:
        self.calls.append(("click", selector))
        target = self.link_targets.get(selector)
        if target:
            return self.navigate(target)
        page = self._need_page()
        if self.require_selectors:
            self._need_selector(selector)
        return page

    def type(self, selector: str, text: str) -> PageSnapshot:
        self.calls.append(("type", selector, text))
        page = self._need_page()
        if self.require_selectors:
            self._need_selector(selector)
        page.fields[selector] = text
        return page

    def select(self, selector: str, value: str) -> PageSnapshot:
        self.calls.append(("select", selector, value))
        page = self._need_page()
        if self.require_selectors:
            self._need_selector(selector)
        page.fields[selector] = value
        return page

    def wait_for(self, selector: str) -> PageSnapshot:
        self.calls.append(("wait_for", selector))
        page = self._need_page()
        self._need_selector(selector)
        return page

    def screenshot(self) -> bytes:
        self.calls.append(("screenshot",))
        page = self._need_page()
        return page.screenshot or b"PNG-PLACEHOLDER"

    def download(self, selector: str | None, url: str | None) -> DownloadResult:
        self.calls.append(("download", selector, url))
        page = self._need_page()
        if selector and self.require_selectors:
            self._need_selector(selector)
        data = page.download_bytes or b"file-bytes"
        return DownloadResult(name=page.download_name, data=data, url=url or page.url)

    def current_url(self) -> str:
        if self.current is None:
            return ""
        return self.current.url

    def fill_credentials(self, host: str, secrets: Mapping[str, str]) -> None:
        self.calls.append(("fill_credentials", host, list(secrets)))
        self.filled_host = host
        self.credentials = dict(secrets)
        self.scrubbed = False

    def scrub_credentials(self) -> None:
        tokens = [str(value) for value in self.credentials.values() if value]
        self.calls.append(("scrub_credentials",))
        self.credentials = {}
        self.scrubbed = True
        if not tokens:
            return
        pages = list(self.pages.values())
        if self.current is not None:
            pages.append(self.current)
        seen: set[int] = set()
        for page in pages:
            if id(page) in seen:
                continue
            seen.add(id(page))
            _scrub_snapshot(page, tokens)
        self.calls = [_scrub_call(call, tokens) for call in self.calls]

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True
        self.current = None

    def lookup(self, selector: str) -> str | None:
        page = self.current
        if page is None:
            return None
        if selector in page.fields:
            return page.fields[selector]
        return None

    def _need_page(self) -> PageSnapshot:
        if self.current is None:
            raise BrowserRefused("browser has no current page", reason="state")
        return self.current

    def _need_selector(self, selector: str) -> None:
        page = self._need_page()
        if selector in page.fields:
            return
        if any(selector in row for row in page.rows):
            return
        raise BrowserExtract(f"selector mismatch: {selector}")


def _scrub_snapshot(page: PageSnapshot, tokens: list[str]) -> None:
    for token in tokens:
        if not token:
            continue
        raw = token.encode("utf-8", errors="ignore")
        if raw and page.screenshot:
            page.screenshot = page.screenshot.replace(raw, b"[REDACTED]")
        if raw and page.download_bytes:
            page.download_bytes = page.download_bytes.replace(raw, b"[REDACTED]")
        for attr in ("text", "hidden_text", "alt_text", "title", "url", "download_name"):
            value = getattr(page, attr)
            if isinstance(value, str) and token in value:
                setattr(page, attr, value.replace(token, "[REDACTED]"))
        page.fields = {
            key: (val.replace(token, "[REDACTED]") if isinstance(val, str) else val)
            for key, val in page.fields.items()
        }
        page.rows = [
            {
                key: (val.replace(token, "[REDACTED]") if isinstance(val, str) else val)
                for key, val in row.items()
            }
            for row in page.rows
        ]


def _scrub_call(call: tuple[Any, ...], tokens: list[str]) -> tuple[Any, ...]:
    out: list[Any] = []
    for part in call:
        if isinstance(part, str):
            for token in tokens:
                part = part.replace(token, "[REDACTED]")
        elif isinstance(part, (bytes, bytearray)):
            data = bytes(part)
            for token in tokens:
                raw = token.encode("utf-8", errors="ignore")
                if raw:
                    data = data.replace(raw, b"[REDACTED]")
            part = data
        out.append(part)
    return tuple(out)


def _copy(page: PageSnapshot) -> PageSnapshot:
    return PageSnapshot(
        url=page.url,
        title=page.title,
        text=page.text,
        hidden_text=page.hidden_text,
        alt_text=page.alt_text,
        links=list(page.links),
        redirects=list(page.redirects),
        subresources=list(page.subresources),
        fields=dict(page.fields),
        rows=[dict(row) for row in page.rows],
        memory_bytes=page.memory_bytes,
        elapsed_ms=page.elapsed_ms,
        screenshot=page.screenshot,
        download_bytes=page.download_bytes,
        download_name=page.download_name,
    )
