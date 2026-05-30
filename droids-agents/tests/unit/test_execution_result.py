"""interpret_result — classify agentspan results into Outcomes."""

from __future__ import annotations

from types import SimpleNamespace

from droids_agents.execution import interpret_result


def test_ok_result() -> None:
    res = SimpleNamespace(execution_id="exec_1", output={"k": "v"})
    out = interpret_result(res, dry_run=False)
    assert out.kind == "ok"
    assert out.exec_id == "exec_1"
    assert out.output == {"k": "v"}


def test_dry_run_result() -> None:
    res = SimpleNamespace(execution_id="exec_2", output={"k": "v"})
    out = interpret_result(res, dry_run=True)
    assert out.kind == "dry_run"


def test_dict_shaped_result() -> None:
    res = {"exec_id": "exec_5", "output": {"a": 1}}
    out = interpret_result(res, dry_run=False)
    assert out.kind == "ok"
    assert out.exec_id == "exec_5"


def test_pydantic_output_is_dumped() -> None:
    class _Out:
        def model_dump(self):
            return {"dumped": True}

    res = SimpleNamespace(execution_id="e", output=_Out())
    out = interpret_result(res, dry_run=False)
    assert out.output == {"dumped": True}
