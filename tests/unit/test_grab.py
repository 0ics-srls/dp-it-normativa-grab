"""run_grab: per-item export, atomic zip + sidecar, dry run, error aggregation."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import Bff, make_zip
from dp_it_normativa_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_normativa_grab.grab import GrabDeps, run_grab, safe_name

LEGGE: dict[str, Any] = {
    "key": "normattiva:LEGGE:2000:212|v1",
    "url": "https://www.normattiva.it/ricerca/avanzata?denominazione=LEGGE&anno=2000&numero=212",
    "meta": {"kind": "normattiva", "actId": "normattiva:LEGGE:2000:212", "version": "1", "denominazione": "LEGGE", "anno": 2000, "numero": 212},
}
DL: dict[str, Any] = {
    "key": "normattiva:DECRETO-LEGGE:2026:157|v1",
    "url": "https://www.normattiva.it/ricerca/avanzata?denominazione=DECRETO-LEGGE&anno=2026&numero=157",
    "meta": {"kind": "normattiva", "actId": "normattiva:DECRETO-LEGGE:2026:157", "version": "1", "denominazione": "DECRETO-LEGGE",
             "anno": 2026, "numero": 157},
}


def deps(bff: Bff, sleeps: list[float]) -> GrabDeps:
    return GrabDeps(transport=bff.transport, sleep=sleeps.append, today=lambda: date(2026, 11, 11))


def progress_sink() -> tuple[list[tuple[int, str]], Any]:
    seen: list[tuple[int, str]] = []
    return seen, lambda percent, message: seen.append((percent, message))


def test_safe_name_keeps_the_act_id_readable_and_filesystem_safe() -> None:
    assert safe_name("normattiva:LEGGE:2000:212") == "normattiva_LEGGE_2000_212"
    assert safe_name("normattiva:DECRETO LEGISLATIVO:2003:196") == "normattiva_DECRETO-LEGISLATIVO_2003_196"
    assert safe_name("normattiva:DECRETO-LEGGE:2026:157") == "normattiva_DECRETO-LEGGE_2026_157"
    assert safe_name("gu:26G00178") == "gu_26G00178"
    assert safe_name("a/b\\c:..") == "a_b_c_.."  # path separators never survive


def test_writes_the_zip_and_a_sidecar_per_item(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    zip_bytes = bff.downloads[0].content
    seen, sink = progress_sink()

    result = run_grab({"items": [LEGGE], "dir": str(tmp_path / "raw")}, deps(bff, sleeps), sink)

    zip_path = tmp_path / "raw" / "normattiva_LEGGE_2000_212.zip"
    sidecar = tmp_path / "raw" / "normattiva_LEGGE_2000_212.act.json"
    assert zip_path.read_bytes() == zip_bytes
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "key": LEGGE["key"],
        "actId": "normattiva:LEGGE:2000:212",
        "kind": "normattiva",
        "denominazione": "LEGGE",
        "anno": 2000,
        "numero": 212,
        "zip": "normattiva_LEGGE_2000_212.zip",
        "bytes": len(zip_bytes),
        "sha256": hashlib.sha256(zip_bytes).hexdigest(),
    }
    assert result == {"downloaded": 1, "files": [{"key": LEGGE["key"], "path": str(zip_path), "sidecar": str(sidecar)}]}
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == ["normattiva_LEGGE_2000_212.act.json", "normattiva_LEGGE_2000_212.zip"]
    assert seen[-1][0] == 100


def test_several_items_are_fetched_in_order_with_progress(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    seen, sink = progress_sink()

    result = run_grab({"items": [LEGGE, DL], "dir": str(tmp_path)}, deps(bff, sleeps), sink)

    assert [f["key"] for f in result["files"]] == [LEGGE["key"], DL["key"]]
    assert [p for p, _ in seen] == [50, 100]


def test_an_existing_file_is_replaced_atomically(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    (tmp_path / "normattiva_LEGGE_2000_212.zip").write_bytes(b"old")

    run_grab({"items": [LEGGE], "dir": str(tmp_path)}, deps(bff, sleeps), lambda *_: None)

    assert (tmp_path / "normattiva_LEGGE_2000_212.zip").read_bytes() == bff.downloads[0].content
    assert not list(tmp_path.glob("*.tmp"))


def test_dry_run_makes_no_request_and_writes_nothing(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    result = run_grab({"items": [LEGGE, DL], "dir": str(tmp_path / "raw"), "dryRun": True}, deps(bff, sleeps), lambda *_: None)

    assert bff.requests == []
    assert not (tmp_path / "raw").exists()
    assert result["downloaded"] == 0
    assert [f["path"] for f in result["files"]] == [
        str(tmp_path / "raw" / "normattiva_LEGGE_2000_212.zip"),
        str(tmp_path / "raw" / "normattiva_DECRETO-LEGGE_2026_157.zip"),
    ]
    assert result["dryRun"] is True


def test_every_item_is_tried_and_a_config_failure_wins_over_transient_ones(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    bff.nuova = httpx.Response(400)

    with pytest.raises(GrabError) as error:
        run_grab({"items": [LEGGE, DL], "dir": str(tmp_path)}, deps(bff, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert LEGGE["key"] in str(error.value)
    assert DL["key"] in str(error.value)
    assert [r.method for r in bff.requests].count("POST") == 2


def test_transient_failures_alone_give_a_transient_error_and_good_items_are_kept(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    bff.downloads = [httpx.Response(200, content=make_zip()), httpx.Response(200, content=b"<html>down</html>")]

    with pytest.raises(GrabError) as error:
        run_grab({"items": [LEGGE, DL], "dir": str(tmp_path)}, deps(bff, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert DL["key"] in str(error.value)
    assert (tmp_path / "normattiva_LEGGE_2000_212.zip").exists()
    assert not (tmp_path / "normattiva_DECRETO-LEGGE_2026_157.zip").exists()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"items": [], "dir": "x"},
        {"items": "x", "dir": "x"},
        {"items": [LEGGE]},
        {"items": [{"key": "k"}], "dir": "x"},
        {"items": [{**LEGGE, "meta": {"kind": "normattiva", "denominazione": "LEGGE", "anno": 2000}}], "dir": "x"},
        {"items": [{**LEGGE, "meta": {**LEGGE["meta"], "kind": "other"}}], "dir": "x"},
        {"items": [{**LEGGE, "meta": {**LEGGE["meta"], "anno": "2000"}}], "dir": "x"},
        {"items": [LEGGE], "dir": "x", "allowedHosts": "api.normattiva.it"},
    ],
)
def test_bad_parameters_are_config_errors_before_any_request(bff: Bff, sleeps: list[float], params: dict[str, Any]) -> None:
    with pytest.raises(GrabError) as error:
        run_grab(params, deps(bff, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert bff.requests == []


def test_allowed_hosts_can_be_set_by_the_step(bff: Bff, sleeps: list[float], tmp_path: Path) -> None:
    with pytest.raises(GrabError) as error:
        run_grab({"items": [LEGGE], "dir": str(tmp_path), "allowedHosts": ["api.normattiva.it"]}, deps(bff, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert "export.normattiva.it" in str(error.value)
