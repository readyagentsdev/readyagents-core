"""Local distillation: plan, consented dataset, pack-trained adapter, gated route."""

from readyagents.distill.adapters import catalog, remove_adapter, require_signed, show_adapter
from readyagents.distill.dataset import build_dataset, load_manifest
from readyagents.distill.evaluate import evaluate
from readyagents.distill.plan import plan
from readyagents.distill.promote import demote, promote, rescore_promoted
from readyagents.distill.schema import AdapterRecord, DatasetManifest, EvalComparison, PlanReport
from readyagents.distill.train import collect_tuner, train

__all__ = [
    "AdapterRecord",
    "DatasetManifest",
    "EvalComparison",
    "PlanReport",
    "build_dataset",
    "catalog",
    "collect_tuner",
    "demote",
    "evaluate",
    "load_manifest",
    "plan",
    "promote",
    "remove_adapter",
    "require_signed",
    "rescore_promoted",
    "show_adapter",
    "train",
]
