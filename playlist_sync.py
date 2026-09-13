import json
import os
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/sync", tags=["playlist-sync"])

SPOTIFY_CLIENT_ID = os.getenv("DINGO_SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("DINGO_SPOTIFY_CLIENT_SECRET", "").strip()
GOOGLE_CLIENT_ID = os.getenv("DINGO_GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("DINGO_GOOGLE_CLIENT_SECRET", "").strip()

HTTP_TIMEOUT = 25
MAX_TRACKS = max(1, min(int(os.getenv("DINGO_SYNC_MAX_TRACKS", "500")), 2000))
MATCH_THRESHOLD = max(0.55, min(float(os.getenv("DINGO_SYNC_MATCH_THRESHOLD", "0.78")), 0.98))

CONNECTIONS: dict[str, dict[str, dict[str, Any]]] = {}
CONNECTION_LOCK = threading.RLock()
SYNC_JOBS: dict[str, dict[str, Any]] = {}
SYNC_LOCK = threading.RLock()
SYNC_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, min(int(os.getenv("DINGO_SYNC_WORKERS", "2")), 4)),
    thread_name_prefix="dingo-sync",
)

ACTIVE_PROVIDERS = {
    "spotify": "Spotify",
    "youtube": "YouTube / YouTube Music",
}
COMING_SOON = [
    {"id": "deezer", "name": "Deezer"},
    {"id": "apple_music", "name": "Apple Music"},
    {"id": "tidal", "name": "TIDAL"},
    {"id": "soundcloud", "name": "SoundCloud"},
]


class TransferRequest(BaseModel):
    source: str = Field(pattern="^(spotify|youtube)$")
    destination: str = Field(pattern="^(spotify|youtube)$")
    playlist_id: str
    destination_name: str | None = None
    max_tracks: int = Field(default=500, ge=1, le=2000)


class ImportRequest(BaseModel):
    raw_text: str = Field(min_length=1, max_length=500000)
    destination_name: str | None = None
    max_tracks: int = Field(default=250, ge=1, le=2000)


class ResolveRequest(BaseModel):
    video_id: str = Field(min_length=6, max_length=32)


class UnifiedTrack(BaseModel):
    id: str
    title: str
    artist: str = ""
    album: str = ""
    isrc: str | None = None
    duration_ms: int | None = None
    url: str | None = None
    source: str


def _session_id(request: Request) -> str:
    sid = request.session.get("sync_sid")
    if not sid:
        sid = secrets.token_urlsafe(24)
        request.session["sync_sid"] = sid
    return sid


def _public_base(request: Request) -> str:
    forced = os.getenv("DINGO_PUBLIC_URL", "").strip().rstrip("/")
    return forced or str(request.base_url).rstrip("/")


def _save_connection(sid: str, provider: str, token: dict[str, Any]) -> None:
    with CONNECTION_LOCK:
        CONNECTIONS.setdefault(sid, {})[provider] = token


def _get_connection(sid: str, provider: str) -> dict[str, Any]:
    with CONNECTION_LOCK:
        token = dict(CONNECTIONS.get(sid, {}).get(provider) or {})
    if not token:
        raise HTTPException(status_code=401, detail=f"{provider} n'est pas connecté")
    return token


def _refresh_if_needed(sid: str, provider: str) -> dict[str, Any]:
    token = _get_connection(sid, provider)
    if float(token.get("expires_at") or 0) > time.time() + 45:
        return token
    refresh = token.get("refresh_token")
    if not refresh:
        return token

    if provider == "spotify":
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=HTTP_TIMEOUT,
        )
    elif provider == "youtube":
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
            },
            timeout=HTTP_TIMEOUT,
        )
    else:
        return token

    if not r.ok:
        return token
    data = r.json()
    token["access_token"] = data.get("access_token") or token.get("access_token")
    token["expires_at"] = time.time() + int(data.get("expires_in") or 3600)
    token["refresh_token"] = data.get("refresh_token") or refresh
    _save_connection(sid, provider, token)
    return token


def _api(provider: str, sid: str, method: str, url: str, **kwargs):
    token = _refresh_if_needed(sid, provider)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token['access_token']}"
    r = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
    if r.status_code == 401:
        token["expires_at"] = 0
        _save_connection(sid, provider, token)
    if not r.ok:
        raise RuntimeError(f"{provider} API {r.status_code}: {r.text[:1200]}")
    return r.json() if r.content else {}


def _spotify_pages(sid: str, url: str, params: dict[str, Any] | None = None):
    out = []
    while url:
        data = _api("spotify", sid, "GET", url, params=params)
        out.extend(data.get("items") or [])
        url = data.get("next")
        params = None
    return out


def spotify_playlists(sid: str):
    items = _spotify_pages(sid, "https://api.spotify.com/v1/me/playlists", {"limit": 50})
    return [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "count": (p.get("items") or {}).get("total", 0),
            "image": ((p.get("images") or [{}])[0]).get("url"),
        }
        for p in items if p.get("id")
    ]


def spotify_tracks(sid: str, playlist_id: str, limit: int):
    raw = _spotify_pages(
        sid,
        f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
        {"limit": 50, "additional_types": "track"},
    )
    tracks = []
    for row in raw[:limit]:
        item = row.get("item") or row.get("track") or {}
        if not item or item.get("type") not in {None, "track"}:
            continue
        artists = ", ".join(a.get("name", "") for a in item.get("artists") or [])
        tracks.append(UnifiedTrack(
            id=item.get("id") or item.get("uri") or uuid.uuid4().hex,
            title=item.get("name") or "",
            artist=artists,
            album=(item.get("album") or {}).get("name") or "",
            isrc=(item.get("external_ids") or {}).get("isrc"),
            duration_ms=item.get("duration_ms"),
            url=(item.get("external_urls") or {}).get("spotify"),
            source="spotify",
        ).model_dump())
    return tracks


def spotify_search(sid: str, track: dict[str, Any]):
    q = f'track:"{track["title"]}" artist:"{track.get("artist") or ""}"'
    data = _api("spotify", sid, "GET", "https://api.spotify.com/v1/search", params={"q": q, "type": "track", "limit": 8})
    out = []
    for item in ((data.get("tracks") or {}).get("items") or []):
        artists = ", ".join(a.get("name", "") for a in item.get("artists") or [])
        out.append({
            "id": item.get("id"),
            "title": item.get("name") or "",
            "artist": artists,
            "isrc": (item.get("external_ids") or {}).get("isrc"),
            "duration_ms": item.get("duration_ms"),
        })
    return out


def spotify_create_playlist(sid: str, name: str):
    data = _api(
        "spotify", sid, "POST", "https://api.spotify.com/v1/me/playlists",
        json={"name": name[:100], "public": False, "description": "Transférée avec Dingo-dl"},
    )
    return data.get("id")


def spotify_add_tracks(sid: str, playlist_id: str, ids: list[str]):
    for start in range(0, len(ids), 100):
        _api(
            "spotify", sid, "POST", f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
            json={"uris": [f"spotify:track:{x}" for x in ids[start:start + 100]]},
        )


def _youtube_pages(sid: str, endpoint: str, params: dict[str, Any]):
    items, page = [], None
    while True:
        call = dict(params)
        if page:
            call["pageToken"] = page
        data = _api("youtube", sid, "GET", f"https://www.googleapis.com/youtube/v3/{endpoint}", params=call)
        items.extend(data.get("items") or [])
        page = data.get("nextPageToken")
        if not page:
            break
    return items


def youtube_playlists(sid: str):
    items = _youtube_pages(sid, "playlists", {"part": "snippet,contentDetails", "mine": "true", "maxResults": 50})
    return [{
        "id": p.get("id"),
        "name": (p.get("snippet") or {}).get("title"),
        "count": (p.get("contentDetails") or {}).get("itemCount", 0),
        "image": ((((p.get("snippet") or {}).get("thumbnails") or {}).get("medium") or {}).get("url")),
    } for p in items if p.get("id")]


def youtube_tracks(sid: str, playlist_id: str, limit: int):
    raw = _youtube_pages(
        sid, "playlistItems",
        {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50},
    )[:limit]
    ids = [
        (x.get("contentDetails") or {}).get("videoId")
        or (((x.get("snippet") or {}).get("resourceId") or {}).get("videoId"))
        for x in raw
    ]
    ids = [x for x in ids if x]
    durations: dict[str, int] = {}
    for start in range(0, len(ids), 50):
        videos = _api(
            "youtube", sid, "GET", "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails", "id": ",".join(ids[start:start + 50])},
        )
        for v in videos.get("items") or []:
            durations[v.get("id")] = _iso_duration_ms(((v.get("contentDetails") or {}).get("duration") or ""))

    tracks = []
    for item in raw:
        snippet = item.get("snippet") or {}
        video_id = (item.get("contentDetails") or {}).get("videoId") or ((snippet.get("resourceId") or {}).get("videoId"))
        if not video_id:
            continue
        title, artist = _split_youtube_title(snippet.get("title") or "")
        tracks.append(UnifiedTrack(
            id=video_id,
            title=title,
            artist=artist or snippet.get("videoOwnerChannelTitle") or "",
            duration_ms=durations.get(video_id),
            url=f"https://www.youtube.com/watch?v={video_id}",
            source="youtube",
        ).model_dump())
    return tracks


def youtube_search(sid: str, track: dict[str, Any]):
    q = f'{track.get("artist") or ""} {track.get("title") or ""}'.strip()
    data = _api(
        "youtube", sid, "GET", "https://www.googleapis.com/youtube/v3/search",
        params={"part": "snippet", "type": "video", "maxResults": 5, "q": q},
    )
    items = data.get("items") or []
    ids = [((x.get("id") or {}).get("videoId")) for x in items]
    ids = [x for x in ids if x]
    durations = {}
    if ids:
        videos = _api(
            "youtube", sid, "GET", "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails", "id": ",".join(ids)},
        )
        for v in videos.get("items") or []:
            durations[v.get("id")] = _iso_duration_ms(((v.get("contentDetails") or {}).get("duration") or ""))

    out = []
    for item in items:
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            continue
        snippet = item.get("snippet") or {}
        title, artist = _split_youtube_title(snippet.get("title") or "")
        out.append({
            "id": vid,
            "title": title,
            "artist": artist or snippet.get("channelTitle") or "",
            "duration_ms": durations.get(vid),
            "isrc": None,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


def youtube_create_playlist(sid: str, name: str):
    data = _api(
        "youtube", sid, "POST", "https://www.googleapis.com/youtube/v3/playlists",
        params={"part": "snippet,status"},
        json={
            "snippet": {"title": name[:150], "description": "Créée avec Dingo-dl"},
            "status": {"privacyStatus": "private"},
        },
    )
    return data.get("id")


def youtube_add_tracks(sid: str, playlist_id: str, ids: list[str]):
    for video_id in ids:
        _api(
            "youtube", sid, "POST", "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet"},
            json={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        )


def _iso_duration_ms(value: str) -> int | None:
    m = re.fullmatch(r"P(?:(?P<d>\d+)D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?", value or "")
    if not m:
        return None
    seconds = int(m.group("d") or 0) * 86400 + int(m.group("h") or 0) * 3600 + int(m.group("m") or 0) * 60 + int(m.group("s") or 0)
    return seconds * 1000


NOISE = re.compile(r"\b(official|official audio|official video|lyrics?|lyric video|visuali[sz]er|remaster(?:ed)?(?:\s+\d{4})?|hd|hq|audio|video|topic)\b", re.I)


def _norm(value: str) -> str:
    value = NOISE.sub(" ", value or "")
    value = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _split_youtube_title(value: str):
    clean = re.sub(r"\s+", " ", NOISE.sub(" ", value or "")).strip(" -–—|")
    parts = re.split(r"\s[-–—]\s", clean, maxsplit=1)
    return (parts[1].strip(), parts[0].strip()) if len(parts) == 2 and len(parts[0]) < 80 else (clean, "")


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def match_score(source: dict[str, Any], candidate: dict[str, Any]) -> float:
    s_isrc, c_isrc = (source.get("isrc") or "").upper(), (candidate.get("isrc") or "").upper()
    if s_isrc and c_isrc and s_isrc == c_isrc:
        return 1.0
    title = _sim(source.get("title") or "", candidate.get("title") or "")
    artist = _sim(source.get("artist") or "", candidate.get("artist") or "")
    duration_score = 0.5
    sd, cd = source.get("duration_ms"), candidate.get("duration_ms")
    if sd and cd:
        diff = abs(int(sd) - int(cd)) / 1000
        duration_score = 1.0 if diff <= 3 else 0.9 if diff <= 5 else 0.65 if diff <= 10 else 0.25 if diff <= 20 else 0.0
    return round(title * 0.58 + artist * 0.27 + duration_score * 0.15, 4)


def _provider_playlists(provider: str, sid: str):
    return spotify_playlists(sid) if provider == "spotify" else youtube_playlists(sid)


def _provider_tracks(provider: str, sid: str, playlist_id: str, limit: int):
    return spotify_tracks(sid, playlist_id, limit) if provider == "spotify" else youtube_tracks(sid, playlist_id, limit)


def _provider_search(provider: str, sid: str, track: dict[str, Any]):
    return spotify_search(sid, track) if provider == "spotify" else youtube_search(sid, track)


def _provider_create(provider: str, sid: str, name: str):
    return spotify_create_playlist(sid, name) if provider == "spotify" else youtube_create_playlist(sid, name)


def _provider_add(provider: str, sid: str, playlist_id: str, ids: list[str]):
    return spotify_add_tracks(sid, playlist_id, ids) if provider == "spotify" else youtube_add_tracks(sid, playlist_id, ids)


def _update_job(job_id: str, **values):
    with SYNC_LOCK:
        job = SYNC_JOBS.get(job_id)
        if job:
            job.update(values)
            job["updated_at"] = time.time()


def _clone_youtube(job_id: str, sid: str, req: dict[str, Any]):
    tracks = youtube_tracks(sid, req["playlist_id"], min(req["max_tracks"], MAX_TRACKS))
    if not tracks:
        raise RuntimeError("Playlist vide ou inaccessible")
    name = req.get("destination_name") or f"Dingo clone {time.strftime('%Y-%m-%d')}"
    playlist_id = youtube_create_playlist(sid, name)
    ids = [t["id"] for t in tracks if t.get("id")]
    _update_job(job_id, stage="writing", progress=88, total=len(tracks), matched=len(ids), unmatched=len(tracks) - len(ids))
    if ids:
        youtube_add_tracks(sid, playlist_id, ids)
    report = [{
        "index": i,
        "source": t,
        "matched": bool(t.get("id")),
        "score": 1.0 if t.get("id") else 0.0,
        "destination": t if t.get("id") else None,
    } for i, t in enumerate(tracks)]
    _update_job(
        job_id, status="ready", stage="ready", progress=100,
        total=len(tracks), matched=len(ids), unmatched=len(tracks) - len(ids),
        destination_playlist_id=playlist_id, report=report,
    )


def _run_transfer(job_id: str, sid: str, req: dict[str, Any]):
    try:
        source, dest = req["source"], req["destination"]
        _update_job(job_id, status="running", stage="loading", progress=3)

        if source == dest == "youtube":
            _clone_youtube(job_id, sid, req)
            return
        if source == dest:
            raise RuntimeError("La source et la destination doivent être différentes")

        tracks = _provider_tracks(source, sid, req["playlist_id"], min(req["max_tracks"], MAX_TRACKS))
        if not tracks:
            raise RuntimeError("Playlist vide ou inaccessible")

        name = req.get("destination_name") or f"Dingo transfer {time.strftime('%Y-%m-%d')}"
        destination_playlist_id = _provider_create(dest, sid, name)
        matched_ids, report, total = [], [], len(tracks)

        for idx, track in enumerate(tracks, 1):
            candidates = _provider_search(dest, sid, track)
            ranked = sorted(((match_score(track, c), c) for c in candidates), key=lambda x: x[0], reverse=True)
            score, best = ranked[0] if ranked else (0.0, None)
            matched = bool(best and score >= MATCH_THRESHOLD)
            if matched:
                matched_ids.append(best["id"])
            report.append({
                "index": idx - 1,
                "source": track,
                "matched": matched,
                "score": score,
                "destination": best if matched else None,
            })
            _update_job(
                job_id,
                progress=5 + int((idx / total) * 80),
                stage="matching",
                current=idx,
                total=total,
                matched=len(matched_ids),
                unmatched=idx - len(matched_ids),
            )

        _update_job(job_id, stage="writing", progress=88)
        if matched_ids:
            _provider_add(dest, sid, destination_playlist_id, matched_ids)
        _update_job(
            job_id, status="ready", stage="ready", progress=100,
            matched=len(matched_ids), unmatched=total - len(matched_ids), total=total,
            destination_playlist_id=destination_playlist_id, report=report,
        )
    except Exception as exc:
        _update_job(job_id, status="error", stage="error", error=str(exc), message=str(exc))


def _clean_import_line(line: str) -> str:
    line = line.strip().lstrip("\ufeff")
    if not line or line.startswith("#"):
        return ""
    if line.startswith("http://") or line.startswith("https://"):
        return ""
    return line


def _parse_import(raw_text: str, limit: int) -> list[dict[str, Any]]:
    text = raw_text.strip()
    rows: list[dict[str, Any]] = []

    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("tracks") or data.get("items") or []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        line = _clean_import_line(item)
                        if line:
                            rows.append({"title": line, "artist": ""})
                    elif isinstance(item, dict):
                        title = str(item.get("title") or item.get("name") or "").strip()
                        artist = str(item.get("artist") or item.get("artists") or "").strip()
                        if isinstance(item.get("artists"), list):
                            artist = ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in item["artists"])
                        if title:
                            rows.append({"title": title, "artist": artist})
        except Exception:
            pass

    if not rows:
        for raw in text.splitlines():
            line = _clean_import_line(raw)
            if not line:
                continue
            if "," in line and line.count(",") <= 4:
                left, right = [x.strip().strip('"') for x in line.split(",", 1)]
                if left.lower() in {"artist", "artiste", "title", "titre"}:
                    continue
                if left and right:
                    rows.append({"artist": left, "title": right})
                    continue
            parts = re.split(r"\s[-–—|]\s", line, maxsplit=1)
            if len(parts) == 2:
                rows.append({"artist": parts[0].strip(), "title": parts[1].strip()})
            else:
                rows.append({"artist": "", "title": line})

    seen, out = set(), []
    for row in rows:
        title = str(row.get("title") or "").strip()
        artist = str(row.get("artist") or "").strip()
        key = (_norm(title), _norm(artist))
        if not title or key in seen:
            continue
        seen.add(key)
        out.append(UnifiedTrack(
            id=f"import-{len(out)+1}",
            title=title,
            artist=artist,
            source="import",
        ).model_dump())
        if len(out) >= limit:
            break
    return out


def _run_import(job_id: str, sid: str, req: dict[str, Any]):
    try:
        _update_job(job_id, status="running", stage="parsing", progress=3)
        tracks = _parse_import(req["raw_text"], min(req["max_tracks"], MAX_TRACKS))
        if not tracks:
            raise RuntimeError("Aucun titre reconnu dans l'import")

        name = req.get("destination_name") or f"Dingo import {time.strftime('%Y-%m-%d')}"
        destination_playlist_id = youtube_create_playlist(sid, name)
        matched_ids, report, total = [], [], len(tracks)

        for idx, track in enumerate(tracks, 1):
            candidates = _provider_search("youtube", sid, track)
            ranked = sorted(((match_score(track, c), c) for c in candidates), key=lambda x: x[0], reverse=True)
            score, best = ranked[0] if ranked else (0.0, None)
            matched = bool(best and score >= MATCH_THRESHOLD)
            if matched:
                matched_ids.append(best["id"])
            report.append({
                "index": idx - 1,
                "source": track,
                "matched": matched,
                "score": score,
                "destination": best if matched else None,
            })
            _update_job(
                job_id,
                progress=5 + int((idx / total) * 80),
                stage="matching",
                current=idx,
                total=total,
                matched=len(matched_ids),
                unmatched=idx - len(matched_ids),
            )

        _update_job(job_id, stage="writing", progress=88)
        if matched_ids:
            youtube_add_tracks(sid, destination_playlist_id, matched_ids)

        _update_job(
            job_id, status="ready", stage="ready", progress=100,
            matched=len(matched_ids), unmatched=total - len(matched_ids), total=total,
            destination_playlist_id=destination_playlist_id, report=report,
            import_mode=True,
        )
    except Exception as exc:
        _update_job(job_id, status="error", stage="error", error=str(exc), message=str(exc))


@router.get("/providers")
def providers(request: Request):
    sid = _session_id(request)
    with CONNECTION_LOCK:
        connected = set(CONNECTIONS.get(sid, {}).keys())
    active = [
        {
            "id": "spotify",
            "name": "Spotify",
            "configured": bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET),
            "connected": "spotify" in connected,
            "status": "active",
        },
        {
            "id": "youtube",
            "name": "YouTube / YouTube Music",
            "configured": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
            "connected": "youtube" in connected,
            "status": "active",
        },
    ]
    upcoming = [{**p, "configured": False, "connected": False, "status": "coming_soon"} for p in COMING_SOON]
    return {"providers": active + upcoming}


@router.get("/oauth/{provider}/start")
def oauth_start(provider: str, request: Request):
    _session_id(request)
    state = secrets.token_urlsafe(24)
    request.session[f"sync_oauth_state_{provider}"] = state
    base = _public_base(request)

    if provider == "spotify":
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="Spotify OAuth non configuré")
        redirect = f"{base}/api/sync/oauth/spotify/callback"
        params = {
            "client_id": SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect,
            "state": state,
            "scope": "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public",
            "show_dialog": "true",
        }
        return RedirectResponse("https://accounts.spotify.com/authorize?" + urlencode(params))

    if provider == "youtube":
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="Google OAuth non configuré")
        redirect = f"{base}/api/sync/oauth/youtube/callback"
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

    raise HTTPException(status_code=404, detail="Plateforme inconnue")


@router.get("/oauth/{provider}/callback")
def oauth_callback(provider: str, request: Request, code: str, state: str):
    expected = request.session.pop(f"sync_oauth_state_{provider}", None)
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(status_code=400, detail="OAuth state invalide")

    sid = _session_id(request)
    base = _public_base(request)
    redirect = f"{base}/api/sync/oauth/{provider}/callback"

    if provider == "spotify":
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=HTTP_TIMEOUT,
        )
    elif provider == "youtube":
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
            },
            timeout=HTTP_TIMEOUT,
        )
    else:
        raise HTTPException(status_code=404, detail="Plateforme inconnue")

    if not r.ok:
        raise HTTPException(status_code=400, detail=r.text[:1200])

    token = r.json()
    token["expires_at"] = time.time() + int(token.get("expires_in") or 3600)
    _save_connection(sid, provider, token)
    return RedirectResponse("/sync?connected=" + provider)


@router.post("/disconnect/{provider}")
def disconnect(provider: str, request: Request):
    if provider not in ACTIVE_PROVIDERS:
        raise HTTPException(status_code=404, detail="Plateforme inconnue")
    sid = _session_id(request)
    with CONNECTION_LOCK:
        CONNECTIONS.get(sid, {}).pop(provider, None)
    return {"ok": True}


@router.get("/playlists/{provider}")
def playlists(provider: str, request: Request):
    if provider not in ACTIVE_PROVIDERS:
        raise HTTPException(status_code=404, detail="Plateforme inconnue")
    sid = _session_id(request)
    try:
        return {"playlists": _provider_playlists(provider, sid)}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/transfer")
def transfer(body: TransferRequest, request: Request):
    sid = _session_id(request)
    _get_connection(sid, body.source)
    _get_connection(sid, body.destination)
    job_id, now = uuid.uuid4().hex, time.time()
    with SYNC_LOCK:
        SYNC_JOBS[job_id] = {
            "id": job_id, "status": "queued", "stage": "queued", "progress": 0,
            "created_at": now, "updated_at": now, "matched": 0, "unmatched": 0,
            "mode": "transfer",
        }
    payload = body.model_dump()
    payload["max_tracks"] = min(payload["max_tracks"], MAX_TRACKS)
    SYNC_EXECUTOR.submit(_run_transfer, job_id, sid, payload)
    return {"job_id": job_id, "status": "queued"}


@router.post("/import/youtube")
def import_youtube(body: ImportRequest, request: Request):
    sid = _session_id(request)
    _get_connection(sid, "youtube")
    job_id, now = uuid.uuid4().hex, time.time()
    with SYNC_LOCK:
        SYNC_JOBS[job_id] = {
            "id": job_id, "status": "queued", "stage": "queued", "progress": 0,
            "created_at": now, "updated_at": now, "matched": 0, "unmatched": 0,
            "mode": "import", "destination": "youtube",
        }
    payload = body.model_dump()
    payload["max_tracks"] = min(payload["max_tracks"], MAX_TRACKS)
    SYNC_EXECUTOR.submit(_run_import, job_id, sid, payload)
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
def sync_job(job_id: str):
    with SYNC_LOCK:
        job = dict(SYNC_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return job


@router.get("/jobs/{job_id}/candidates/{index}")
def job_candidates(job_id: str, index: int, request: Request):
    sid = _session_id(request)
    _get_connection(sid, "youtube")
    with SYNC_LOCK:
        job = dict(SYNC_JOBS.get(job_id) or {})
    report = job.get("report") or []
    if not job or index < 0 or index >= len(report):
        raise HTTPException(status_code=404, detail="Élément introuvable")
    source = (report[index] or {}).get("source") or {}
    try:
        candidates = youtube_search(sid, source)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    ranked = sorted(
        [{**c, "score": match_score(source, c)} for c in candidates],
        key=lambda x: x["score"],
        reverse=True,
    )
    return {"source": source, "candidates": ranked}


@router.post("/jobs/{job_id}/resolve/{index}")
def resolve_job(job_id: str, index: int, body: ResolveRequest, request: Request):
    sid = _session_id(request)
    _get_connection(sid, "youtube")
    with SYNC_LOCK:
        job = SYNC_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job introuvable")
        report = job.get("report") or []
        if index < 0 or index >= len(report):
            raise HTTPException(status_code=404, detail="Élément introuvable")
        destination_playlist_id = job.get("destination_playlist_id")
        if not destination_playlist_id:
            raise HTTPException(status_code=409, detail="Playlist destination indisponible")
        if report[index].get("matched"):
            return {"ok": True, "already_resolved": True}
    try:
        youtube_add_tracks(sid, destination_playlist_id, [body.video_id])
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with SYNC_LOCK:
        job = SYNC_JOBS[job_id]
        row = job["report"][index]
        row["matched"] = True
        row["score"] = 1.0
        row["destination"] = {
            "id": body.video_id,
            "title": "Résolution manuelle",
            "artist": "",
            "url": f"https://www.youtube.com/watch?v={body.video_id}",
        }
        job["matched"] = int(job.get("matched") or 0) + 1
        job["unmatched"] = max(0, int(job.get("unmatched") or 0) - 1)
        job["updated_at"] = time.time()
    return {"ok": True}
