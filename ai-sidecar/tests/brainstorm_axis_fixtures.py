"""Explicit valid wire fixtures; never translate arbitrary legacy goal prose."""
from creation.brainstorm_root_axes import RootAxisCompiler, load_root_axes_binding
from creation.brainstorm_stages import BrainstormStageCoordinator


def axis_plan_raw(stage="direction", identifiers=None):
    if identifiers is None:
        identifiers = ["narrative_perspective" if stage == "direction" else "information_granularity"]
    return {"axis_ids": list(identifiers)}


def axis_plan_manifest(stage="direction", identifiers=None, limits=None, brief_edits=None):
    return RootAxisCompiler(load_root_axes_binding()).compile_plan(axis_plan_raw(stage, identifiers),
        stage, BrainstormStageCoordinator.limits(limits), brief_edits or {})
