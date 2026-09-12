"""Optional stub browser pack: in-process FakeDriver, no Playwright.

Load with ``readyagents run ... --pack examples/packs/browser_pack.py``.
A real driver pack would wrap Playwright/Chromium behind the same protocol.
Core never imports a browser engine.
"""

from __future__ import annotations

from typing import Any

from readyagents.browser.fake import FakeDriver
from readyagents.browser.node import run_browser_node
from readyagents.packs import BasePack


class BrowserHandler:
    type_name = "browser"

    def execute(self, node: Any, state: Any, context: Any) -> Any:
        if getattr(context, "browser_driver", None) is None:
            context.browser_driver = FakeDriver.canned()
        return run_browser_node(node, state, context)


class BrowserPack(BasePack):
    name = "browser-stub"
    version = "0.1.0"

    def register_nodes(self) -> dict[str, BrowserHandler]:
        return {"browser": BrowserHandler()}


def get_pack() -> BrowserPack:
    return BrowserPack()
