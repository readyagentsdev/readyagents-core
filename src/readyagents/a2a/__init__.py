"""A2A projection over the durable run record. Not a second state machine.

Remote content is untrusted. Delegation can exfiltrate. This is a served
mapping, not A2A certification.
"""

from readyagents.a2a.card import build_agent_card, card_digest, validate_card
from readyagents.a2a.mapping import map_run_to_task_state, validate_transition

__all__ = [
    "build_agent_card",
    "card_digest",
    "map_run_to_task_state",
    "validate_card",
    "validate_transition",
]
