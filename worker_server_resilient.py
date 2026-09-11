import os
import subprocess
import time

import worker_server as core

# PANDA DL resilient YouTube chain:
# 1) yt-dlp with the user's Chromium session (normal path)
# 2) yt-dlp without browser cookies (public-content fallback when a stale/broken cookie session is the problem)
# 3) official ytdl-org/youtube-dl without cookies (independent legacy extractor fallback for public content)
# None of the fallback paths grants access to private/restricted content.
YOUTUBEDL = os.getenv("PANDA_YOUTUBEDL_BIN", "/opt/panda-dl-worker/venv/bin/youtube-dl")

_original_run_ytdlp = core._run_ytdlp


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
    try:
        return _original_run_ytdlp(job_id, payload, url, workdir)
    except InterruptedError:
        raise
    except Exception as primary_exc:
        if not core._youtube(url):
            raise

        errors = [f"yt-dlp session: {primary_exc}"]

        # Cookie/session problems on an otherwise public video can disappear when retried anonymously.
        _clean_failed_media(workdir)
        try:
            _run_public_engine(job_id, payload, url, workdir, core.YTDLP, "yt-dlp-public", modern=True)
            return
        except InterruptedError:
            raise
        except Exception as exc:
            errors.append(f"yt-dlp public: {exc}")

        # Independent fallback requested by the user: official ytdl-org/youtube-dl.
        _clean_failed_media(workdir)
        try:
            _run_public_engine(job_id, payload, url, workdir, YOUTUBEDL, "youtube-dl", modern=False)
            return
        except InterruptedError:
            raise
        except Exception as exc:
            errors.append(f"youtube-dl: {exc}")

        # A second engine cannot replace authorization for private/age/account-only media.
        if any(core._auth_required_error(text) for text in errors):
            raise RuntimeError(
                "YouTube demande une session autorisée. Les fallbacks publics yt-dlp et youtube-dl ont aussi échoué. "
                "Reconnecte ton propre compte dans Chromium sur le worker si cette vidéo nécessite une connexion."
            )
        raise RuntimeError("Tous les moteurs YouTube ont échoué. " + " | ".join(errors)[-4500:])


core._run_ytdlp = _run_ytdlp_resilient
app = core.app
