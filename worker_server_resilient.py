import json
import os
import subprocess
import time

from fastapi import Header, HTTPException, Request

import worker_server as core

# PANDA DL resilient YouTube chain:
# 1) SIMPLE yt-dlp command matching the UI's default BEST VIDEO path
# 2) yt-dlp with the user's Chromium session
# 3) yt-dlp without browser cookies (public-content fallback)
# 4) official ytdl-org/youtube-dl without cookies (independent legacy fallback)
# None of the fallback paths grants access to private/restricted content.
YOUTUBEDL = os.getenv("PANDA_YOUTUBEDL_BIN", "/opt/panda-dl-worker/venv/bin/youtube-dl")

_original_run_ytdlp = core._run_ytdlp
_original_info = core.info


def _clean_failed_media(workdir):
    for path in core._walk_payload(workdir):
        if path.endswith(".json"):
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def _legacy_selector(quality):
    if quality == "best":
        return "bestvideo+bestaudio/best"
    try:
        height = max(144, min(int(quality), 4320))
    except Exception:
        height = 1080
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"


def _run_simple_video(job_id, url, workdir):
    """Run the exact simple command requested by the user, adapted to the worker temp dir."""
    cmd = [
        core.YTDLP,
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--embed-metadata",
        "-o", os.path.join(workdir, "%(title)s.%(ext)s"),
        url,
    ]
    core._update(job_id, stage="youtube_simple", progress=5, message="YouTube · yt-dlp simple")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    tail = []
    while True:
        if core._cancelled(job_id):
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise InterruptedError("Job annulé")
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            tail.append(line.rstrip())
            tail = tail[-120:]
        if proc.poll() is not None:
            if proc.stdout:
                rest = proc.stdout.read()
                if rest:
                    tail.extend(rest.splitlines()[-60:])
            break
        if not line:
            time.sleep(0.05)
    if proc.returncode != 0:
        raise RuntimeError("\n".join(tail[-60:])[-5000:] or "yt-dlp simple a échoué")


def _run_public_engine(job_id, payload, url, workdir, binary, label, modern=False):
    if not binary or not os.path.isfile(binary):
        raise RuntimeError(f"{label} indisponible")

    mode = "audio" if payload.get("mode") == "audio" else "video"
    quality = str(payload.get("quality") or "best")
    playlist_mode = bool(payload.get("playlist_mode"))
    selected = payload.get("selected_playlist") or []

    cmd = [
        binary,
        "--newline",
        "--retries", "6",
        "--socket-timeout", "20",
        "--write-info-json",
        "--restrict-filenames",
    ]
    if modern:
        cmd += core._deno_flags()
        cmd += ["--fragment-retries", "6", "--extractor-retries", "2", "--concurrent-fragments", str(core.FRAGMENTS)]

    if playlist_mode:
        one_based = sorted({int(x) + 1 for x in selected if 0 <= int(x) < core.PLAYLIST_LIMIT})
        if not one_based:
            raise RuntimeError("Aucune vidéo sélectionnée")
        if core._youtube(url):
            url = core._playlist_url(url)
        cmd += [
            "--yes-playlist",
            "--playlist-items", ",".join(str(x) for x in one_based),
            "--playlist-end", str(core.PLAYLIST_LIMIT),
            "-o", os.path.join(workdir, f"{label}-%(playlist_index)03d-%(title)s.%(ext)s"),
        ]
    else:
        cmd += ["--no-playlist", "-o", os.path.join(workdir, f"{label}-%(id)s.%(ext)s")]

    if mode == "audio":
        cmd += ["-f", "bestaudio/best"]
    else:
        selector = core._selector(quality) if modern else _legacy_selector(quality)
        cmd += ["-f", selector, "--merge-output-format", "mp4"]
    cmd.append(url)

    core._update(job_id, stage="youtube_fallback", progress=9, message=f"YouTube · fallback {label}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    tail = []
    while True:
        if core._cancelled(job_id):
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise InterruptedError("Job annulé")
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            tail.append(line.rstrip())
            tail = tail[-100:]
        if proc.poll() is not None:
            if proc.stdout:
                rest = proc.stdout.read()
                if rest:
                    tail.extend(rest.splitlines()[-50:])
            break
        if not line:
            time.sleep(0.05)

    if proc.returncode != 0:
        raise RuntimeError("\n".join(tail[-60:])[-5000:] or f"{label} a échoué")


def _run_ytdlp_resilient(job_id, payload, url, workdir):
    if not core._youtube(url):
        return _original_run_ytdlp(job_id, payload, url, workdir)

    errors = []
    simple_eligible = (
        str(payload.get("mode") or "video") == "video"
        and str(payload.get("quality") or "best") == "best"
        and not bool(payload.get("playlist_mode"))
    )

    # Default UI BEST VIDEO uses the user's exact simple command first:
    # yt-dlp -f "bv*+ba/b" --merge-output-format mp4 --embed-metadata -o ".../%(title)s.%(ext)s" URL
    if simple_eligible:
        try:
            _run_simple_video(job_id, url, workdir)
            return
        except InterruptedError:
            raise
        except Exception as exc:
            errors.append(f"yt-dlp simple: {exc}")
            _clean_failed_media(workdir)

    # Next retry uses the user's own authenticated Chromium session.
    try:
        _original_run_ytdlp(job_id, payload, url, workdir)
        return
    except InterruptedError:
        raise
    except Exception as exc:
        errors.append(f"yt-dlp session: {exc}")

    # Public retry without browser cookies. Useful when the browser cookie store is stale/broken.
    _clean_failed_media(workdir)
    try:
        _run_public_engine(job_id, payload, url, workdir, core.YTDLP, "yt-dlp-public", modern=True)
        return
    except InterruptedError:
        raise
    except Exception as exc:
        errors.append(f"yt-dlp public: {exc}")

    # Independent official fallback.
    _clean_failed_media(workdir)
    try:
        _run_public_engine(job_id, payload, url, workdir, YOUTUBEDL, "youtube-dl", modern=False)
        return
    except InterruptedError:
        raise
    except Exception as exc:
        errors.append(f"youtube-dl: {exc}")

    if any(core._auth_required_error(text) for text in errors):
        raise RuntimeError(
            "YouTube demande une session autorisée. Le mode simple et les fallbacks publics ont échoué. "
            "Reconnecte ton propre compte dans Chromium sur le worker si cette vidéo nécessite une connexion."
        )
    raise RuntimeError("Tous les moteurs YouTube ont échoué. " + " | ".join(errors)[-4500:])


async def _resilient_info(request: Request, authorization: str | None = Header(default=None)):
    """For YouTube, analyze publicly first so broken cookies do not block the UI."""
    core._auth(authorization)
    payload = await request.json()
    url = core._public_url(payload.get("url"))
    playlist_mode = bool(payload.get("playlist_mode"))
    media_mode = str(payload.get("media_mode") or "video")

    if not core._youtube(url) or media_mode == "image":
        return await _original_info(request, authorization)

    cmd = [core.YTDLP, "--dump-single-json", "--skip-download", "--no-warnings"]
    if playlist_mode:
        url = core._playlist_url(url)
        cmd += ["--flat-playlist", "--yes-playlist", "--playlist-end", str(core.PLAYLIST_LIMIT)]
    else:
        cmd += ["--no-playlist"]
    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180, check=False)
    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
            if not playlist_mode or (data.get("entries") or []):
                return data
        except Exception:
            pass

    # If public analysis fails, retry through the authenticated Chromium route.
    return await _original_info(request, authorization)


core._run_ytdlp = _run_ytdlp_resilient

# FastAPI built the /info dependency graph before this wrapper was imported.
# Swap its callable so the web interface also gets the public-first analysis path.
for route in core.app.router.routes:
    if getattr(route, "path", None) == "/info" and "POST" in (getattr(route, "methods", set()) or set()):
        route.endpoint = _resilient_info
        if getattr(route, "dependant", None) is not None:
            route.dependant.call = _resilient_info

app = core.app
