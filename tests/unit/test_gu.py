"""Gazzetta Ufficiale print grab: the 101-selection POST, JBoss page detection, 1.5 s pacing, <codice>.html + <codice>.meta.json."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from dp_it_normativa_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_normativa_grab.grab import GrabDeps, run_grab
from dp_it_normativa_grab.gu import ENDPOINT, JBOSS_MARKER, PACING_SEC, SELECTIONS, build_body

PAGE = "<html><body><PRE>\nLEGGE 5 febbraio 1992, n. 104\n  Art. 1.\n  1. Testo. perché è\n</PRE></body></html>"
JBOSS = "<html><head><title>JBoss Web/7.5.7 - Error report</title></head><body>500</body></html>"
VIGENZA = date(2026, 11, 11)


def gu_item(code: str, data_gu: str = "1992-02-17", **extra: Any) -> dict[str, Any]:
    return {
        "key": f"gu:{code}|v1",
        "url": "https://www.gazzettaufficiale.it/",
        "meta": {"kind": "gu", "actId": f"gu:{code}", "version": "1", "dataGU": data_gu, "codiceRedazionale": code, **extra},
    }


@dataclass
class Gazzetta:
    """Scripted print endpoint: one answer per POST, the last repeats."""

    answers: list[httpx.Response] = field(default_factory=lambda: [httpx.Response(200, content=PAGE.encode("utf-8"))])
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answers[min(len(self.requests) - 1, len(self.answers) - 1)]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def deps(gazzetta: Gazzetta, sleeps: list[float]) -> GrabDeps:
    return GrabDeps(transport=gazzetta.transport, sleep=sleeps.append, today=lambda: VIGENZA)


def fields(body: str) -> list[tuple[str, str]]:
    return parse_qsl(body, keep_blank_values=True)


# --- the POST body ---------------------------------------------------------------------------------------------------


def test_the_body_has_101_selections_and_the_coordinates_of_the_act() -> None:
    body = build_body("092G0108", date(1992, 2, 17), VIGENZA)

    selections = [value for name, value in fields(body) if name == "articoliSelected"]
    assert SELECTIONS == 101
    assert len(selections) == 101
    values = dict(fields(body))
    assert values["creaHTML"] == "Visualizza"
    assert values["dataPubblicazioneGazzetta"] == "1992-02-17"
    assert values["codiceRedazionale"] == "092G0108"
    assert (values["annoVigenza"], values["meseVigenza"], values["giornoVigenza"]) == ("2026", "11", "11")
    assert values["_articoliSelected"] == "1"
    assert values["_noteSelected"] == "0"


def test_each_selection_is_a_url_encoded_query_string_with_ids_1_to_101() -> None:
    body = build_body("092G0108", date(1992, 2, 17), VIGENZA)

    raw = [f for f in body.split("&") if f.startswith("articoliSelected=")]
    assert len(raw) == 101
    assert all("%3D" in f and "%26" in f and "=" not in f.removeprefix("articoliSelected=") for f in raw)
    for article_id in (1, 50, 100, 101):
        assert f"art.idArticolo%3D{article_id}%26" in body
    assert "art.idArticolo%3D102%26" not in body
    inner = dict(parse_qsl(next(v for n, v in fields(body) if n == "articoliSelected")))
    assert inner["art.codiceRedazionale"] == "092G0108"
    assert inner["art.dataPubblicazioneGazzetta"] == "1992-02-17"


# --- the grab --------------------------------------------------------------------------------------------------------


def test_posts_the_form_and_writes_the_html_and_a_sidecar_named_by_the_codice(tmp_path: Path) -> None:
    gazzetta = Gazzetta()
    sleeps: list[float] = []

    result = run_grab({"items": [gu_item("092G0108")], "dir": str(tmp_path / "raw")}, deps(gazzetta, sleeps), lambda *_: None)

    (request,) = gazzetta.requests
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert dict(fields(request.content.decode("ascii")))["annoVigenza"] == "2026"
    html = tmp_path / "raw" / "092G0108.html"
    sidecar = tmp_path / "raw" / "092G0108.meta.json"
    assert html.read_bytes() == PAGE.encode("utf-8")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "key": "gu:092G0108|v1",
        "actId": "gu:092G0108",
        "kind": "gu",
        "codiceRedazionale": "092G0108",
        "dataGU": "1992-02-17",
        "vigenza": "2026-11-11",
        "bytes": len(PAGE.encode("utf-8")),
        "sha256": hashlib.sha256(PAGE.encode("utf-8")).hexdigest(),
    }
    assert result == {"downloaded": 1, "files": [{"key": "gu:092G0108|v1", "path": str(html), "sidecar": str(sidecar)}]}
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == ["092G0108.html", "092G0108.meta.json"]


def test_identity_known_to_the_item_is_copied_into_the_sidecar(tmp_path: Path) -> None:
    item = gu_item("092G0108", denominazione="LEGGE", anno=1992, numero=104, dataProvvedimento="1992-02-05", titolo="Legge-quadro")

    run_grab({"items": [item], "dir": str(tmp_path)}, deps(Gazzetta(), []), lambda *_: None)

    sidecar = json.loads((tmp_path / "092G0108.meta.json").read_text(encoding="utf-8"))
    assert (sidecar["denominazione"], sidecar["anno"], sidecar["numero"]) == ("LEGGE", 1992, 104)
    assert (sidecar["dataProvvedimento"], sidecar["titolo"]) == ("1992-02-05", "Legge-quadro")


def test_a_jboss_page_with_http_200_fails_the_item_and_removes_the_file(tmp_path: Path) -> None:
    (tmp_path / "092G0108.html").write_text("stale", encoding="utf-8")
    (tmp_path / "092G0108.meta.json").write_text("{}", encoding="utf-8")
    gazzetta = Gazzetta(answers=[httpx.Response(200, content=JBOSS.encode("utf-8"))])

    with pytest.raises(GrabError, match=r"gu:092G0108\|v1") as error:
        run_grab({"items": [gu_item("092G0108")], "dir": str(tmp_path)}, deps(gazzetta, []), lambda *_: None)

    assert JBOSS_MARKER == "<title>JBoss"
    assert error.value.code == TRANSIENT_ERROR
    assert list(tmp_path.iterdir()) == []


def test_the_good_acts_are_kept_when_one_fails(tmp_path: Path) -> None:
    gazzetta = Gazzetta(answers=[httpx.Response(200, content=PAGE.encode("utf-8")), httpx.Response(200, content=JBOSS.encode("utf-8"))])

    with pytest.raises(GrabError, match="1 of 2 items failed"):
        run_grab({"items": [gu_item("A1"), gu_item("B2")], "dir": str(tmp_path)}, deps(gazzetta, []), lambda *_: None)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["A1.html", "A1.meta.json"]


@pytest.mark.parametrize(("status", "code"), [(503, TRANSIENT_ERROR), (429, TRANSIENT_ERROR), (404, CONFIG_ERROR)])
def test_http_errors_map_to_protocol_codes(tmp_path: Path, status: int, code: str) -> None:
    gazzetta = Gazzetta(answers=[httpx.Response(status)])

    with pytest.raises(GrabError) as error:
        run_grab({"items": [gu_item("092G0108")], "dir": str(tmp_path)}, deps(gazzetta, []), lambda *_: None)

    assert error.value.code == code


def test_pacing_waits_between_requests_with_the_injected_sleeper_and_never_before_the_first(tmp_path: Path) -> None:
    gazzetta = Gazzetta()
    sleeps: list[float] = []

    run_grab({"items": [gu_item("A1"), gu_item("B2"), gu_item("C3")], "dir": str(tmp_path)}, deps(gazzetta, sleeps), lambda *_: None)

    assert PACING_SEC == 1.5
    assert sleeps == [1.5, 1.5]
    assert len(gazzetta.requests) == 3


def test_the_default_allowed_hosts_include_the_gazzetta_but_a_narrower_list_is_enforced(tmp_path: Path) -> None:
    run_grab({"items": [gu_item("A1")], "dir": str(tmp_path)}, deps(Gazzetta(), []), lambda *_: None)

    with pytest.raises(GrabError) as error:
        run_grab({"items": [gu_item("B2")], "dir": str(tmp_path), "allowedHosts": ["api.normattiva.it"]}, deps(Gazzetta(), []), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert "www.gazzettaufficiale.it" in str(error.value)


def test_dry_run_plans_the_files_without_a_request(tmp_path: Path) -> None:
    gazzetta = Gazzetta()

    result = run_grab({"items": [gu_item("A1")], "dir": str(tmp_path), "dryRun": True}, deps(gazzetta, []), lambda *_: None)

    assert gazzetta.requests == []
    assert result["dryRun"] is True
    assert result["files"] == [{"key": "gu:A1|v1", "path": str(tmp_path / "A1.html"), "sidecar": str(tmp_path / "A1.meta.json")}]


@pytest.mark.parametrize("meta", [{"kind": "gu", "actId": "gu:A1"}, {"kind": "gu", "actId": "gu:A1", "codiceRedazionale": "A1", "dataGU": "17/02/1992"},
                                  {"kind": "gu", "actId": "gu:A1", "codiceRedazionale": "../x", "dataGU": "1992-02-17"}])
def test_gu_items_need_a_safe_codice_and_an_iso_data(tmp_path: Path, meta: dict[str, Any]) -> None:
    with pytest.raises(GrabError) as error:
        run_grab({"items": [{"key": "k", "meta": meta}], "dir": str(tmp_path)}, deps(Gazzetta(), []), lambda *_: None)

    assert error.value.code == CONFIG_ERROR


def test_a_normattiva_and_a_gu_item_can_share_one_run(tmp_path: Path) -> None:
    from conftest import Bff  # noqa: PLC0415

    bff = Bff()
    sleeps: list[float] = []

    def route(request: httpx.Request) -> httpx.Response:
        return Gazzetta().handler(request) if request.url.host == "www.gazzettaufficiale.it" else bff.handler(request)

    mixed = GrabDeps(transport=httpx.MockTransport(route), sleep=sleeps.append, today=lambda: VIGENZA)
    normattiva = {
        "key": "normattiva:LEGGE:2000:212|v1", "url": "u",
        "meta": {"kind": "normattiva", "actId": "normattiva:LEGGE:2000:212", "version": "1", "denominazione": "LEGGE", "anno": 2000, "numero": 212},
    }

    result = run_grab({"items": [normattiva, gu_item("A1")], "dir": str(tmp_path)}, mixed, lambda *_: None)

    assert result["downloaded"] == 2
    assert (tmp_path / "normattiva_LEGGE_2000_212.zip").exists()
    assert (tmp_path / "A1.html").exists()
