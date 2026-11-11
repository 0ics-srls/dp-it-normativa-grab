"""The class manifest accepts Gazzetta Ufficiale items next to Normattiva ones."""
import json
from pathlib import Path
from typing import Any

MANIFEST = Path(__file__).resolve().parent.parent.parent / "manifest.json"


def test_the_item_meta_kind_is_normattiva_or_gu() -> None:
    manifest: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    meta = manifest["actions"]["execute"]["input"]["properties"]["items"]["items"]["properties"]["meta"]

    assert meta["properties"]["kind"] == {"enum": ["normattiva", "gu"]}
    assert meta["required"] == ["kind", "actId"]
