"""Proc-protocol loop and manifest of the grab class."""
from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx

import main
from conftest import ROOT, Bff
from dp_it_normativa_grab.grab import GrabDeps

ITEM = {
    "key": "normattiva:LEGGE:2000:212|v1",
    "url": "https://www.normattiva.it/x",
    "meta": {"kind": "normattiva", "actId": "normattiva:LEGGE:2000:212", "version": "1", "denominazione": "LEGGE", "anno": 2000, "numero": 212},
}


def lines_of(text: str, deps: GrabDeps | None = None) -> tuple[int, list[dict[str, Any]]]:
    out = io.StringIO()
    code = main.main(io.StringIO(text), out, deps)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def request(action: str, **params: Any) -> str:
    return json.dumps({"_action": action, "_meta": {"executionId": "e1"}, **params}) + "\n"


def test_execute_emits_progress_then_one_result(bff: Bff, tmp_path: Path) -> None:
    deps = GrabDeps(transport=bff.transport, sleep=lambda _s: None, today=lambda: date(2026, 11, 11))

    code, lines = lines_of(request("execute", items=[ITEM], dir=str(tmp_path)), deps)

    assert code == 0
    assert lines[-1]["type"] == "result"
    assert all(m["type"] == "progress" for m in lines[:-1])
    assert lines[-1]["data"]["downloaded"] == 1
    assert (tmp_path / "normattiva_LEGGE_2000_212.zip").exists()


def test_a_failing_item_prints_exactly_one_error_line_with_the_code(bff: Bff, tmp_path: Path) -> None:


    bff.nuova = httpx.Response(400)
    deps = GrabDeps(transport=bff.transport, sleep=lambda _s: None, today=lambda: date(2026, 11, 11))

    code, lines = lines_of(request("execute", items=[ITEM], dir=str(tmp_path)), deps)

    assert code != 0
    assert [m["type"] for m in lines if m["type"] == "error"] == ["error"]
    assert lines[-1]["code"] == "config_error"
    assert ITEM["key"] in lines[-1]["message"]


def test_a_bad_request_line_emits_one_error_line_and_the_loop_continues(bff: Bff, tmp_path: Path) -> None:
    deps = GrabDeps(transport=bff.transport, sleep=lambda _s: None, today=lambda: date(2026, 11, 11))

    code, lines = lines_of("not json\n" + request("execute", items=[ITEM], dir=str(tmp_path), dryRun=True), deps)

    assert lines[0]["type"] == "error"
    assert lines[0]["code"] == "config_error"
    assert lines[-1]["type"] == "result"
    assert code != 0


def test_unknown_action_is_a_config_error_and_terminate_ends_the_loop() -> None:
    _, lines = lines_of(request("bogus"))
    assert lines[0]["code"] == "config_error"

    _, after = lines_of(request("terminate") + request("bogus"))
    assert after == []


def test_stdout_carries_protocol_lines_only(bff: Bff, tmp_path: Path) -> None:
    deps = GrabDeps(transport=bff.transport, sleep=lambda _s: None, today=lambda: date(2026, 11, 11))
    out = io.StringIO()

    main.main(io.StringIO(request("execute", items=[ITEM], dir=str(tmp_path))), out, deps)

    for raw in out.getvalue().splitlines():
        assert json.loads(raw)["type"] in {"progress", "result", "error"}


def test_manifest_declares_execute_with_items_dir_and_dry_run() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "dp-it-normativa-grab"
    assert set(manifest["actions"]) == {"execute", "terminate"}
    properties = manifest["actions"]["execute"]["input"]["properties"]
    assert {"items", "dir", "dryRun", "allowedHosts"} <= set(properties)
    assert manifest["actions"]["execute"]["input"]["required"] == ["items", "dir"]
    assert manifest["execution"]["timeout"] >= 3600
