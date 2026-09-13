# Dingo-dl

Dingo-dl combines the existing download/conversion engine with a playlist transfer/sync layer.

## Downloader

Existing yt-dlp / gallery-dl / FFmpeg features remain available on `/`.

## Playlist Sync

The sync UI is available on `/sync` and currently supports Spotify ↔ YouTube playlist transfer with OAuth, matching and transfer reports.

### OAuth bootstrap

After the Cloud Run service is deployed, run:

```bash
chmod +x configure-oauth.sh && ./configure-oauth.sh
```

The script:
- detects the deployed `dingo-dl` Cloud Run URL;
- enables the YouTube Data API and required GCP services;
- prints the exact Spotify and Google OAuth redirect URIs;
- prompts for Client IDs and Client Secrets;
- stores Client Secrets in Google Secret Manager;
- injects only Client IDs and the public URL as normal Cloud Run environment variables;
- grants the Cloud Run service account access to the OAuth secrets;
- updates the running service and prints `/api/sync/providers` status.

Never commit real OAuth secrets or cookie/session files.

### Redirect URIs

They are generated from the active Cloud Run URL:

```text
https://<cloud-run-host>/api/sync/oauth/spotify/callback
https://<cloud-run-host>/api/sync/oauth/youtube/callback
```

The registered URI must exactly match the URI used by the application.

### Secure persistence

When configured, Dingo Sync can use Firestore for connection persistence, transfer history and matching cache. OAuth tokens are encrypted before Firestore storage using `DINGO_TOKEN_KEY`, which should also come from Secret Manager.

See `.env.sync.example` for the environment variable names.
