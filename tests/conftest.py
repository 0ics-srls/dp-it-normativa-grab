"""Puts the repo root and src/ on sys.path, forbids real network access and provides a scripted Normattiva BFF (httpx.MockTransport)."""
from __future__ import annotations

import io
import socket
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT, ROOT / "src", ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

BFF = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1/"
EXPORT = "https://export.normattiva.it/file-download/v1/download/TOKEN123"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: hermetic unit test")
    config.addinivalue_line("markers", "integration: needs a network")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "tests must not use the network"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)


def make_zip(entries: dict[str, str] | None = None) -> bytes:
    """A small valid export zip (one act directory with the original version)."""
    content = entries or {"LEGGE_20000727_212/2000-07-31_000G0265_ORIGINALE_V0.xml": "<akomaNtoso/>"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in content.items():
            archive.writestr(name, text)
    return buffer.getvalue()


@dataclass
class Bff:
    """Scripted Normattiva BFF. ``status`` answers are consumed one per poll, the last one repeats; ``downloads`` one per GET of the location."""

    nuova: httpx.Response = field(default_factory=lambda: httpx.Response(200, json="TOKEN123"))
    conferma: httpx.Response = field(default_factory=lambda: httpx.Response(200, json={}))
    status: list[httpx.Response] = field(
        default_factory=lambda: [httpx.Response(303, headers={"x-ipzs-location": EXPORT})]
    )
    downloads: list[httpx.Response] = field(default_factory=lambda: [httpx.Response(200, content=make_zip())])
    requests: list[httpx.Request] = field(default_factory=list)
    polls: int = 0
    gets: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.endswith("ricerca-asincrona/nuova-ricerca"):
            return self.nuova
        if url.endswith("ricerca-asincrona/conferma-ricerca"):
            return self.conferma
        if "ricerca-asincrona/check-status/" in url:
            answer = self.status[min(self.polls, len(self.status) - 1)]
            self.polls += 1
            return answer
        answer = self.downloads[min(self.gets, len(self.downloads) - 1)]
        self.gets += 1
        return answer

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls(self) -> list[str]:
        return [f"{r.method} {str(r.url).removeprefix(BFF)}" for r in self.requests]


@pytest.fixture
def bff() -> Bff:
    return Bff()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_sleep(sleeps: list[float]) -> Callable[[float], None]:
    return sleeps.append
