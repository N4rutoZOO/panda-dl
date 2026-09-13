#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def main() -> None:
    py_files = sorted(ROOT.glob("*.py"))
    for path in py_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    backend = (ROOT / "playlist_sync.py").read_text(encoding="utf-8")
    app = (ROOT / "app_max.py").read_text(encoding="utf-8")
    ui = (ROOT / "sync_ui.html").read_text(encoding="utf-8")
    patch = (ROOT / "sync_v2_patch.py").read_text(encoding="utf-8")

    require("app_max:app" in docker, "Dockerfile doit lancer app_max:app")
    require('core.VERSION = "4.0-sync-v2"' in app, "version runtime V2 absente")
    require("install_sync_v2_patch" in app, "patch V2 non installé")

    required_routes = [
        '@router.get("/providers")',
        '@router.get("/playlists/{provider}")',
        '@router.post("/transfer")',
        '@router.post("/import/youtube")',
        '@router.get("/jobs/{job_id}")',
        '@router.get("/jobs/{job_id}/candidates/{index}")',
        '@router.post("/jobs/{job_id}/resolve/{index}")',
    ]
    for marker in required_routes:
        require(marker in backend, f"route absente: {marker}")

    for name in ("Deezer", "Apple Music", "TIDAL", "SoundCloud"):
        require(name in backend, f"connecteur à venir absent: {name}")
        require(name in ui or "coming_soon" in backend, f"UI connecteur absente: {name}")

    for marker in (
        "Dingo-dl Sync V2",
        "IMPORT FICHIER / TEXTE",
        'id="importText"',
        'id="importFile"',
        'id="exportJson"',
        'id="exportCsv"',
        'id="exportM3u"',
        'id="resolver"',
        "openResolver",
        "resolveCandidate",
    ):
        require(marker in ui, f"UI V2 incomplète: {marker}")

    require("#EXTINF:" in patch, "support M3U/M3U8 absent")

    manifest = {
        "python_files": len(py_files),
        "runtime": "4.0-sync-v2",
        "routes_checked": len(required_routes),
        "connectors_upcoming": ["Deezer", "Apple Music", "TIDAL", "SoundCloud"],
        "features": ["transfer", "youtube-import", "manual-resolution", "json-export", "csv-export", "m3u8-export", "history"],
    }
    print("OK", json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
