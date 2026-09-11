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
from urllib.request import Request, urlopen

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

app = FastAPI(title="PANDA DL")

WORKER_URL = os.getenv("PANDA_YT_WORKER_URL", "").strip().rstrip("/")
WORKER_TOKEN = os.getenv("PANDA_YT_WORKER_TOKEN", "").strip()
WORKER_POLL = max(0.5, min(float(os.getenv("PANDA_YT_WORKER_POLL", "0.8")), 5.0))
JOB_TTL = max(600, min(int(os.getenv("PANDA_JOB_TTL", "3600")), 21600))

VIDEO_QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]
AUDIO_QUALITIES = ["320", "256", "192", "128"]

JOBS = {}
JOB_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="panda-dl")


def worker_enabled():
    return bool(WORKER_URL and WORKER_TOKEN)


def worker_json(method, path, payload=None, timeout=120):
    if not worker_enabled():
        raise RuntimeError("Worker YouTube non configuré")
    body = None
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{WORKER_URL}{path}", data=body, headers=headers, method=method)
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
    req = Request(
        f"{WORKER_URL}{path}",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as response, open(destination, "wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = str(exc)
        raise RuntimeError(detail or str(exc)) from exc


def safe_extract(bundle, destination):
    root = os.path.abspath(destination)
    with zipfile.ZipFile(bundle) as archive:
        for member in archive.infolist():
            target = os.path.abspath(os.path.join(root, member.filename))
            if target != root and not target.startswith(root + os.sep):
                raise RuntimeError("Archive worker invalide")
        archive.extractall(root)


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


def media_files(root):
    ignored = (".json", ".zip", ".part", ".ytdl")
    files = []
    for base, _, names in os.walk(root):
        for name in names:
            if name.lower().endswith(ignored):
                continue
            path = os.path.join(base, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                files.append(path)
    return files


def load_info(root):
    for base, _, names in os.walk(root):
        for name in names:
            if name.endswith(".info.json"):
                try:
                    with open(os.path.join(base, name), "r", encoding="utf-8") as handle:
                        return json.load(handle)
                except Exception:
                    pass
    return {}


def clean_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "PANDA DL"))
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value[:150] or "PANDA DL"


def convert_mp3(source, output, bitrate):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", source, "-vn", "-map_metadata", "0",
        "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
        "-id3v2_version", "3", output,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "Conversion MP3 impossible")[-2500:])


def run_job(job_id, payload):
    workdir = tempfile.mkdtemp(prefix=f"panda_dl_{job_id[:8]}_")
    update_job(job_id, status="running", stage="connecting", progress=3, message="Connexion au worker yt-dlp", workdir=workdir)
    try:
        remote = worker_json(
            "POST",
            "/jobs",
            {
                "url": payload["url"],
                "mode": payload["mode"],
                "quality": payload["quality"] if payload["mode"] == "video" else "best",
                "video_format": "mp4",
                "playlist_mode": False,
                "selected_playlist": [],
            },
            timeout=30,
        )
        remote_id = remote.get("job_id")
        if not remote_id:
            raise RuntimeError("Le worker n'a pas créé le job")

        while True:
            state = worker_json("GET", f"/jobs/{remote_id}", timeout=30)
            status = state.get("status")
            remote_progress = int(state.get("progress") or 0)
            update_job(
                job_id,
                status="running",
                stage="download",
                progress=min(82, 5 + int(max(0, min(remote_progress, 100)) * 0.77)),
                message=state.get("message") or "yt-dlp · téléchargement",
            )
            if status == "ready":
                break
            if status == "error":
                raise RuntimeError(state.get("error") or state.get("message") or "yt-dlp a échoué")
            if status == "cancelled":
                raise RuntimeError("Téléchargement annulé")
            time.sleep(WORKER_POLL)

        bundle = os.path.join(workdir, "worker-bundle.zip")
        update_job(job_id, stage="transfer", progress=84, message="Transfert depuis le worker")
        worker_download(f"/jobs/{remote_id}/download", bundle)
        safe_extract(bundle, workdir)
        try:
            os.remove(bundle)
        except OSError:
            pass

        files = media_files(workdir)
        if not files:
            raise RuntimeError("Aucun média reçu")
        source = max(files, key=os.path.getsize)
        info = load_info(workdir)
        title = clean_name(info.get("title") or "PANDA DL")

        if payload["mode"] == "audio":
            bitrate = payload["audio_quality"]
            final = os.path.join(workdir, "final.mp3")
            update_job(job_id, stage="convert", progress=91, message=f"MP3 · {bitrate} kbps")
            convert_mp3(source, final, bitrate)
            filename = f"{title}.mp3"
        else:
            final = source
            if os.path.splitext(final)[1].lower() != ".mp4":
                remux = os.path.join(workdir, "final.mp4")
                update_job(job_id, stage="remux", progress=91, message="Finalisation MP4")
                proc = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", final, "-map", "0", "-c", "copy", "-movflags", "+faststart", remux],
                    capture_output=True, text=True, timeout=3600, check=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or "Finalisation MP4 impossible")[-2500:])
                final = remux
            filename = f"{title}.mp4"

        update_job(
            job_id,
            status="ready",
            stage="ready",
            progress=100,
            message="Prêt à télécharger",
            result_path=final,
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            result_size=os.path.getsize(final),
        )
    except Exception as exc:
        update_job(job_id, status="error", stage="error", progress=0, message=str(exc), error=str(exc))


HTML = r'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0c1322"><title>PANDA DL</title>
<style>
:root{--ink:#0b111d;--ink2:#151e31;--panel:#eef5f9;--white:#f8fbfd;--line:#d7e2e9;--pink:#ff3c9d;--cyan:#22d8ff;--violet:#8448ff;--amber:#ffd12f;--orange:#ff7736;--muted:#8090a5;--lime:#95ff70}*{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;background:#090d14;color:#fff}body{overflow-x:hidden}button,input{font:inherit}.app{width:min(1240px,100%);margin:auto;min-height:100dvh;background:linear-gradient(#10182a 0 47%,#edf4f8 47% 100%);box-shadow:0 0 70px rgba(0,0,0,.45)}.topbar{height:62px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:linear-gradient(#111a2c,#0b1220);border-bottom:1px solid rgba(255,255,255,.08)}.brand{display:flex;align-items:center;gap:8px;font-weight:950;letter-spacing:.09em}.logo{width:29px;height:29px;position:relative}.logo:before,.logo:after{content:"";position:absolute;left:13px;top:3px;width:4px;height:24px;border-radius:8px;background:linear-gradient(#23ddff,#9258ff);box-shadow:0 0 13px #24d8ff}.logo:before{transform:rotate(45deg)}.logo:after{transform:rotate(-45deg)}.brand span{color:#9aabbd}.engine{font-size:10px;color:#8796aa;font-weight:850;display:flex;gap:7px;align-items:center}.engine:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--lime);box-shadow:0 0 12px var(--lime)}.dark{padding:0 18px 18px;background:linear-gradient(#0d1525,#141d31)}.modules{display:grid;grid-template-columns:repeat(4,1fr)}.module{height:164px;position:relative;padding:18px;border-right:1px solid rgba(255,255,255,.07);background:radial-gradient(circle at 50% 105%,var(--glow),transparent 55%)}.module:first-child{border-left:1px solid rgba(255,255,255,.07)}.module small{font-size:10px;color:#718096;font-weight:950;letter-spacing:.12em}.glyph{position:absolute;left:50%;top:47px;transform:translateX(-50%);width:55px;height:55px;border:3px solid currentColor;border-radius:16px;box-shadow:0 0 26px currentColor,inset 0 0 18px currentColor}.module h3{margin:50px 0 0;text-align:center;font-size:12px;color:#9ca9ba;letter-spacing:.08em}.pink{color:var(--pink);--glow:rgba(255,60,157,.18)}.cyan{color:var(--cyan);--glow:rgba(34,216,255,.15)}.violet{color:var(--violet);--glow:rgba(132,72,255,.18)}.amber{color:var(--amber);--glow:rgba(255,209,47,.15)}.scope{height:260px;border-radius:0 0 22px 22px;overflow:hidden;position:relative;background:#152039;border:1px solid rgba(255,255,255,.06)}#scope{width:100%;height:100%}.scope-label{position:absolute;left:16px;top:13px;font-size:9px;color:#8594a9;letter-spacing:.1em}.transport{height:62px;margin:0 18px;display:flex;justify-content:center;align-items:center;gap:10px;background:#1b2539;border-radius:0 0 18px 18px}.transport button{width:46px;height:36px;border:1px solid #2c3952;border-radius:7px;background:#111929;color:#95a4b8}.transport .play{border-color:#ff803c;color:#fff;box-shadow:0 0 18px rgba(255,128,60,.16)}.console{padding:26px 22px 36px;color:#283449}.urlrow{display:grid;grid-template-columns:1fr 150px;gap:9px;max-width:990px;margin:0 auto 22px;padding:9px;border:1px solid #cedae3;border-radius:18px;background:#dfe8ef;box-shadow:inset 0 1px #fff,0 12px 30px rgba(43,64,84,.07)}.urlrow input{height:55px;border:1px solid #d8e2e8;border-radius:12px;padding:0 15px;background:#fbfdfe;outline:0;color:#243147;font-weight:700}.urlrow input:focus{border-color:#90a7b8;box-shadow:0 0 0 3px rgba(81,130,162,.09)}.urlrow button{border:0;border-radius:12px;background:#151f36;color:white;font-weight:950;letter-spacing:.06em}.meta,.error{display:none;max-width:990px;margin:0 auto 16px;padding:12px 14px;border-radius:12px;font-size:12px}.meta.show{display:block;background:#fff;border:1px solid #dae4eb;color:#617083}.error.show{display:block;background:#fff0f4;border:1px solid #ffd0dc;color:#b63758}.controls{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;max-width:990px;margin:auto}.control{min-height:325px;padding:18px;border-radius:22px;background:linear-gradient(#fafdff,#eaf2f7);border:1px solid #d8e2e9;box-shadow:inset 0 1px #fff,0 18px 36px rgba(44,65,83,.08);text-align:center}.control>small{font-size:9px;color:#8090a2;font-weight:950;letter-spacing:.12em}.meter{width:114px;height:68px;margin:13px auto 6px;border:5px solid #d6e1e8;border-radius:14px 14px 8px 8px;background:radial-gradient(circle at 50% 110%,#ffd53b,#ff7c35 42%,#36263a 73%,#141b2a);box-shadow:inset 0 0 18px rgba(0,0,0,.35);position:relative}.meter:after{content:"";position:absolute;left:55px;bottom:4px;width:2px;height:37px;background:#293143;transform-origin:bottom;transform:rotate(-18deg)}.knobbox{width:178px;height:178px;margin:10px auto 4px;position:relative;display:grid;place-items:center}.ring{position:absolute;inset:5px;border-radius:50%;background:conic-gradient(from 220deg,#27dfff,#e4c137 32%,#ff6949 55%,#263249 56% 79%,transparent 80%);-webkit-mask:radial-gradient(circle,transparent 61%,#000 62%);mask:radial-gradient(circle,transparent 61%,#000 62%)}.knob{width:130px;height:130px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:linear-gradient(145deg,#fff,#dce7ed);border:1px solid #cddae3;box-shadow:10px 12px 25px rgba(52,74,91,.12),inset 7px 7px 13px white,inset -8px -8px 14px rgba(173,192,207,.22)}.knob b{font-size:16px}.knob em{font-size:8px;color:#8795a6;font-style:normal;margin-top:3px}.switches,.qualities{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:11px}.switches button,.qualities button{height:39px;border:1px solid #d3dde5;border-radius:10px;background:#fbfdff;color:#69788a;font-size:9px;font-weight:950}.switches button.active,.qualities button.active{background:#172137;border-color:#172137;color:white}.output{margin-top:11px;padding:11px;border-radius:10px;background:#172137;color:white;font-size:10px;font-weight:950}.download{display:none;max-width:990px;margin:20px auto 0;padding:15px;border-radius:17px;background:white;border:1px solid #d8e1e8}.download.show{display:block}.jobhead{display:flex;justify-content:space-between;gap:12px;font-size:10px;font-weight:950;color:#4a586b}.progress{height:11px;margin-top:11px;background:#e3eaf0;border-radius:999px;overflow:hidden}.bar{height:100%;width:0;background:linear-gradient(90deg,var(--pink),var(--violet),var(--cyan),var(--amber));transition:width .25s}.ready{display:none;height:48px;margin-top:12px;border-radius:11px;background:#172137;color:white;text-decoration:none;font-weight:950;align-items:center;justify-content:center}.ready.show{display:flex}.download-main{height:46px;width:100%;margin-top:12px;border:0;border-radius:11px;background:linear-gradient(#18233b,#101827);color:white;font-weight:950}.download-main:disabled{opacity:.45}
@media(max-width:760px){.topbar{height:55px;padding:0 13px}.engine{font-size:8px}.dark{padding:0 8px 9px}.modules{grid-template-columns:1fr 1fr}.module{height:116px;padding:12px}.glyph{top:34px;width:40px;height:40px}.module h3{margin-top:31px;font-size:9px}.scope{height:210px}.transport{height:55px;margin:0 8px}.transport button{width:40px}.console{padding:15px 9px 28px}.urlrow{grid-template-columns:1fr;padding:7px;margin-bottom:13px}.urlrow input{height:49px}.urlrow button{height:47px}.controls{grid-template-columns:1fr;gap:10px}.control{min-height:auto;padding:14px}.meter{display:none}.knobbox{width:145px;height:145px}.knob{width:105px;height:105px}.switches,.qualities{max-width:380px;margin-left:auto;margin-right:auto}.download{margin-top:12px}}
</style></head><body><div class="app"><header class="topbar"><div class="brand"><i class="logo"></i><b>PANDA</b><span>DL</span></div><div class="engine">YT-DLP WORKER</div></header><section class="dark"><div class="modules"><div class="module pink"><small>MODE</small><i class="glyph"></i><h3 id="mMode">VIDEO</h3></div><div class="module cyan"><small>QUALITY</small><i class="glyph" style="border-radius:50%"></i><h3 id="mQuality">BEST</h3></div><div class="module violet"><small>FORMAT</small><i class="glyph" style="transform:translateX(-50%) rotate(45deg);border-radius:8px"></i><h3 id="mFormat">MP4</h3></div><div class="module amber"><small>STATUS</small><i class="glyph" style="border-radius:50%"></i><h3 id="mStatus">READY</h3></div></div><div class="scope"><canvas id="scope"></canvas><span class="scope-label">PANDA SPECTROGRAM</span></div><div class="transport"><button>◀</button><button class="play">▶</button><button>Ⅱ</button><button>■</button><button>↻</button></div></section><main class="console"><div class="urlrow"><input id="url" placeholder="Colle une URL YouTube…" autocomplete="off"><button id="analyse">ANALYSER</button></div><div class="meta" id="meta"></div><div class="error" id="error"></div><div class="controls"><section class="control"><small>MODE</small><div class="meter"></div><div class="knobbox"><div class="ring"></div><div class="knob"><b id="kMode">VIDEO</b><em>DOWNLOAD MODE</em></div></div><div class="switches"><button class="active" data-mode="video">VIDEO</button><button data-mode="audio">AUDIO</button></div></section><section class="control"><small>QUALITY</small><div class="meter"></div><div class="knobbox"><div class="ring"></div><div class="knob"><b id="kQuality">BEST</b><em>QUALITY</em></div></div><div class="qualities" id="qualities"></div></section><section class="control"><small>OUTPUT</small><div class="meter"></div><div class="knobbox"><div class="ring"></div><div class="knob"><b id="kFormat">MP4</b><em>OUTPUT</em></div></div><div class="output" id="output">MP4 · EMBED METADATA</div><button class="download-main" id="start" disabled>TÉLÉCHARGER</button></section></div><section class="download" id="download"><div class="jobhead"><span id="jobMsg">PRÉPARATION</span><span id="jobPct">0%</span></div><div class="progress"><div class="bar" id="bar"></div></div><a class="ready" id="ready">TÉLÉCHARGER LE FICHIER</a></section></main></div>
<script>
const $=s=>document.querySelector(s);let mode='video',quality='best',analysed=false,activeJob=null,timer=null;const vq=['best','2160','1440','1080','720','480','360'],aq=['320','256','192','128'];
function draw(){const c=$('#scope'),d=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*d;c.height=h*d;const x=c.getContext('2d');x.scale(d,d);x.clearRect(0,0,w,h);x.strokeStyle='rgba(145,161,185,.13)';for(let i=1;i<7;i++){x.beginPath();x.moveTo(w*i/7,0);x.lineTo(w*i/7,h);x.stroke()}[['#ff7a31',.13,0],['#ff3c9d',.20,1.7],['#8448ff',.31,3.3],['#22d8ff',.40,4.9]].forEach(([col,a,p],j)=>{x.beginPath();x.strokeStyle=col;x.globalAlpha=.62;x.lineWidth=1.2;for(let px=0;px<w;px++){const env=.18+.82*Math.pow(Math.sin(px/w*Math.PI*3),2),y=h/2+Math.sin(px/w*18+p)*h*a*env*(.35+.65*Math.sin(px/w*Math.PI*2+j)**2);px?x.lineTo(px,y):x.moveTo(px,y)}x.stroke()});x.globalAlpha=1}addEventListener('resize',draw);draw();
function sync(){const fmt=mode==='video'?'MP4':'MP3',q=quality==='best'?'BEST':mode==='video'?quality+'P':quality+'K';$('#mMode').textContent=$('#kMode').textContent=mode.toUpperCase();$('#mQuality').textContent=$('#kQuality').textContent=q;$('#mFormat').textContent=$('#kFormat').textContent=fmt;$('#output').textContent=fmt+' · EMBED METADATA';document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode))}
function qualities(){const box=$('#qualities');box.innerHTML='';const list=mode==='video'?vq:aq;if(!list.includes(quality))quality=list[0];list.forEach(q=>{const b=document.createElement('button');b.textContent=q==='best'?'BEST':mode==='video'?q+'P':q+'K';b.className=q===quality?'active':'';b.onclick=()=>{quality=q;qualities();sync()};box.appendChild(b)});sync()}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;quality=mode==='video'?'best':'320';qualities()});qualities();$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')$('#analyse').click()});
$('#analyse').onclick=async()=>{const u=$('#url').value.trim();if(!u)return;$('#error').classList.remove('show');$('#meta').classList.remove('show');$('#mStatus').textContent='SCAN';$('#start').disabled=true;const fd=new FormData();fd.append('url',u);try{const r=await fetch('/api/info',{method:'POST',body:fd}),d=await r.json();if(!r.ok)throw new Error(d.error||'Analyse impossible');$('#meta').textContent=(d.title||'YouTube')+(d.uploader?' · '+d.uploader:'');$('#meta').classList.add('show');analysed=true;$('#start').disabled=false;$('#mStatus').textContent='READY';if(mode==='video'&&Array.isArray(d.qualities)&&d.qualities.length){const avail=vq.filter(q=>q==='best'||d.qualities.includes(Number(q)));if(!avail.includes(quality))quality=avail[0]||'best';const box=$('#qualities');box.innerHTML='';avail.forEach(q=>{const b=document.createElement('button');b.textContent=q==='best'?'BEST':q+'P';b.className=q===quality?'active':'';b.onclick=()=>{quality=q;document.querySelectorAll('#qualities button').forEach(x=>x.classList.remove('active'));b.classList.add('active');sync()};box.appendChild(b)});sync()}}catch(e){$('#error').textContent=e.message;$('#error').classList.add('show');$('#mStatus').textContent='ERROR'}};
$('#start').onclick=async()=>{if(!analysed)return;const fd=new FormData();fd.append('url',$('#url').value.trim());fd.append('mode',mode);fd.append('quality',mode==='video'?quality:'best');fd.append('audio_quality',mode==='audio'?quality:'320');$('#download').classList.add('show');$('#ready').classList.remove('show');$('#bar').style.width='0%';$('#mStatus').textContent='LOAD';const r=await fetch('/api/jobs',{method:'POST',body:fd}),d=await r.json();if(!r.ok){$('#error').textContent=d.error||'Job impossible';$('#error').classList.add('show');return}activeJob=d.job_id;clearInterval(timer);timer=setInterval(poll,800);poll()};
async function poll(){if(!activeJob)return;const r=await fetch('/api/jobs/'+activeJob),d=await r.json(),p=Math.max(0,Math.min(100,d.progress||0));$('#bar').style.width=p+'%';$('#jobPct').textContent=p+'%';$('#jobMsg').textContent=(d.message||d.stage||'').toUpperCase();if(d.status==='ready'){clearInterval(timer);$('#mStatus').textContent='DONE';$('#ready').href='/api/jobs/'+activeJob+'/download';$('#ready').classList.add('show')}else if(d.status==='error'){clearInterval(timer);$('#mStatus').textContent='ERROR';$('#error').textContent=d.error||d.message||'Erreur';$('#error').classList.add('show')}}
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.get("/health")
def health():
    cleanup_jobs()
    return {
        "status": "ok",
        "service": "panda-dl",
        "worker_configured": worker_enabled(),
        "video": {"format": "mp4", "qualities": VIDEO_QUALITIES},
        "audio": {"format": "mp3", "qualities": AUDIO_QUALITIES},
    }


@app.post("/api/info")
def info(url: str = Form(...)):
    try:
        data = worker_json("POST", "/info", {"url": url.strip(), "playlist_mode": False}, timeout=180)
        qualities = sorted(
            {int(fmt["height"]) for fmt in data.get("formats", []) if fmt.get("height") and fmt.get("vcodec") != "none"},
            reverse=True,
        )
        return {
            "title": data.get("title"),
            "uploader": data.get("uploader") or data.get("channel"),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "qualities": qualities,
        }
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/api/jobs")
def create_job(url: str = Form(...), mode: str = Form("video"), quality: str = Form("best"), audio_quality: str = Form("320")):
    cleanup_jobs()
    mode = "audio" if mode == "audio" else "video"
    quality = quality if quality in VIDEO_QUALITIES else "best"
    audio_quality = audio_quality if audio_quality in AUDIO_QUALITIES else "320"
    job_id = uuid.uuid4().hex
    now = time.time()
    with JOB_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "message": "En attente",
            "created": now,
            "updated": now,
            "error": None,
            "workdir": None,
            "result_path": None,
            "filename": None,
            "result_size": None,
        }
    EXECUTOR.submit(run_job, job_id, {"url": url.strip(), "mode": mode, "quality": quality, "audio_quality": audio_quality})
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    cleanup_jobs()
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return {k: v for k, v in job.items() if k not in {"workdir", "result_path"}}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job introuvable")
    if job.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Job non prêt")
    path = job.get("result_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="Fichier expiré")
    return FileResponse(path, media_type=job.get("media_type") or "application/octet-stream", filename=job.get("filename") or os.path.basename(path))
