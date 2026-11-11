"""Normattiva BFF export dance: nuova-ricerca -> conferma -> poll check-status -> download, against a scripted transport."""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from conftest import BFF, EXPORT, Bff, make_zip
from dp_it_normativa_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_normativa_grab.http import DEFAULT_ALLOWED_HOSTS, build_client
from dp_it_normativa_grab.normattiva import MAX_ATTEMPTS, MAX_POLLS, POLL_INTERVAL_SEC, NormattivaClient

TODAY = date(2026, 11, 11)


def client_for(bff: Bff, sleeps: list[float], allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS) -> NormattivaClient:
    return NormattivaClient(build_client(allowed, bff.transport), sleep=sleeps.append, today=lambda: TODAY)


def test_happy_path_runs_the_four_calls_in_order_and_returns_the_zip(bff: Bff, sleeps: list[float]) -> None:
    zip_bytes = bff.downloads[0].content

    result = client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert result == zip_bytes
    assert bff.calls() == [
        "POST ricerca-asincrona/nuova-ricerca",
        "PUT ricerca-asincrona/conferma-ricerca",
        "GET ricerca-asincrona/check-status/TOKEN123",
        f"GET {EXPORT}".replace(BFF, ""),
    ]
    assert sleeps == []


def test_nuova_ricerca_asks_for_the_multivigente_akn_export_of_the_act(bff: Bff, sleeps: list[float]) -> None:
    client_for(bff, sleeps).fetch_export("DECRETO-LEGGE", 2026, 157)

    nuova, conferma = bff.requests[0], bff.requests[1]
    assert json.loads(nuova.content) == {
        "formato": "AKN",
        "richiestaExport": "M",
        "tipoRicerca": "A",
        "dataVigenza": "2026-11-11",
        "parametriRicerca": {
            "denominazioneAtto": "DECRETO-LEGGE",
            "annoProvvedimento": 2026,
            "numeroProvvedimento": 157,
            "vigenza": "2026-11-11",
            "dataVigenza": "2026-11-11",
            "limitaAnniVigenza": False,
        },
    }
    assert json.loads(conferma.content) == {"token": "TOKEN123"}
    assert str(nuova.url).startswith(BFF)
    assert all("Mozilla/5.0" in r.headers["user-agent"] for r in bff.requests)


def test_the_303_may_arrive_on_a_later_poll_and_each_wait_is_two_seconds(bff: Bff, sleeps: list[float]) -> None:
    bff.status = [httpx.Response(200), httpx.Response(202), httpx.Response(303, headers={"x-ipzs-location": EXPORT})]

    client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert bff.polls == 3
    assert sleeps == [POLL_INTERVAL_SEC, POLL_INTERVAL_SEC]
    assert POLL_INTERVAL_SEC == 2.0


def test_a_status_that_never_answers_303_is_a_transient_error_after_the_poll_limit(bff: Bff, sleeps: list[float]) -> None:
    bff.status = [httpx.Response(200)]

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == TRANSIENT_ERROR
    assert "TOKEN123" in str(error.value)
    assert bff.polls == MAX_POLLS == 60
    assert not any(r.url.host == "export.normattiva.it" for r in bff.requests)


def test_a_303_without_the_location_header_keeps_polling(bff: Bff, sleeps: list[float]) -> None:
    bff.status = [httpx.Response(303), httpx.Response(303, headers={"x-ipzs-location": EXPORT})]

    client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert bff.polls == 2


def test_an_empty_body_regenerates_the_export_and_a_valid_zip_then_succeeds(bff: Bff, sleeps: list[float]) -> None:
    good = make_zip()
    bff.downloads = [httpx.Response(200, content=b""), httpx.Response(200, content=good)]

    result = client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert result == good
    assert sum(1 for c in bff.calls() if c == "POST ricerca-asincrona/nuova-ricerca") == 2


def test_an_export_that_stays_empty_fails_transiently_after_three_attempts(bff: Bff, sleeps: list[float]) -> None:
    bff.downloads = [httpx.Response(200, content=b"")]

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == TRANSIENT_ERROR
    assert MAX_ATTEMPTS == 3
    assert sum(1 for c in bff.calls() if c == "POST ricerca-asincrona/nuova-ricerca") == 3


@pytest.mark.parametrize("body", [b"<html>maintenance</html>", b"PK\x03\x04 truncated, not a zip", b"P"])
def test_a_body_that_is_not_a_valid_zip_is_a_transient_error_without_regeneration(bff: Bff, sleeps: list[float], body: bytes) -> None:
    bff.downloads = [httpx.Response(200, content=body)]

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == TRANSIENT_ERROR
    assert sum(1 for c in bff.calls() if c == "POST ricerca-asincrona/nuova-ricerca") == 1


def test_a_4xx_answer_of_nuova_ricerca_is_a_config_error_naming_the_act(bff: Bff, sleeps: list[float]) -> None:
    bff.nuova = httpx.Response(400, json={"message": "atto non trovato", "code": 1404})

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 99999)

    assert error.value.code == CONFIG_ERROR
    assert "LEGGE" in str(error.value)
    assert "99999" in str(error.value)
    assert "400" in str(error.value)


@pytest.mark.parametrize("stage", ["conferma", "download"])
def test_a_4xx_in_a_later_stage_is_also_a_config_error(bff: Bff, sleeps: list[float], stage: str) -> None:
    if stage == "conferma":
        bff.conferma = httpx.Response(404)
    else:
        bff.downloads = [httpx.Response(403)]

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == CONFIG_ERROR


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_throttling_and_server_errors_are_transient(bff: Bff, sleeps: list[float], status: int) -> None:
    bff.nuova = httpx.Response(status)

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == TRANSIENT_ERROR


def test_a_transport_failure_is_transient(sleeps: list[float]) -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = NormattivaClient(build_client(DEFAULT_ALLOWED_HOSTS, httpx.MockTransport(broken)), sleep=sleeps.append, today=lambda: TODAY)

    with pytest.raises(GrabError) as error:
        client.fetch_export("LEGGE", 2000, 212)

    assert error.value.code == TRANSIENT_ERROR
    assert "connection refused" in str(error.value)


def test_a_host_outside_the_allow_list_is_refused_before_any_request(bff: Bff, sleeps: list[float]) -> None:
    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps, allowed=("example.org",)).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == CONFIG_ERROR
    assert "api.normattiva.it" in str(error.value)
    assert bff.requests == []


def test_a_download_location_on_another_host_is_refused_before_it_is_fetched(bff: Bff, sleeps: list[float]) -> None:
    bff.status = [httpx.Response(303, headers={"x-ipzs-location": "https://evil.example.org/x.zip"})]

    with pytest.raises(GrabError) as error:
        client_for(bff, sleeps).fetch_export("LEGGE", 2000, 212)

    assert error.value.code == CONFIG_ERROR
    assert "evil.example.org" in str(error.value)
    assert bff.gets == 0
    assert not any(r.url.host == "evil.example.org" for r in bff.requests)


def test_redirects_are_never_followed(bff: Bff, sleeps: list[float]) -> None:
    client = build_client(DEFAULT_ALLOWED_HOSTS, bff.transport)

    assert client.follow_redirects is False
    assert client_for(bff, sleeps) is not None
