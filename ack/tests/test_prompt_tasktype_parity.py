"""Keep orchestrator prompt task-type values aligned with the ACK contract (#1329)."""
from pathlib import Path

from ack.case_spec import TaskType

PROMPT = (
    Path(__file__).resolve().parents[2]
    / "engine"
    / "prompts"
    / "GX10_Orchestrator_SystemPrompt.md"
)
HANDOVER_KINDS = {"review", "docs"}


def _pipe_values(anchor: str) -> set[str]:
    line = next(
        line for line in PROMPT.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith(anchor)
    )
    values = line.split(":", 1)[1].strip().strip('",')
    return {value.strip().strip('"') for value in values.split("|")}


def test_prompt_task_type_values_match_ack_contract():
    valid = {task_type.value for task_type in TaskType}
    assert _pipe_values('"type":') <= valid
    assert _pipe_values("task:") <= valid | HANDOVER_KINDS
