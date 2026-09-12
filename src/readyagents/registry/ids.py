"""Stable agent ids. Minted once; a copy is a new agent."""

from __future__ import annotations

import secrets


def mint_id() -> str:
    return "agt_" + secrets.token_hex(10)
