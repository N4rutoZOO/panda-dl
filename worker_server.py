import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

app = FastAPI(title="panda-dl-worker")

TOKEN = os.getenv("PANDA_WORKER_TOKEN", "").strip()
PROFILE = os.getenv("PANDA_CHROME_PROFILE", "/home/gbeerus489/chrome-profile").strip()
PORT = int(os.getenv("PANDA_WORKER_PORT", "8865"))
PLAYLIST_LIMIT = max(1, min(int(os.getenv("PANDA_PLAYLIST_LIMIT", "100")), 200))
JOB_TTL = max(600, min(int(os.getenv("PANDA_WORKER_JOB_TTL", "3600")), 21600))
WORKERS = max(1, min(int(os.getenv("PANDA_WORKER_JOBS", "1")), 2))
FRAGMENTS = max(1, min(int(os.getenv("PANDA_WORKER_FRAGMENTS", "2")), 4))

YTDLP = os.getenv("PANDA_YTDLP_BIN", "/opt/panda-dl-worker/venv/bin/yt-dlp")

JOBS = {}
LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="panda-dl-worker")


def _youtube_url(value):
    value = str(value or "").strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
    if parsed.scheme not in {"http", "https"} or (host not in allowed and not host.endswith(".youtube.com")):
        raise ValueError("URL YouTube invalide")
    return value


def _playlist_id(url):
    try:
        return (parse_qs(urlparse(url).query).get("list") or [None])[0]
    except Exception:
        return None


def _playlist_url(url):
    pid = _playlist_id(url)
    return f"https://www.youtube.com/playlist?list={pid}" if pid else url


def _auth(authorization):
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Worker token non configuré")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _deno_flags():
    candidates = [
        shutil.which("deno"),
        "/home/gbeerus489/.deno/bin/deno",
        "/usr/local/bin/deno",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return ["--js-runtimes", f"deno:{path}"]
    return []


def _browser_flags():
    return [
        "--cookies-from-browser", f"chromium:{PROFILE}",
        *_deno_flags(),
        "--retries", "10",
        "--fragment-retries", "10",
        "--extractor-retries", "3",
        "--socket-timeout", "20",
        "--concurrent-fragments", str(FRAGMENTS),
    ]


def _update(job_id, **values):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(values)
        job["updated"] = time.time()


def _get(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _cancelled(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def _cleanup():
    now = time.time()
    doomed = []
    with LOCK:
        for job_id, job in list(JOBS.items()):
            if job.get("status") in {"ready", "error", "cancelled"} and now - job.get("updated", now) > JOB_TTL:
                doomed.append(job.get("workdir"))
                del JOBS[job_id]
    for path in doomed:
        if path:
            shutil.rmtree(path, ignore_errors=True)


def _progress(raw):
    try:
        return max(0.0, min(100.0, float(str(raw or "0").strip().replace("%", ""))))
    except Exception:
        return 0.0


def _selector(quality):
    if quality == "best":
        return "bv*+ba/b"
    try:
        height = max(144, min(int(quality), 4320))
    except Exception:
        height = 1080
    return f"bv*[height<={height}]+ba/b[height<={height}]"


def _run_job(job_id, payload):
    workdir = tempfile.mkdtemp(prefix=f"panda_dl_worker_{job_id[:8]}_")
    _update(job_id, status="running", stage="preparing", progress=2, message="Préparation yt-dlp", workdir=workdir)
    proc = None
    try:
        url = _youtube_url(payload.get("url"))
        mode = "audio" if payload.get("mode") == "audio" else "video"
        quality = str(payload.get("quality") or "best")
        playlist_mode = bool(payload.get("playlist_mode"))
        selected = payload.get("selected_playlist") or []

        cmd = [
            YTDLP,
            *_browser_flags(),
            "--newline",
            "--progress",
            "--embed-metadata",
            "--write-info-json",
            "--windows-filenames",
            "--trim-filenames", "140",
            "--progress-template",
            "download:PDL|%(info.playlist_index)s|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        ]

        if playlist_mode:
            one_based = sorted({int(x) + 1 for x in selected if 0 <= int(x) < PLAYLIST_LIMIT})
            if not one_based:
                raise RuntimeError("Aucune vidéo sélectionnée")
            url = _playlist_url(url)
            cmd += [
                "--yes-playlist",
                "--playlist-items", ",".join(str(x) for x in one_based),
                "--playlist-end", str(PLAYLIST_LIMIT),
                "-o", os.path.join(workdir, "%(playlist_index)03d - %(title)s.%(ext)s"),
            ]
            position_map = {idx: pos for pos, idx in enumerate(one_based, 1)}
            total_items = len(one_based)
        else:
            cmd += ["--no-playlist", "-o", os.path.join(workdir, "%(id)s.%(ext)s")]
            position_map = {}
            total_items = 1

        if mode == "audio":
            cmd += ["-f", "bestaudio/best"]
        else:
            cmd += ["-f", _selector(quality), "--merge-output-format", "mp4"]

        cmd.append(url)
        _update(job_id, stage="downloading", progress=5, message="Session Chromium · yt-dlp")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        tail = []
        while True:
            if _cancelled(job_id):
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise InterruptedError("Job annulé")

            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                line = line.rstrip()
                tail.append(line)
                if len(tail) > 120:
                    tail = tail[-120:]
                if line.startswith("PDL|"):
                    parts = line.split("|", 4)
                    raw_index = parts[1] if len(parts) > 1 else ""
                    pct = _progress(parts[2] if len(parts) > 2 else "0")
                    speed = (parts[3] if len(parts) > 3 else "").strip()
                    eta = (parts[4] if len(parts) > 4 else "").strip()
                    if playlist_mode:
                        try:
                            position = position_map.get(int(raw_index), 1)
                        except Exception:
                            position = 1
                        overall = ((position - 1) + pct / 100.0) / max(1, total_items)
                        message = f"Vidéo {position}/{total_items} · {pct:.0f}%"
                    else:
                        overall = pct / 100.0
                        message = f"Téléchargement {pct:.0f}%"
                    if speed and speed not in {"N/A", "Unknown"}:
                        message += f" · {speed}"
                    if eta and eta not in {"N/A", "Unknown"}:
                        message += f" · ETA {eta}"
                    _update(job_id, stage="downloading", progress=5 + int(overall * 80), message=message)

            if proc.poll() is not None:
                if proc.stdout:
                    rest = proc.stdout.read()
                    if rest:
                        tail.extend(rest.splitlines()[-50:])
                break
            if not line:
                time.sleep(0.05)

        if proc.returncode != 0:
            raise RuntimeError("\n".join(tail[-60:])[-6000:] or "yt-dlp a échoué")

        candidates = []
        for base, _, files in os.walk(workdir):
            for name in files:
                path = os.path.join(base, name)
                if name.endswith((".part", ".ytdl")):
                    continue
                candidates.append(path)
        media = [p for p in candidates if not p.endswith((".json", ".zip"))]
        if not media:
            raise RuntimeError("yt-dlp n'a généré aucun média")

        bundle = os.path.join(workdir, "bundle.zip")
        _update(job_id, stage="packaging", progress=90, message="Préparation du transfert")
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for pos, path in enumerate(candidates, 1):
                if path == bundle:
                    continue
                archive.write(path, arcname=os.path.relpath(path, workdir))
                _update(job_id, stage="packaging", progress=90 + int((pos / max(1, len(candidates))) * 9), message=f"Bundle {pos}/{len(candidates)}")

        _update(job_id, status="ready", stage="ready", progress=100, message="Worker prêt", bundle=bundle, result_size=os.path.getsize(bundle))
    except InterruptedError:
        shutil.rmtree(workdir, ignore_errors=True)
        _update(job_id, status="cancelled", stage="cancelled", progress=0, message="Job annulé", workdir=None, bundle=None)
    except Exception as exc:
        message = str(exc)[-5000:]
        shutil.rmtree(workdir, ignore_errors=True)
        _update(job_id, status="error", stage="error", progress=0, message=message, error=message, workdir=None, bundle=None)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


@app.get("/health")
def health():
    _cleanup()
    return {
        "status": "ok",
        "service": "panda-dl-worker",
        "profile_exists": os.path.isdir(PROFILE),
        "token_configured": bool(TOKEN),
        "yt_dlp": os.path.isfile(YTDLP),
        "deno": bool(_deno_flags()),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "playlist_limit": PLAYLIST_LIMIT,
    }


@app.post("/info")
async def info(request: Request, authorization: str | None = Header(default=None)):
    _auth(authorization)
    payload = await request.json()
    url = _youtube_url(payload.get("url"))
    playlist_mode = bool(payload.get("playlist_mode"))
    cmd = [YTDLP, *_browser_flags(), "--dump-single-json", "--skip-download", "--no-warnings"]
    if playlist_mode:
        url = _playlist_url(url)
        cmd += ["--flat-playlist", "--yes-playlist", "--playlist-end", str(PLAYLIST_LIMIT)]
    else:
        cmd += ["--no-playlist"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180, check=False)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=(proc.stderr or proc.stdout or "Analyse YouTube impossible")[-5000:])
    try:
        data = json.loads(proc.stdout)
    except Exception:
        raise HTTPException(status_code=502, detail="Réponse yt-dlp invalide")
    if playlist_mode and not (data.get("entries") or []):
        raise HTTPException(status_code=502, detail="Aucune vidéo détectée dans cette playlist")
    return data


@app.post("/jobs")
async def create_job(request: Request, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _cleanup()
    payload = await request.json()
    try:
        payload["url"] = _youtube_url(payload.get("url"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job_id = uuid.uuid4().hex
    now = time.time()
    with LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "message": "En attente",
            "created": now,
            "updated": now,
            "cancel_requested": False,
            "workdir": None,
            "bundle": None,
            "error": None,
            "result_size": None,
        }
    EXECUTOR.submit(_run_job, job_id, payload)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _cleanup()
    job = _get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return {k: v for k, v in job.items() if k not in {"workdir", "bundle"}}


@app.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if not _get(job_id):
        raise HTTPException(status_code=404, detail="Job introuvable")
    _update(job_id, cancel_requested=True, message="Annulation demandée")
    return {"status": "cancelling"}


@app.get("/jobs/{job_id}/download")
def download(job_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    job = _get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job introuvable")
    if job.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Job non prêt")
    path = job.get("bundle")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="Bundle expiré")
    return FileResponse(path, media_type="application/zip", filename="panda-dl-worker.zip")
