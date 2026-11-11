"""Normattiva BFF export dance (port of the legacy ``NormattivaBffClient`` export resolution and the empty-body regeneration).

nuova-ricerca (AKN, multivigente) -> conferma-ricerca -> poll check-status until 303 + ``x-ipzs-location`` -> GET that location (zip).
An empty body means the export was not ready: the whole dance is repeated (up to ``MAX_ATTEMPTS``).
"""
from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import httpx

from dp_it_normativa_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

BFF_BASE = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1/"
POLL_INTERVAL_SEC = 2.0
MAX_POLLS = 60
MAX_ATTEMPTS = 3
VARIANT_MULTIVIGENTE = "M"
HTTP_REDIRECT_SEE_OTHER = 303
HTTP_ERROR_FLOOR = 400
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_FLOOR = 500


def is_valid_zip(data: bytes) -> bool:
    """A payload that starts with the ``PK`` magic and opens as an archive."""
    if len(data) < 2 or data[:2] != b"PK":  # noqa: PLR2004 - the two magic bytes
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _ = archive.namelist()
    except zipfile.BadZipFile:
        return False
    return True


class NormattivaClient:
    def __init__(self, client: httpx.Client, *, sleep: Callable[[float], None], today: Callable[[], date]) -> None:
        self._client = client
        self._sleep = sleep
        self._today = today

    def fetch_export(self, denominazione: str, anno: int, numero: int, variante: str = VARIANT_MULTIVIGENTE) -> bytes:
        """The export zip of one act; raises :class:`GrabError` (transient / config) on failure."""
        label = f"{denominazione} {anno}/{numero}"
        for _ in range(MAX_ATTEMPTS):
            location = self._resolve_location(label, denominazione, anno, numero, variante)
            body = self._send(label, "GET", location).content
            if not body:
                continue  # the export was not generated yet: run the dance again
            if not is_valid_zip(body):
                msg = f"export of {label} is not a valid zip ({len(body)} bytes)"
                raise GrabError(TRANSIENT_ERROR, msg)
            return body
        msg = f"export of {label} stayed empty after {MAX_ATTEMPTS} attempts"
        raise GrabError(TRANSIENT_ERROR, msg)

    def _resolve_location(self, label: str, denominazione: str, anno: int, numero: int, variante: str) -> str:
        vigenza = self._today().isoformat()
        request = {
            "formato": "AKN",
            "richiestaExport": variante,
            "tipoRicerca": "A",
            "dataVigenza": vigenza,
            "parametriRicerca": {
                "denominazioneAtto": denominazione,
                "annoProvvedimento": anno,
                "numeroProvvedimento": numero,
                "vigenza": vigenza,
                "dataVigenza": vigenza,
                "limitaAnniVigenza": False,
            },
        }
        token = self._send(label, "POST", BFF_BASE + "ricerca-asincrona/nuova-ricerca", json=request).text.strip().strip('"')
        if not token:
            msg = f"nuova-ricerca of {label} returned no token"
            raise GrabError(TRANSIENT_ERROR, msg)
        self._send(label, "PUT", BFF_BASE + "ricerca-asincrona/conferma-ricerca", json={"token": token})

        for _ in range(MAX_POLLS):
            response = self._send(label, "GET", f"{BFF_BASE}ricerca-asincrona/check-status/{token}")
            location = response.headers.get("x-ipzs-location", "").strip()
            if response.status_code == HTTP_REDIRECT_SEE_OTHER and location:
                return str(location)
            self._sleep(POLL_INTERVAL_SEC)
        msg = f"check-status of {label} never returned 303 for token {token}"
        raise GrabError(TRANSIENT_ERROR, msg)

    def _send(self, label: str, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.TransportError as error:
            msg = f"cannot reach Normattiva for {label}: {error}"
            raise GrabError(TRANSIENT_ERROR, msg) from error
        status = response.status_code
        if status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR_FLOOR:
            msg = f"Normattiva answered HTTP {status} for {label}"
            raise GrabError(TRANSIENT_ERROR, msg)
        if status >= HTTP_ERROR_FLOOR:
            msg = f"Normattiva answered HTTP {status} for {label}"
            raise GrabError(CONFIG_ERROR, msg)
        return response
