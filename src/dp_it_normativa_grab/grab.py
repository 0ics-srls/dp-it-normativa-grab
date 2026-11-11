"""Per-item grab into ``<dir>``: normattiva = export zip + ``.act.json``, gu = print ``<codice>.html`` + ``.meta.json``."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dp_it_normativa_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_normativa_grab.gu import GuClient, JBossPageError
from dp_it_normativa_grab.http import DEFAULT_ALLOWED_HOSTS, build_client
from dp_it_normativa_grab.normattiva import NormattivaClient

if TYPE_CHECKING:

    import httpx

Progress = Callable[[int, str], None]
KIND_NORMATTIVA = "normattiva"
KIND_GU = "gu"
_CODICE = re.compile(r"^[0-9A-Za-z]{1,20}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _utc_today() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class GrabDeps:
    """The ports of a run: transport (None = real network), sleep and today (clock)."""

    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    today: Callable[[], date] = field(default=_utc_today)


def safe_name(act_id: str) -> str:
    """File stem of an act id: spaces become hyphens, every other unsafe character an underscore."""
    return _UNSAFE.sub("_", act_id.replace(" ", "-"))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

def _validate_normattiva(key: str, meta: dict[str, Any]) -> None:
    if not isinstance(meta.get("denominazione"), str) or not _is_int(meta.get("anno")) or not _is_int(meta.get("numero")):
        raise GrabError(CONFIG_ERROR, f"item {key}: meta needs denominazione, integer anno and numero")


def _validate_gu(key: str, meta: dict[str, Any]) -> None:
    code = meta.get("codiceRedazionale")
    if not isinstance(code, str) or not _CODICE.match(code):
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.codiceRedazionale is required (letters and digits only)")
    for name in ("dataGU", "vigenza"):
        value = meta.get(name)
        if value is None and name == "vigenza":
            continue
        try:
            date.fromisoformat(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise GrabError(CONFIG_ERROR, f"item {key}: meta.{name} must be an ISO date YYYY-MM-DD") from None


def validate_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise GrabError(CONFIG_ERROR, "items must be a non-empty array")
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("key"):
            raise GrabError(CONFIG_ERROR, f"item needs a key: {entry!r}")
        meta = entry.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") not in (KIND_NORMATTIVA, KIND_GU):
            raise GrabError(CONFIG_ERROR, f"item {entry['key']}: meta.kind must be '{KIND_NORMATTIVA}' or '{KIND_GU}'")
        if not isinstance(meta.get("actId"), str) or not meta["actId"]:
            raise GrabError(CONFIG_ERROR, f"item {entry['key']}: meta.actId is required")
        (_validate_gu if meta["kind"] == KIND_GU else _validate_normattiva)(entry["key"], meta)
    return items


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _paths(base: Path, act_id: str) -> tuple[Path, Path]:
    stem = safe_name(act_id)
    return base / f"{stem}.zip", base / f"{stem}.act.json"


def _gu_paths(base: Path, code: str) -> tuple[Path, Path]:
    return base / f"{code}.html", base / f"{code}.meta.json"


def _planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    if entry["meta"]["kind"] == KIND_GU:
        html_path, meta_path = _gu_paths(base, entry["meta"]["codiceRedazionale"])
        return {"key": entry["key"], "path": str(html_path), "sidecar": str(meta_path)}
    zip_path, sidecar_path = _paths(base, entry["meta"]["actId"])
    return {"key": entry["key"], "path": str(zip_path), "sidecar": str(sidecar_path)}


def grab_item(entry: dict[str, Any], base: Path, client: NormattivaClient) -> dict[str, str]:  # kind normattiva
    meta = entry["meta"]
    data = client.fetch_export(meta["denominazione"], meta["anno"], meta["numero"])
    zip_path, sidecar_path = _paths(base, meta["actId"])
    _write_atomic(zip_path, data)
    sidecar = {
        "key": entry["key"],
        "actId": meta["actId"],
        "kind": KIND_NORMATTIVA,
        "denominazione": meta["denominazione"],
        "anno": meta["anno"],
        "numero": meta["numero"],
        "zip": zip_path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    _write_atomic(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"key": entry["key"], "path": str(zip_path), "sidecar": str(sidecar_path)}


_IDENTITY_FIELDS = ("denominazione", "anno", "numero", "dataProvvedimento", "titolo")


def grab_gu_item(entry: dict[str, Any], base: Path, client: GuClient, today: date) -> dict[str, str]:
    """The print of one act: ``<codice>.html`` and the sidecar ``<codice>.meta.json`` (coordinates, vigenza, identity if given)."""
    meta = entry["meta"]
    code = meta["codiceRedazionale"]
    vigenza = date.fromisoformat(meta["vigenza"]) if meta.get("vigenza") else today
    html_path, sidecar_path = _gu_paths(base, code)
    try:
        data = client.fetch_print(entry["key"], code, date.fromisoformat(meta["dataGU"]), vigenza)
    except JBossPageError:
        html_path.unlink(missing_ok=True)  # an error page is no act, and an earlier file of it is stale
        sidecar_path.unlink(missing_ok=True)
        raise
    _write_atomic(html_path, data)
    sidecar: dict[str, Any] = {
        "key": entry["key"],
        "actId": meta["actId"],
        "kind": KIND_GU,
        "codiceRedazionale": code,
        "dataGU": meta["dataGU"],
        "vigenza": vigenza.isoformat(),
        **{name: meta[name] for name in _IDENTITY_FIELDS if name in meta},
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    _write_atomic(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"key": entry["key"], "path": str(html_path), "sidecar": str(sidecar_path)}


def run_grab(params: dict[str, Any], deps: GrabDeps, on_progress: Progress) -> dict[str, Any]:
    """Tries every item; if any failed raises GrabError (config_error if any failure is a config error, else transient)."""
    items = validate_items(params.get("items"))
    directory = params.get("dir")
    if not isinstance(directory, str) or not directory:
        raise GrabError(CONFIG_ERROR, "dir is required")
    hosts = params.get("allowedHosts", list(DEFAULT_ALLOWED_HOSTS))
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise GrabError(CONFIG_ERROR, "allowedHosts must be an array of host names")
    base = Path(directory)

    if params.get("dryRun") is True:
        planned = [_planned(base, e) for e in items]
        return {"downloaded": 0, "files": planned, "dryRun": True}

    http_client = build_client(hosts, deps.transport)
    client = NormattivaClient(http_client, sleep=deps.sleep, today=deps.today)
    gu_client = GuClient(http_client, sleep=deps.sleep)
    files: list[dict[str, str]] = []
    failures: list[tuple[str, GrabError]] = []
    for index, entry in enumerate(items):
        try:
            if entry["meta"]["kind"] == KIND_GU:
                files.append(grab_gu_item(entry, base, gu_client, deps.today()))
            else:
                files.append(grab_item(entry, base, client))
        except GrabError as error:
            failures.append((entry["key"], error))
        on_progress(int((index + 1) * 100 / len(items)), f"{index + 1}/{len(items)} {entry['key']}")
    if failures:
        code = CONFIG_ERROR if any(error.code == CONFIG_ERROR for _, error in failures) else TRANSIENT_ERROR
        detail = "; ".join(f"{key} ({error})" for key, error in failures)
        msg = f"{len(failures)} of {len(items)} items failed: {detail}"
        raise GrabError(code, msg)
    return {"downloaded": len(files), "files": files}
