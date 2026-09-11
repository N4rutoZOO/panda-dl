import os
from concurrent.futures import ThreadPoolExecutor

import app_full as core

# PANDA DL MAX runtime: keep a single Cloud Run process (jobs live in memory),
# but allow a small bounded pool of heavy jobs inside that process.
JOB_WORKERS = max(1, min(int(os.getenv("PANDA_JOB_WORKERS", "2")), 4))

try:
    core.EXECUTOR.shutdown(wait=False, cancel_futures=False)
except Exception:
    pass

core.EXECUTOR = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="panda-dl-max")
core.WORKER_POLL = max(0.4, min(float(os.getenv("PANDA_YT_WORKER_POLL", "0.5")), 5.0))
core.VERSION = "3.0-max"

app = core.app
