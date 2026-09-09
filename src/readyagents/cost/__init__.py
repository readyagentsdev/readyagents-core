"""TokenOps: price table, preflight estimate, spend meter, local ledger.

Interface freeze (TASK-05):

* Price table: exact model id, then provider prefix, then unpriced.
  An unknown model is never silently cost zero.
* SpendMeter: consult_before_call under a lock, shared across parallel
  branches, restored from run metadata on resume.
* Ledger entry: hash-chained JSONL (same seq/prev_hash/entry_hash as audit),
  one append per terminal run, labels redacted.
"""

from __future__ import annotations

from readyagents.cost.estimate import EstimateResult, estimate_workflow, estimate_workflow_file
from readyagents.cost.ledger import (
    SpendAggregate,
    append_spend,
    parse_labels,
    query_spend,
)
from readyagents.cost.meter import SpendMeter
from readyagents.cost.prices import PriceQuote, PriceTable, load_price_table, quote_model

__all__ = [
    "EstimateResult",
    "PriceQuote",
    "PriceTable",
    "SpendAggregate",
    "SpendMeter",
    "append_spend",
    "estimate_workflow",
    "estimate_workflow_file",
    "load_price_table",
    "parse_labels",
    "query_spend",
    "quote_model",
]
