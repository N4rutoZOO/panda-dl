"""Dingo Sync V2 compatibility and durability patches."""


def install(module):
    from sync_persistence import STORE

    original_parse = module._parse_import

    def parse_import_v2(raw_text: str, limit: int):
        text = raw_text or ""
        if "#EXTINF:" in text:
            tracks = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line.startswith("#EXTINF:"):
                    continue
                meta = line.split(",", 1)[1].strip() if "," in line else ""
                if meta:
                    tracks.append(meta)
            if tracks:
                return original_parse("\n".join(tracks), limit)
        return original_parse(text, limit)

    module._parse_import = parse_import_v2

    original_transfer = module._run_transfer

    def run_transfer_v2(job_id: str, sid: str, req: dict):
        with module.SYNC_LOCK:
            job = module.SYNC_JOBS.get(job_id)
            if job:
                job["source"] = req.get("source")
                job["destination"] = req.get("destination")
        return original_transfer(job_id, sid, req)

    module._run_transfer = run_transfer_v2

    original_import = module._run_import

    def run_import_v2(job_id: str, sid: str, req: dict):
        with module.SYNC_LOCK:
            job = module.SYNC_JOBS.get(job_id)
            if job:
                job["source"] = "import"
                job["destination"] = "youtube"
        try:
            return original_import(job_id, sid, req)
        finally:
            try:
                with module.SYNC_LOCK:
                    snapshot = dict(module.SYNC_JOBS.get(job_id) or {})
                if snapshot:
                    STORE.save_history(sid, snapshot, {"source": "import", "destination": "youtube"})
            except Exception as exc:
                STORE.last_error = f"Historique import V2: {exc}"

    module._run_import = run_import_v2
    return module
