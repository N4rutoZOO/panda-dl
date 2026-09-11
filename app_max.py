import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.responses import HTMLResponse

import app_full as core
import playlist_sync as playlist_sync_core
from sync_persistence import install as install_sync_persistence

JOB_WORKERS = max(1, min(int(os.getenv("PANDA_JOB_WORKERS", "2")), 4))

try:
    core.EXECUTOR.shutdown(wait=False, cancel_futures=False)
except Exception:
    pass

core.EXECUTOR = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="panda-dl-max")
core.WORKER_POLL = max(0.4, min(float(os.getenv("PANDA_YT_WORKER_POLL", "0.5")), 5.0))
core.VERSION = "4.0-sync-v2"

sync_storage_router = install_sync_persistence(playlist_sync_core)
sync_router = playlist_sync_core.router

app = core.app
app.include_router(sync_router)
app.include_router(sync_storage_router)


@app.get("/sync", response_class=HTMLResponse)
def sync_ui():
    path = os.path.join(os.path.dirname(__file__), "sync_ui.html")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()
