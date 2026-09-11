import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

VERSION = "2.1-full-social"
WORKER_URL = os.getenv("PANDA_YT_WORKER_URL", "").strip().rstrip("/")
WORKER_TOKEN = os.getenv("PANDA_YT_WORKER_TOKEN", "").strip()
GOOGLE_CLIENT_ID = os.getenv("PANDA_GOOGLE_CLIENT_ID", "").strip()
SESSION_SECRET = os.getenv("PANDA_SESSION_SECRET", "dev-only-change-me").strip()
ALLOWED_EMAILS = {x.strip().lower() for x in os.getenv("PANDA_ALLOWED_GOOGLE_EMAILS", "").split(",") if x.strip()}
AUTH_REQUIRED = os.getenv("PANDA_AUTH_REQUIRED", "1").strip() not in {"0", "false", "False"}
COOKIE_SECURE = os.getenv("PANDA_COOKIE_SECURE", "1").strip() not in {"0", "false", "False"}
WORKER_POLL = max(0.5, min(float(os.getenv("PANDA_YT_WORKER_POLL", "0.8")), 5.0))
JOB_TTL = max(600, min(int(os.getenv("PANDA_JOB_TTL", "3600")), 21600))
PLAYLIST_LIMIT = max(1, min(int(os.getenv("PANDA_PLAYLIST_LIMIT", "100")), 200))
INFO_TTL = max(30, min(int(os.getenv("PANDA_INFO_TTL", "180")), 1800))

VIDEO_QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]
AUDIO_QUALITIES = ["320", "256", "192", "128"]

app = FastAPI(title="PANDA DL")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=COOKIE_SECURE, max_age=60 * 60 * 24 * 14)

JOBS = {}
JOB_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="panda-dl")
INFO_CACHE = {}
INFO_LOCK = threading.Lock()


def worker_enabled():
    return bool(WORKER_URL and WORKER_TOKEN)


def require_user(request: Request):
    user = request.session.get("user")
    if AUTH_REQUIRED and not user:
        raise HTTPException(status_code=401, detail="Connecte-toi avec Google")
    return user


def worker_json(method, path, payload=None, timeout=120):
    if not worker_enabled():
        raise RuntimeError("Worker média non configuré")
    body = None
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = UrlRequest(f"{WORKER_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = str(exc)
        raise RuntimeError(detail or str(exc)) from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Worker inaccessible: {exc}") from exc


def worker_download(path, destination, timeout=3600):
    req = UrlRequest(f"{WORKER_URL}{path}", headers={"Authorization": f"Bearer {WORKER_TOKEN}"}, method="GET")
    with urlopen(req, timeout=timeout) as response, open(destination, "wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def safe_extract(bundle, destination):
    root = os.path.abspath(destination)
    with zipfile.ZipFile(bundle) as archive:
        for member in archive.infolist():
            target = os.path.abspath(os.path.join(root, member.filename))
            if target != root and not target.startswith(root + os.sep):
                raise RuntimeError("Archive worker invalide")
        archive.extractall(root)


def validate_url(value):
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Colle une URL valide")
    return value


def detect_platform(url):
    host = (urlparse(url).hostname or "").lower()
    if "youtube" in host or host == "youtu.be": return "youtube"
    if "instagram" in host: return "instagram"
    if "tiktok" in host: return "tiktok"
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}: return "x-twitter"
    if "facebook" in host or host == "fb.watch": return "facebook"
    if "reddit" in host or host == "redd.it": return "reddit"
    if "vimeo" in host: return "vimeo"
    if "twitch" in host: return "twitch"
    if "soundcloud" in host: return "soundcloud"
    if "pinterest" in host: return "pinterest"
    return host or "web"


def playlist_id(url):
    try:
        return (parse_qs(urlparse(url).query).get("list") or [None])[0]
    except Exception:
        return None


def is_playlist(url):
    return bool(playlist_id(url))


def parse_list(raw):
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_dict(raw):
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clean_name(value, fallback="PANDA DL"):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or fallback))
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value[:150] or fallback


def update_job(job_id, **values):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(values)
        job["updated"] = time.time()


def get_job(job_id):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def cancelled(job_id):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def cleanup_jobs():
    now = time.time()
    workdirs = []
    with JOB_LOCK:
        for job_id, job in list(JOBS.items()):
            if job.get("status") in {"ready", "error", "cancelled"} and now - job.get("updated", now) > JOB_TTL:
                workdirs.append(job.get("workdir"))
                del JOBS[job_id]
    for workdir in workdirs:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def media_files(root):
    ignored = (".json", ".zip", ".part", ".ytdl", ".tmp")
    files = []
    for base, _, names in os.walk(root):
        for name in names:
            if name.lower().endswith(ignored):
                continue
            path = os.path.join(base, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                files.append(path)
    return sorted(files)


def info_files(root):
    found = []
    for base, _, names in os.walk(root):
        for name in names:
            if name.endswith(".info.json"):
                found.append(os.path.join(base, name))
    return sorted(found)


def load_info(root):
    files = info_files(root)
    if not files:
        return {}
    try:
        with open(files[0], "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def extract_chapters(info):
    result = []
    for idx, chapter in enumerate(info.get("chapters") or [], 1):
        try:
            start = float(chapter.get("start_time")); end = float(chapter.get("end_time"))
        except Exception:
            continue
        if end > start:
            result.append({"number": idx, "title": clean_name(chapter.get("title"), f"Track {idx:02d}"), "start": start, "end": end, "duration": end - start})
    if result:
        return result
    points = []
    pattern = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*(?:[-–—|:]\s*)?(.*)\s*$")
    for line in str(info.get("description") or "").splitlines():
        match = pattern.match(line)
        if not match: continue
        sec = int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        if points and sec <= points[-1][0]: continue
        points.append((sec, match.group(4).strip()))
    if len(points) < 2: return []
    duration = info.get("duration")
    for idx, (start, title) in enumerate(points, 1):
        end = points[idx][0] if idx < len(points) else duration
        try: end = float(end)
        except Exception: continue
        if end > start:
            result.append({"number": idx, "title": clean_name(title, f"Track {idx:02d}"), "start": float(start), "end": end, "duration": end - start})
    return result


def quality_sizes(raw):
    audio = [f.get("filesize") or f.get("filesize_approx") for f in raw.get("formats", []) if f.get("acodec") != "none" and f.get("vcodec") == "none"]
    audio_size = max([x for x in audio if isinstance(x, (int, float))] or [0])
    sizes = {}
    for fmt in raw.get("formats", []):
        height = fmt.get("height"); size = fmt.get("filesize") or fmt.get("filesize_approx")
        if not height or fmt.get("vcodec") == "none" or not isinstance(size, (int, float)): continue
        key = str(int(height)); sizes[key] = max(int(size + audio_size), sizes.get(key, 0))
    if sizes: sizes["best"] = max(sizes.values())
    return sizes


def convert_mp3(source, output, bitrate, metadata=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-vn", "-map_metadata", "0", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-id3v2_version", "3"]
    for key, value in (metadata or {}).items():
        if value: cmd += ["-metadata", f"{key}={value}"]
    cmd.append(output)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
    if proc.returncode != 0: raise RuntimeError((proc.stderr or "Conversion MP3 impossible")[-2500:])


def ensure_mp4(source, output):
    if os.path.splitext(source)[1].lower() == ".mp4": return source
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-map", "0", "-c", "copy", "-movflags", "+faststart", output], capture_output=True, text=True, timeout=3600, check=False)
    if proc.returncode == 0: return output
    proc2 = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output], capture_output=True, text=True, timeout=3600, check=False)
    if proc2.returncode != 0: raise RuntimeError((proc2.stderr or proc.stderr or "Conversion MP4 impossible")[-2500:])
    return output


def split_tracks(job_id, source, info, selected, custom_titles, bitrate, workdir):
    chapters = extract_chapters(info)
    indexes = []
    for value in selected:
        try: idx = int(value)
        except Exception: continue
        if 0 <= idx < len(chapters): indexes.append(idx)
    indexes = sorted(set(indexes))
    if not indexes: raise RuntimeError("Sélectionne au moins une track")
    album = clean_name(info.get("title"), "Mix")
    artist = clean_name(info.get("uploader") or info.get("channel") or "", "")
    zip_path = os.path.join(workdir, "tracks.zip")
    manifest = []; listing = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for pos, idx in enumerate(indexes, 1):
            if cancelled(job_id): raise InterruptedError("Job annulé")
            chapter = chapters[idx]; number = int(chapter.get("number") or idx + 1)
            title = clean_name(custom_titles.get(str(idx)) or custom_titles.get(str(number)) or chapter.get("title"), f"Track {number:02d}")
            temp = os.path.join(workdir, f"track-{number:03d}.mp3")
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{float(chapter['start']):.3f}", "-i", source, "-t", f"{float(chapter['duration']):.3f}", "-vn", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-metadata", f"title={title}", "-metadata", f"track={number}", "-metadata", f"album={album}"]
            if artist: cmd += ["-metadata", f"artist={artist}"]
            cmd.append(temp)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
            if proc.returncode != 0: raise RuntimeError((proc.stderr or f"Découpage impossible: {title}")[-2500:])
            archive.write(temp, arcname=f"{number:02d} - {title}.mp3"); os.remove(temp)
            listing.append(f"{number:02d}. {title}"); manifest.append({"number": number, "title": title, "start": chapter["start"], "duration": chapter["duration"]})
            update_job(job_id, stage="splitting", progress=82 + int((pos / len(indexes)) * 15), message=f"Track {pos}/{len(indexes)}")
        archive.writestr("tracklist.txt", "\n".join(listing) + "\n")
        archive.writestr("metadata.json", json.dumps({"album": album, "artist": artist, "tracks": manifest}, ensure_ascii=False, indent=2))
    return zip_path


def run_job(job_id, payload):
    workdir = tempfile.mkdtemp(prefix=f"panda_dl_{job_id[:8]}_")
    update_job(job_id, status="running", stage="connecting", progress=3, message="Connexion au worker", workdir=workdir)
    remote_id = None
    try:
        remote = worker_json("POST", "/jobs", {"url": payload["url"], "mode": payload["mode"], "quality": payload["quality"] if payload["mode"] == "video" else "best", "playlist_mode": bool(payload.get("playlist_mode")), "selected_playlist": payload.get("selected_playlist") or []}, timeout=30)
        remote_id = remote.get("job_id")
        if not remote_id: raise RuntimeError("Le worker n'a pas créé le job")
        update_job(job_id, remote_id=remote_id)
        while True:
            if cancelled(job_id):
                try: worker_json("POST", f"/jobs/{remote_id}/cancel", {}, timeout=10)
                except Exception: pass
                raise InterruptedError("Job annulé")
            state = worker_json("GET", f"/jobs/{remote_id}", timeout=30)
            status = state.get("status"); remote_progress = int(state.get("progress") or 0)
            update_job(job_id, status="running", stage="download", progress=min(78, 4 + int(max(0, min(remote_progress, 100)) * 0.72)), message=state.get("message") or "Téléchargement")
            if status == "ready": break
            if status == "error": raise RuntimeError(state.get("error") or state.get("message") or "Le moteur a échoué")
            if status == "cancelled": raise InterruptedError("Job annulé")
            time.sleep(WORKER_POLL)
        bundle = os.path.join(workdir, "worker-bundle.zip")
        update_job(job_id, stage="transfer", progress=79, message="Transfert depuis le worker")
        worker_download(f"/jobs/{remote_id}/download", bundle); safe_extract(bundle, workdir)
        try: os.remove(bundle)
        except OSError: pass
        files = media_files(workdir)
        if not files: raise RuntimeError("Aucun média reçu")

        mode = payload["mode"]
        if mode == "image":
            if len(files) == 1:
                final = files[0]; filename = clean_name(os.path.basename(final)); media_type = mimetypes.guess_type(final)[0] or "application/octet-stream"
            else:
                final = os.path.join(workdir, "images.zip")
                with zipfile.ZipFile(final, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for pos, path in enumerate(files, 1):
                        archive.write(path, arcname=os.path.basename(path)); update_job(job_id, stage="packaging", progress=84 + int((pos / len(files)) * 15), message=f"Photo {pos}/{len(files)}")
                filename = "PANDA-DL-images.zip"; media_type = "application/zip"
            update_job(job_id, status="ready", stage="ready", progress=100, message=f"Images prêtes · {len(files)} fichier(s)", result_path=final, filename=filename, media_type=media_type, result_size=os.path.getsize(final)); return

        if payload.get("playlist_mode"):
            final_files = []
            if mode == "video":
                for pos, source in enumerate(files, 1):
                    final = source if os.path.splitext(source)[1].lower() == ".mp4" else ensure_mp4(source, os.path.join(workdir, f"playlist-{pos:03d}.mp4"))
                    final_files.append(final); update_job(job_id, stage="processing", progress=82 + int((pos / len(files)) * 9), message=f"Vidéo {pos}/{len(files)}")
                ext = "mp4"
            else:
                for pos, source in enumerate(files, 1):
                    final = os.path.join(workdir, f"playlist-{pos:03d}.mp3"); convert_mp3(source, final, payload["audio_quality"]); final_files.append(final); update_job(job_id, stage="processing", progress=82 + int((pos / len(files)) * 9), message=f"Audio {pos}/{len(files)}")
                ext = "mp3"
            final = os.path.join(workdir, f"playlist-{ext}.zip")
            with zipfile.ZipFile(final, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for pos, path in enumerate(final_files, 1): archive.write(path, arcname=os.path.basename(path))
            update_job(job_id, status="ready", stage="ready", progress=100, message=f"Playlist prête · {len(final_files)} fichiers", result_path=final, filename=f"PANDA-DL-playlist-{ext}.zip", media_type="application/zip", result_size=os.path.getsize(final)); return

        source = max(files, key=os.path.getsize); info = load_info(workdir); title = clean_name(info.get("title"), "PANDA DL")
        if mode == "audio":
            if payload.get("split_tracks"):
                final = split_tracks(job_id, source, info, payload.get("selected_tracks") or [], payload.get("track_titles") or {}, payload["audio_quality"], workdir); filename = f"{title} - tracks.zip"; media_type = "application/zip"
            else:
                final = os.path.join(workdir, "final.mp3"); update_job(job_id, stage="convert", progress=87, message=f"Conversion MP3 · {payload['audio_quality']} kbps"); convert_mp3(source, final, payload["audio_quality"], {"title": info.get("title"), "artist": info.get("artist") or info.get("uploader") or info.get("channel")}); filename = f"{title}.mp3"; media_type = "audio/mpeg"
        else:
            update_job(job_id, stage="processing", progress=88, message="Finalisation MP4"); final = ensure_mp4(source, os.path.join(workdir, "final.mp4")); filename = f"{title}.mp4"; media_type = "video/mp4"
        update_job(job_id, status="ready", stage="ready", progress=100, message="Prêt à télécharger", result_path=final, filename=filename, media_type=media_type, result_size=os.path.getsize(final))
    except InterruptedError:
        shutil.rmtree(workdir, ignore_errors=True); update_job(job_id, status="cancelled", stage="cancelled", progress=0, message="Job annulé", result_path=None, workdir=None)
    except Exception as exc:
        update_job(job_id, status="error", stage="error", progress=0, message=str(exc), error=str(exc))


def cached_info(url, mode):
    key = f"{mode}|{url}"; now = time.time()
    with INFO_LOCK:
        item = INFO_CACHE.get(key)
        if item and now - item[0] < INFO_TTL: return json.loads(json.dumps(item[1]))
    raw = worker_json("POST", "/info", {"url": url, "playlist_mode": is_playlist(url), "media_mode": mode}, timeout=180)
    with INFO_LOCK:
        INFO_CACHE[key] = (now, raw)
        if len(INFO_CACHE) > 128:
            for old_key, _ in sorted(INFO_CACHE.items(), key=lambda kv: kv[1][0])[:32]: INFO_CACHE.pop(old_key, None)
    return raw


@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(path, "r", encoding="utf-8") as handle: return handle.read()


@app.get("/health")
def health():
    cleanup_jobs()
    return {"status": "ok", "service": "panda-dl", "version": VERSION, "worker_configured": worker_enabled(), "google_login_configured": bool(GOOGLE_CLIENT_ID), "auth_required": AUTH_REQUIRED, "playlist_editor": True, "track_editor": True, "photo_download": True, "video_format": "mp4", "audio_format": "mp3", "playlist_limit": PLAYLIST_LIMIT}


@app.get("/auth/config")
def auth_config():
    return {"client_id": GOOGLE_CLIENT_ID, "required": AUTH_REQUIRED, "configured": bool(GOOGLE_CLIENT_ID)}


@app.get("/auth/me")
def auth_me(request: Request):
    return {"user": request.session.get("user")}


@app.post("/auth/google")
async def auth_google(request: Request):
    if not GOOGLE_CLIENT_ID: raise HTTPException(status_code=503, detail="Connexion Google non configurée")
    data = await request.json(); credential = str(data.get("credential") or "")
    try:
        token = id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Jeton Google invalide") from exc
    email = str(token.get("email") or "").lower()
    if not token.get("email_verified"): raise HTTPException(status_code=401, detail="Adresse Google non vérifiée")
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS: raise HTTPException(status_code=403, detail="Compte Google non autorisé")
    user = {"sub": token.get("sub"), "email": email, "name": token.get("name") or email, "picture": token.get("picture")}
    request.session["user"] = user
    return {"ok": True, "user": user}


@app.post("/auth/logout")
def auth_logout(request: Request):
    request.session.clear(); return {"ok": True}


@app.post("/api/info")
def api_info(request: Request, url: str = Form(...), mode: str = Form("video")):
    require_user(request)
    try: clean = validate_url(url)
    except Exception as exc: return JSONResponse(status_code=400, content={"error": str(exc)})
    mode = mode if mode in {"video", "audio", "image"} else "video"
    try: raw = cached_info(clean, mode)
    except Exception as exc: return JSONResponse(status_code=400, content={"error": str(exc)})
    platform = detect_platform(clean)
    if mode == "image":
        return {"platform": platform, "title": raw.get("title") or "Publication / galerie", "uploader": raw.get("uploader") or raw.get("channel") or platform, "thumbnail": raw.get("thumbnail"), "duration": None, "qualities": [], "quality_sizes": {}, "chapters": [], "is_playlist": False, "playlist_entries": [], "photo_mode": True}
    entries = []
    for idx, item in enumerate(raw.get("entries") or []):
        if not item: continue
        ident = item.get("id") or ""; page = item.get("webpage_url") or item.get("url") or ""
        if ident and not str(page).startswith("http") and platform == "youtube": page = f"https://www.youtube.com/watch?v={ident}"
        entries.append({"index": idx, "playlist_index": idx + 1, "id": ident, "title": item.get("title") or f"Élément {idx + 1}", "duration": item.get("duration"), "thumbnail": item.get("thumbnail") or "", "url": page, "uploader": item.get("uploader") or item.get("channel") or ""})
    qualities = sorted({int(f["height"]) for f in raw.get("formats", []) if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    return {"platform": platform, "title": raw.get("title"), "uploader": raw.get("uploader") or raw.get("channel") or raw.get("artist"), "thumbnail": raw.get("thumbnail"), "duration": raw.get("duration"), "qualities": qualities, "quality_sizes": quality_sizes(raw), "chapters": extract_chapters(raw), "is_playlist": bool(entries), "playlist_entries": entries, "playlist_limited": len(entries) >= PLAYLIST_LIMIT}


@app.post("/api/jobs")
def create_job(request: Request, url: str = Form(...), mode: str = Form("video"), quality: str = Form("best"), audio_quality: str = Form("320"), playlist_mode: str = Form("false"), selected_playlist: str = Form("[]"), split_tracks: str = Form("false"), selected_tracks: str = Form("[]"), track_titles: str = Form("{}")):
    require_user(request); cleanup_jobs()
    try: clean = validate_url(url)
    except Exception as exc: return JSONResponse(status_code=400, content={"error": str(exc)})
    mode = mode if mode in {"video", "audio", "image"} else "video"; quality = quality if quality in VIDEO_QUALITIES else "best"; audio_quality = audio_quality if audio_quality in AUDIO_QUALITIES else "320"
    job_id = uuid.uuid4().hex; now = time.time()
    payload = {"url": clean, "mode": mode, "quality": quality, "audio_quality": audio_quality, "playlist_mode": playlist_mode.lower() == "true", "selected_playlist": parse_list(selected_playlist), "split_tracks": split_tracks.lower() == "true", "selected_tracks": parse_list(selected_tracks), "track_titles": parse_dict(track_titles)}
    with JOB_LOCK:
        JOBS[job_id] = {"id": job_id, "status": "queued", "stage": "queued", "progress": 0, "message": "En attente", "created": now, "updated": now, "cancel_requested": False, "remote_id": None, "workdir": None, "result_path": None, "filename": None, "error": None, "result_size": None, "owner": (request.session.get("user") or {}).get("sub")}
    EXECUTOR.submit(run_job, job_id, payload); return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    user = require_user(request); cleanup_jobs(); job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job introuvable")
    if AUTH_REQUIRED and job.get("owner") != (user or {}).get("sub"): raise HTTPException(status_code=403, detail="Job interdit")
    return {k: v for k, v in job.items() if k not in {"workdir", "result_path", "owner", "remote_id"}}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    user = require_user(request); job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job introuvable")
    if AUTH_REQUIRED and job.get("owner") != (user or {}).get("sub"): raise HTTPException(status_code=403, detail="Job interdit")
    update_job(job_id, cancel_requested=True, message="Annulation demandée")
    remote_id = job.get("remote_id")
    if remote_id:
        try: worker_json("POST", f"/jobs/{remote_id}/cancel", {}, timeout=10)
        except Exception: pass
    return {"status": "cancelling"}


@app.get("/api/jobs/{job_id}/download")
def download_job(request: Request, job_id: str):
    user = require_user(request); job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job introuvable")
    if AUTH_REQUIRED and job.get("owner") != (user or {}).get("sub"): raise HTTPException(status_code=403, detail="Job interdit")
    if job.get("status") != "ready": raise HTTPException(status_code=409, detail="Job non prêt")
    path = job.get("result_path")
    if not path or not os.path.isfile(path): raise HTTPException(status_code=410, detail="Fichier expiré")
    return FileResponse(path, media_type=job.get("media_type") or "application/octet-stream", filename=job.get("filename") or os.path.basename(path))
