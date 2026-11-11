"""Gazzetta Ufficiale print grab (port of the branch-53 ``GuStampaForm`` / ``GuStampaResolver`` / ``GuStampaGrab``).

THE TRICK OF 101 (measured on L.633/1941, 206 articles, and already in the 2016 PHP code of the historic system): up to 100
``articoliSelected`` the server renders only the selected articles; at the 101st it flips and renders the WHOLE act, whatever its length. So
every request carries 101 selections and one request per act is enough, without knowing the number of articles beforehand.

The Gazzetta also fails with HTTP 200: the body is then an error page of the application server (``<title>JBoss``). Pacing is 1.5 s between
requests over one connection (measured in the gate).
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from dp_it_normativa_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

# the current text (multivigente) at the requested date; the sibling /originario would serve the text as first printed
ENDPOINT = "https://www.gazzettaufficiale.it/atto/stampa/serie_generale/vigente"
JBOSS_MARKER = "<title>JBoss"
SELECTIONS = 101
PACING_SEC = 1.5
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_FLOOR = 500
HTTP_ERROR_FLOOR = 400


class JBossPageError(GrabError):
    """The Gazzetta answered HTTP 200 with the error page of its application server: not an act, and any earlier file of it is stale."""

    def __init__(self, message: str) -> None:
        super().__init__(TRANSIENT_ERROR, message)


def _field(name: str, value: str) -> str:
    return f"{name}={quote(value, safe='')}"


def _selection(article_id: int, code: str, data_gu: str) -> str:
    """One article as the form of the Gazzetta sends it: a nested query string, url-encoded again as the value of the field."""
    return (
        f"art.progressivo=0&art.idArticolo={article_id}&art.versione=1&art.codiceRedazionale={code}"
        f"&art.dataPubblicazioneGazzetta={data_gu}&art.idGruppo=0&art.idSottoArticolo1=10&art.idSottoArticolo=1&art.flagTipoArticolo=0"
    )


def build_body(code: str, data_gu: date, vigenza: date) -> str:
    """The POST body: the same coordinates always give the same body (pure, no network)."""
    published = data_gu.isoformat()
    parts = [
        _field("_noteSelected", "0"),
        _field("annoVigenza", str(vigenza.year)),
        _field("meseVigenza", str(vigenza.month)),
        _field("giornoVigenza", str(vigenza.day)),
        _field("creaHTML", "Visualizza"),
        _field("dataPubblicazioneGazzetta", published),
        _field("codiceRedazionale", code),
        *(_field("articoliSelected", _selection(i, code, published)) for i in range(1, SELECTIONS + 1)),
        _field("_articoliSelected", "1"),
    ]
    return "&".join(parts)


class GuClient:
    """One connection, one request per act, ``PACING_SEC`` between requests (never before the first)."""

    def __init__(self, client: httpx.Client, *, sleep: Callable[[float], None]) -> None:
        self._client = client
        self._sleep = sleep
        self._requested = False

    def fetch_print(self, label: str, code: str, data_gu: date, vigenza: date) -> bytes:
        """The HTML of the whole act; raises :class:`GrabError` (transient: network, 429, 5xx, empty body, JBoss; config: other 4xx)."""
        if self._requested:
            self._sleep(PACING_SEC)
        self._requested = True
        try:
            response = self._client.post(
                ENDPOINT,
                content=build_body(code, data_gu, vigenza).encode("ascii"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TransportError as error:
            msg = f"cannot reach the Gazzetta Ufficiale for {label}: {error}"
            raise GrabError(TRANSIENT_ERROR, msg) from error
        status = response.status_code
        if status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR_FLOOR:
            msg = f"the Gazzetta Ufficiale answered HTTP {status} for {label}"
            raise GrabError(TRANSIENT_ERROR, msg)
        if status >= HTTP_ERROR_FLOOR:
            msg = f"the Gazzetta Ufficiale answered HTTP {status} for {label}"
            raise GrabError(CONFIG_ERROR, msg)
        body = response.content
        if not body.strip():
            msg = f"the Gazzetta Ufficiale answered an empty body for {label}"
            raise GrabError(TRANSIENT_ERROR, msg)
        if JBOSS_MARKER in body.decode("utf-8", errors="replace"):
            msg = f"the Gazzetta Ufficiale served a JBoss error page with HTTP 200 for {label}"
            raise JBossPageError(msg)
        return body
