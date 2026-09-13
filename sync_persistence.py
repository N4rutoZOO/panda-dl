import hashlib
import json
import os
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # optional until requirements are installed
    Fernet = None
    InvalidToken = Exception

try:
    from google.cloud import firestore
except Exception:  # optional until requirements are installed
    firestore = None


PROVIDERS = ("spotify", "youtube")


def _truthy(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(str(sid).encode("utf-8")).hexdigest()


def _cache_id(provider: str, track: dict[str, Any], normalizer) -> str:
    isrc = str(track.get("isrc") or "").upper().strip()
    if isrc:
        raw = f"{provider}|isrc|{isrc}"
    else:
        title = normalizer(str(track.get("title") or ""))
        artist = normalizer(str(track.get("artist") or ""))
        duration = int((int(track.get("duration_ms") or 0) + 500) / 1000)
        raw = f"{provider}|meta|{title}|{artist}|{duration}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SyncStore:
    def __init__(self):
        self.enabled = _truthy("DINGO_FIRESTORE_ENABLED", "1")
        self.project = os.getenv("DINGO_FIRESTORE_PROJECT", "").strip() or None
        self.cache_ttl = max(3600, min(int(os.getenv("DINGO_MATCH_CACHE_TTL", "1209600")), 7776000))
        self.empty_ttl = max(300, min(int(os.getenv("DINGO_MATCH_EMPTY_TTL", "86400")), 604800))
        self.last_error = ""
        self._lock = threading.RLock()
        self._mem_connections: dict[tuple[str, str], dict[str, Any]] = {}
        self._mem_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._mem_history: dict[str, dict[str, Any]] = {}

        self._fernet = None
        key = os.getenv("DINGO_TOKEN_KEY", "").strip()
        if key and Fernet is not None:
            try:
                self._fernet = Fernet(key.encode("ascii"))
            except Exception as exc:
                self.last_error = f"DINGO_TOKEN_KEY invalide: {exc}"

        self.client = None
        if self.enabled and firestore is not None:
            try:
                self.client = firestore.Client(project=self.project)
            except Exception as exc:
                self.last_error = f"Firestore indisponible: {exc}"

    @property
    def firestore_ready(self) -> bool:
        return self.client is not None

    @property
    def secure_tokens(self) -> bool:
        return self.firestore_ready and self._fernet is not None

    def status(self) -> dict[str, Any]:
        return {
            "firestore_enabled": self.enabled,
            "firestore_ready": self.firestore_ready,
            "secure_token_storage": self.secure_tokens,
            "token_key_configured": self._fernet is not None,
            "project": self.project,
            "match_cache_ttl_seconds": self.cache_ttl,
            "empty_cache_ttl_seconds": self.empty_ttl,
            "fallback": "memory",
            "last_error": self.last_error or None,
        }

    def _conn_doc_id(self, sid: str, provider: str) -> str:
        return hashlib.sha256(f"{_sid_hash(sid)}|{provider}".encode()).hexdigest()

    def save_connection(self, sid: str, provider: str, token: dict[str, Any]) -> None:
        clean = dict(token)
        with self._lock:
            self._mem_connections[(sid, provider)] = clean
        if not self.secure_tokens:
            return
        try:
            plaintext = json.dumps(clean, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ciphertext = self._fernet.encrypt(plaintext).decode("ascii")
            self.client.collection("dingo_sync_connections").document(self._conn_doc_id(sid, provider)).set({
                "sid_hash": _sid_hash(sid),
                "provider": provider,
                "ciphertext": ciphertext,
                "expires_at": float(clean.get("expires_at") or 0),
                "updated_at": time.time(),
            })
        except Exception as exc:
            self.last_error = f"Écriture connexion Firestore: {exc}"

    def get_connection(self, sid: str, provider: str) -> dict[str, Any] | None:
        if self.secure_tokens:
            try:
                snap = self.client.collection("dingo_sync_connections").document(self._conn_doc_id(sid, provider)).get()
                if snap.exists:
                    payload = snap.to_dict() or {}
                    plaintext = self._fernet.decrypt(str(payload.get("ciphertext") or "").encode("ascii"))
                    token = json.loads(plaintext.decode("utf-8"))
                    with self._lock:
                        self._mem_connections[(sid, provider)] = token
                    return dict(token)
            except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
                self.last_error = f"Déchiffrement connexion: {exc}"
            except Exception as exc:
                self.last_error = f"Lecture connexion Firestore: {exc}"
        with self._lock:
            value = self._mem_connections.get((sid, provider))
            return dict(value) if value else None

    def delete_connection(self, sid: str, provider: str) -> None:
        with self._lock:
            self._mem_connections.pop((sid, provider), None)
        if self.firestore_ready:
            try:
                self.client.collection("dingo_sync_connections").document(self._conn_doc_id(sid, provider)).delete()
            except Exception as exc:
                self.last_error = f"Suppression connexion Firestore: {exc}"

    def connected_providers(self, sid: str) -> list[str]:
        return [provider for provider in PROVIDERS if self.get_connection(sid, provider)]

    def get_match(self, cache_id: str) -> list[dict[str, Any]] | None:
        now = time.time()
        if self.firestore_ready:
            try:
                snap = self.client.collection("dingo_sync_match_cache").document(cache_id).get()
                if snap.exists:
                    data = snap.to_dict() or {}
                    if float(data.get("expires_at") or 0) > now:
                        return list(data.get("candidates") or [])
                    snap.reference.delete()
            except Exception as exc:
                self.last_error = f"Lecture cache Firestore: {exc}"
        with self._lock:
            item = self._mem_cache.get(cache_id)
            if item and item[0] > now:
                return list(item[1])
            self._mem_cache.pop(cache_id, None)
        return None

    def save_match(self, cache_id: str, provider: str, candidates: list[dict[str, Any]]) -> None:
        ttl = self.cache_ttl if candidates else self.empty_ttl
        expires_at = time.time() + ttl
        safe_candidates = list(candidates)[:10]
        with self._lock:
            self._mem_cache[cache_id] = (expires_at, safe_candidates)
        if self.firestore_ready:
            try:
                self.client.collection("dingo_sync_match_cache").document(cache_id).set({
                    "provider": provider,
                    "candidates": safe_candidates,
                    "expires_at": expires_at,
                    "updated_at": time.time(),
                })
            except Exception as exc:
                self.last_error = f"Écriture cache Firestore: {exc}"

    def save_history(self, sid: str, job: dict[str, Any], req: dict[str, Any]) -> None:
        record = {
            "job_id": job.get("id"),
            "sid_hash": _sid_hash(sid),
            "status": job.get("status"),
            "source": req.get("source"),
            "destination": req.get("destination"),
            "total": int(job.get("total") or 0),
            "matched": int(job.get("matched") or 0),
            "unmatched": int(job.get("unmatched") or 0),
            "created_at": float(job.get("created_at") or time.time()),
            "finished_at": time.time(),
        }
        job_id = str(record.get("job_id") or hashlib.sha256(os.urandom(16)).hexdigest())
        with self._lock:
            self._mem_history[job_id] = record
        if self.firestore_ready:
            try:
                self.client.collection("dingo_sync_history").document(job_id).set(record)
            except Exception as exc:
                self.last_error = f"Écriture historique Firestore: {exc}"

    def history(self, sid: str, limit: int = 30) -> list[dict[str, Any]]:
        sid_h = _sid_hash(sid)
        rows: list[dict[str, Any]] = []
        if self.firestore_ready:
            try:
                docs = self.client.collection("dingo_sync_history").where("sid_hash", "==", sid_h).stream()
                rows = [d.to_dict() or {} for d in docs]
            except Exception as exc:
                self.last_error = f"Lecture historique Firestore: {exc}"
        if not rows:
            with self._lock:
                rows = [dict(v) for v in self._mem_history.values() if v.get("sid_hash") == sid_h]
        rows.sort(key=lambda x: float(x.get("finished_at") or 0), reverse=True)
        for row in rows:
            row.pop("sid_hash", None)
        return rows[: max(1, min(limit, 100))]


class ProviderProxy:
    def __init__(self, store: SyncStore, sid: str):
        self.store = store
        self.sid = sid

    def __setitem__(self, provider: str, token: dict[str, Any]):
        self.store.save_connection(self.sid, provider, token)

    def __getitem__(self, provider: str):
        value = self.store.get_connection(self.sid, provider)
        if value is None:
            raise KeyError(provider)
        return value

    def get(self, provider: str, default=None):
        return self.store.get_connection(self.sid, provider) or default

    def pop(self, provider: str, default=None):
        old = self.store.get_connection(self.sid, provider)
        self.store.delete_connection(self.sid, provider)
        return old if old is not None else default

    def keys(self):
        return self.store.connected_providers(self.sid)


class ConnectionMap:
    def __init__(self, store: SyncStore):
        self.store = store

    def get(self, sid: str, default=None):
        return ProviderProxy(self.store, sid)

    def setdefault(self, sid: str, default=None):
        return ProviderProxy(self.store, sid)


STORE = SyncStore()
extra_router = APIRouter(prefix="/api/sync", tags=["playlist-sync-storage"])


@extra_router.get("/storage")
def storage_status():
    return STORE.status()


@extra_router.get("/history")
def sync_history(request: Request, limit: int = 30):
    sid = request.session.get("sync_sid")
    if not sid:
        return {"history": []}
    return {"history": STORE.history(sid, limit)}


def install(module):
    module.CONNECTIONS = ConnectionMap(STORE)

    original_search = module._provider_search

    def cached_search(provider: str, sid: str, track: dict[str, Any]):
        cache_id = _cache_id(provider, track, module._norm)
        cached = STORE.get_match(cache_id)
        if cached is not None:
            return cached
        results = original_search(provider, sid, track)
        STORE.save_match(cache_id, provider, results)
        return results

    module._provider_search = cached_search

    original_run = module._run_transfer

    def persistent_run(job_id: str, sid: str, req: dict[str, Any]):
        try:
            return original_run(job_id, sid, req)
        finally:
            try:
                with module.SYNC_LOCK:
                    job = dict(module.SYNC_JOBS.get(job_id) or {})
                if job:
                    STORE.save_history(sid, job, req)
            except Exception as exc:
                STORE.last_error = f"Historique sync: {exc}"

    module._run_transfer = persistent_run
    return extra_router
