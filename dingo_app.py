import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "dingo_index.html"
CACHE_TTL = int(os.getenv("DINGO_INFO_TTL", "300"))
PLAYLIST_LIMIT = int(os.getenv("DINGO_PLAYLIST_LIMIT", "100"))
CACHE: dict[str, dict] = {}
CACHE_LOCK = threading.Lock()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

app = FastAPI(title="Dingo-dl", version="7.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


class URLRequest(BaseModel):
    url: str


class TrackRequest(BaseModel):
    index: int = 0
    title: str = "Track"
    start_time: float
    end_time: float | None = None
    filename: str | None = None


class DownloadRequest(BaseModel):
    url: str
    kind: Literal["video", "audio", "photo", "playlist", "tracks"] = "video"
    height: int | None = None
    output_format: str = "mp4"
    audio_kbps: int = 320
    embed_metadata: bool = True
    selected_indices: list[int] = []
    tracks: list[TrackRequest] = []


def clean_error(value: object) -> str:
    return ANSI_RE.sub("", str(value)).strip()


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    m = re.match(r"^\[[^\]]*\]\((https?://[^)]+)\)$", value)
    if m:
        value = m.group(1)
    return value.strip("<> \t\r\n")


def validate_public_url(value: str) -> str:
    url = normalize_url(value)
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise HTTPException(400, "URL invalide")
    if p.username or p.password:
        raise HTTPException(400, "URL avec identifiants refusée")
    host = p.hostname.lower().strip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise HTTPException(400, "Adresse locale refusée")
    try:
        for info in socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise HTTPException(400, "Adresse réseau privée refusée")
    except HTTPException:
        raise
    except Exception:
        pass
    return url


def cache_get(key: str):
    with CACHE_LOCK:
        item = CACHE.get(key)
        if not item:
            return None
        if time.time() - item["time"] > CACHE_TTL:
            CACHE.pop(key, None)
            return None
        return item["data"]


def cache_set(key: str, data: dict):
    with CACHE_LOCK:
        CACHE[key] = {"time": time.time(), "data": data}


def youtube_id(url: str):
    p = urlparse(url)
    host = p.netloc.lower()
    if "youtu.be" in host:
        return p.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        q = parse_qs(p.query)
        if q.get("v"):
            return q["v"][0]
        m = re.search(r"/(?:shorts|embed|live)/([^/?]+)", p.path)
        if m:
            return m.group(1)
    return None


def playlist_id(url: str):
    q = parse_qs(urlparse(url).query)
    return q.get("list", [None])[0]


def is_playlist_url(url: str) -> bool:
    p = urlparse(url)
    return bool(playlist_id(url) or "/playlist" in p.path)


def platform_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().replace("www.", "")
    rules = [
        (("youtube.com", "youtu.be"), "YouTube"),
        (("instagram.com",), "Instagram"),
        (("tiktok.com",), "TikTok"),
        (("twitter.com", "x.com"), "X"),
        (("facebook.com", "fb.watch"), "Facebook"),
        (("pinterest.", "pin.it"), "Pinterest"),
        (("vimeo.com",), "Vimeo"),
        (("soundcloud.com",), "SoundCloud"),
    ]
    for needles, name in rules:
        if any(n in host for n in needles):
            return name
    return host or "Media"


def thumb(entry: dict):
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    ts = entry.get("thumbnails") or []
    if ts:
        return ts[-1].get("url")
    if entry.get("id") and str(entry.get("extractor_key", "")).lower().startswith("youtube"):
        return f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg"
    return None


def flat_entries(info: dict):
    out = []
    for i, e in enumerate(info.get("entries") or []):
        if not e:
            continue
        eid = e.get("id")
        u = e.get("webpage_url") or e.get("url")
        if eid and (not u or not str(u).startswith("http")) and "youtube" in str(info.get("extractor", "")).lower():
            u = f"https://www.youtube.com/watch?v={eid}"
        out.append({
            "index": i,
            "number": i + 1,
            "id": eid,
            "title": e.get("title") or f"Video {i + 1}",
            "uploader": e.get("uploader") or e.get("channel") or e.get("creator"),
            "duration": e.get("duration"),
            "url": u,
            "thumbnail": thumb(e) or (f"https://i.ytimg.com/vi/{eid}/hqdefault.jpg" if eid else None),
        })
    return out


def analyze_playlist_flat(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "lazy_playlist": True,
        "playlistend": PLAYLIST_LIMIT,
        "ignoreerrors": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = flat_entries(info)
    return {
        "success": True,
        "fast": True,
        "is_playlist": True,
        "playlist_id": playlist_id(url) or info.get("id"),
        "id": info.get("id"),
        "title": info.get("title") or "Playlist",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "extractor": info.get("extractor_key") or info.get("extractor") or platform_name(url),
        "thumbnail": thumb(info) or (entries[0]["thumbnail"] if entries else None),
        "duration": None,
        "entries": entries,
        "playlist_count": len(entries),
        "formats": [],
        "tracks": [],
        "details_pending": False,
    }


def youtube_oembed(video_id: str):
    endpoint = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=4) as r:
        data = json.loads(r.read().decode("utf-8"))
    return {
        "success": True,
        "fast": True,
        "is_playlist": False,
        "id": video_id,
        "title": data.get("title"),
        "uploader": data.get("author_name"),
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "duration": None,
        "extractor": "YouTube",
        "formats": [],
        "tracks": [],
        "entries": [],
        "details_pending": True,
    }


TS_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")


def ts_seconds(text: str):
    m = TS_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def description_tracks(description: str, duration=None):
    found = []
    for raw in (description or "").splitlines():
        line = raw.strip()
        start = ts_seconds(line)
        if start is None:
            continue
        title = TS_RE.sub("", line, count=1)
        title = re.sub(r"^[\s\-–—|:•·.]+|[\s\-–—|:•·.]+$", "", title).strip()
        if title:
            found.append((start, title))
    unique = []
    seen = set()
    for item in sorted(found):
        if item[0] not in seen:
            seen.add(item[0]); unique.append(item)
    if len(unique) < 2:
        return []
    out = []
    for i, (start, title) in enumerate(unique):
        end = unique[i + 1][0] if i + 1 < len(unique) else duration
        if end is not None and end <= start:
            continue
        out.append({"index": i, "number": i + 1, "title": title, "start_time": start, "end_time": end, "source": "description"})
    return out


def native_tracks(chapters, duration=None):
    out = []
    chapters = chapters or []
    for i, ch in enumerate(chapters):
        start = ch.get("start_time")
        if start is None:
            continue
        end = ch.get("end_time")
        if end is None:
            end = chapters[i + 1].get("start_time") if i + 1 < len(chapters) else duration
        out.append({"index": i, "number": i + 1, "title": ch.get("title") or f"Track {i + 1:02d}", "start_time": start, "end_time": end, "source": "chapter"})
    return out


def safe_name(value: str):
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value or "file").strip(" .")
    return value[:180] or "file"


def final_files(root: Path):
    bad = {".part", ".ytdl", ".temp"}
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() not in bad]


def zip_files(root: Path, files: list[Path], name="Dingo-dl.zip"):
    target = root / name
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f != target:
                try:
                    arc = f.relative_to(root)
                except ValueError:
                    arc = f.name
                z.write(f, arcname=str(arc))
    return target


def response_file(path: Path, workdir: Path):
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream", background=BackgroundTask(shutil.rmtree, str(workdir), True))


@app.get("/api/ping")
def ping():
    return {"ok": True, "app": "Dingo-dl", "version": "7.0"}


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": "Dingo-dl",
        "version": "7.0",
        "yt_dlp": yt_dlp.version.__version__,
        "gallery_dl": bool(shutil.which("gallery-dl")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }


@app.post("/api/analyze-fast")
def analyze_fast(req: URLRequest):
    url = validate_public_url(req.url)
    key = "fast:" + url
    cached = cache_get(key)
    if cached:
        return {**cached, "cached": True}
    try:
        if is_playlist_url(url):
            data = analyze_playlist_flat(url)
        elif youtube_id(url):
            data = youtube_oembed(youtube_id(url))
        else:
            opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True, "lazy_playlist": True, "playlistend": PLAYLIST_LIMIT, "ignoreerrors": True, "socket_timeout": 8, "retries": 1, "extractor_retries": 1}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = flat_entries(info)
            data = {
                "success": True, "fast": True, "id": info.get("id"), "title": info.get("title") or "Media",
                "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
                "duration": info.get("duration"), "thumbnail": thumb(info), "extractor": info.get("extractor_key") or platform_name(url),
                "is_playlist": bool(entries), "playlist_count": len(entries), "entries": entries, "formats": [], "tracks": [], "details_pending": not bool(entries),
            }
        cache_set(key, data)
        return data
    except Exception as exc:
        raise HTTPException(400, clean_error(exc))


@app.post("/api/details")
def details(req: URLRequest):
    url = validate_public_url(req.url)
    key = "details:" + url
    cached = cache_get(key)
    if cached:
        return {**cached, "cached": True}
    if is_playlist_url(url):
        try:
            data = analyze_playlist_flat(url); cache_set(key, data); return data
        except Exception as exc:
            raise HTTPException(400, clean_error(exc))
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 15, "retries": 1, "extractor_retries": 1}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = []
        for f in info.get("formats") or []:
            formats.append({
                "format_id": f.get("format_id"), "ext": f.get("ext"), "width": f.get("width"), "height": f.get("height"), "resolution": f.get("resolution"),
                "fps": f.get("fps"), "vcodec": f.get("vcodec"), "acodec": f.get("acodec"), "filesize": f.get("filesize") or f.get("filesize_approx"), "tbr": f.get("tbr"), "abr": f.get("abr"),
            })
        tracks = native_tracks(info.get("chapters"), info.get("duration")) or description_tracks(info.get("description") or "", info.get("duration"))
        data = {"success": True, "is_playlist": False, "duration": info.get("duration"), "formats": formats, "tracks": tracks, "format_count": len(formats), "track_count": len(tracks)}
        cache_set(key, data)
        return data
    except Exception as exc:
        message = clean_error(exc)
        if "not a bot" in message.lower() or "sign in" in message.lower():
            raise HTTPException(401, "YouTube demande une session autorisée pour l’analyse complète. La prévisualisation reste disponible; utilise une session utilisateur sur le worker pour ce média.")
        raise HTTPException(400, message)


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_public_url(req.url)
    workdir = Path(tempfile.mkdtemp(prefix="dingo_"))
    try:
        if req.kind == "photo":
            cmd = ["gallery-dl", "-D", str(workdir), url]
            cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
            if cp.returncode != 0:
                raise RuntimeError(cp.stdout[-1500:] or "gallery-dl a échoué")
            files = final_files(workdir)
            if not files:
                raise RuntimeError("Aucune image générée")
            target = files[0] if len(files) == 1 else zip_files(workdir, files, "Dingo-dl-gallery.zip")
            return response_file(target, workdir)

        if req.kind == "tracks":
            if not req.tracks:
                raise HTTPException(400, "Aucune track sélectionnée")
            opts = {"format": "bestaudio/best", "outtmpl": str(workdir / "source.%(ext)s"), "quiet": False, "noplaylist": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            sources = [p for p in final_files(workdir) if not p.name.startswith("track-")]
            if not sources:
                raise RuntimeError("Source audio introuvable")
            source = max(sources, key=lambda p: p.stat().st_size)
            outputs = []
            for n, tr in enumerate(req.tracks, start=1):
                name = safe_name(tr.filename or tr.title or f"Track {n:02d}")
                if not name.lower().endswith(".mp3"):
                    name += ".mp3"
                target = workdir / name
                cmd = ["ffmpeg", "-y", "-ss", str(tr.start_time), "-i", str(source)]
                if tr.end_time is not None:
                    cmd += ["-to", str(max(0.0, tr.end_time - tr.start_time))]
                cmd += ["-vn", "-codec:a", "libmp3lame", "-b:a", f"{max(64, min(320, req.audio_kbps))}k", str(target)]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
                outputs.append(target)
            target = outputs[0] if len(outputs) == 1 else zip_files(workdir, outputs, "Dingo-dl-tracks.zip")
            return response_file(target, workdir)

        postprocessors = []
        opts = {"outtmpl": str(workdir / "%(playlist_index&{} - |)s%(title)s.%(ext)s"), "quiet": False, "no_warnings": False}

        if req.kind == "playlist":
            if req.selected_indices:
                opts["playlist_items"] = ",".join(str(i + 1) for i in sorted(set(req.selected_indices)))
            opts["format"] = "bv*+ba/b"
            opts["merge_output_format"] = "mp4"
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
        elif req.kind == "audio":
            codec = req.output_format if req.output_format in {"mp3", "m4a", "flac", "wav", "opus"} else "mp3"
            opts["format"] = "bestaudio/best"
            opts["noplaylist"] = True
            postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": str(max(64, min(320, req.audio_kbps)))})
            if req.embed_metadata:
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
        else:
            ext = req.output_format if req.output_format in {"mp4", "mkv", "webm"} else "mp4"
            opts["noplaylist"] = True
            opts["format"] = f"bv*[height<={req.height}]+ba/b[height<={req.height}]" if req.height else "bv*+ba/b"
            opts["merge_output_format"] = ext
            if req.embed_metadata:
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
        if postprocessors:
            opts["postprocessors"] = postprocessors
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = final_files(workdir)
        if not files:
            raise RuntimeError("Aucun fichier final généré")
        if req.kind == "playlist" and len(files) > 1:
            target = zip_files(workdir, files, "Dingo-dl-playlist.zip")
        else:
            target = max(files, key=lambda p: p.stat().st_mtime)
        return response_file(target, workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True); raise
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, clean_error((exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else exc.stderr or exc))
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        message = clean_error(exc)
        status = 401 if ("not a bot" in message.lower() or "sign in" in message.lower()) else 500
        raise HTTPException(status, message)


@app.get("/")
def root():
    if not INDEX_FILE.exists():
        raise HTTPException(500, "dingo_index.html introuvable")
    return FileResponse(str(INDEX_FILE), media_type="text/html")
