"""interpret_result — classify agentspan results into Outcomes."""

from __future__ import annotations

from types import SimpleNamespace

from droids_agents.execution import interpret_result


def test_ok_result() -> None:
    res = SimpleNamespace(execution_id="exec_1", output={"k": "v"}, is_waiting=False)
    out = interpret_result(res, dry_run=False)
    assert out.kind == "ok"
    assert out.exec_id == "exec_1"
    assert out.output == {"k": "v"}


def test_dry_run_result() -> None:
    res = SimpleNamespace(execution_id="exec_2", output={"k": "v"}, is_waiting=False)
    out = interpret_result(res, dry_run=True)
    assert out.kind == "dry_run"


def test_hitl_result() -> None:
    res = SimpleNamespace(
        execution_id="exec_3",
        is_waiting=True,
        pending_approval={"tool_name": "gmail_send"},
    )
    out = interpret_result(res, dry_run=False)
    assert out.kind == "hitl"
    assert out.hitl == {"tool_name": "gmail_send"}


def test_cost_cap_result() -> None:
    res = SimpleNamespace(
        execution_id="exec_4",
        output={},
        is_waiting=False,
        termination_reason="TokenUsageTermination: budget hit",
    )
    out = interpret_result(res, dry_run=False)
    assert out.kind == "cost_cap"
    assert "token" in out.termination_reason.lower()


def test_dict_shaped_result() -> None:
    res = {"exec_id": "exec_5", "output": {"a": 1}, "is_waiting": False}
    out = interpret_result(res, dry_run=False)
    assert out.kind == "ok"
    assert out.exec_id == "exec_5"


def test_pydantic_output_is_dumped() -> None:
    class _Out:
        def model_dump(self):
            return {"dumped": True}

    res = SimpleNamespace(execution_id="e", output=_Out(), is_waiting=False)
    out = interpret_result(res, dry_run=False)
    assert out.output == {"dumped": True}
