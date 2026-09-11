# PANDA DL

Nouveau projet séparé de `panda-download`.

## Interface

Direction artistique inspirée d'un plug-in audio premium :

- bandeau supérieur bleu nuit ;
- 4 modules néon `MODE / QUALITY / FORMAT / STATUS` ;
- spectrogramme central ;
- commandes type transport ;
- console inférieure blanc/aluminium ;
- gros potentiomètres et VU-mètres ;
- responsive desktop + mobile.

## Fonctions

- YouTube uniquement ;
- VIDEO -> MP4 uniquement ;
- AUDIO -> MP3 uniquement ;
- vidéo : BEST / 2160p / 1440p / 1080p / 720p / 480p / 360p ;
- audio : 320 / 256 / 192 / 128 kbps ;
- métadonnées intégrées ;
- progression en direct ;
- téléchargement final depuis la même interface.

## yt-dlp

La session Chromium n'existe pas dans Cloud Shell. Elle existe sur la VM `panda-youtube-worker`. C'est donc cette VM qui exécute yt-dlp avec le profil connecté :

```bash
yt-dlp \
  --cookies-from-browser "chromium:$HOME/chrome-profile" \
  -f "bv*+ba/b" \
  --merge-output-format mp4 \
  --embed-metadata \
  -o "$HOME/Downloads/%(title)s.%(ext)s" \
  "https://www.youtube.com/watch?v=fwLMySFVEAA"
```

PANDA DL ne cherche jamais les cookies Chromium dans Cloud Shell. Le site appelle le worker privé déjà authentifié.

## Déploiement

Depuis Cloud Shell :

```bash
git clone https://github.com/N4rutoZOO/panda-dl.git
cd panda-dl
chmod +x deploy.sh
./deploy.sh
```

Le service Cloud Run s'appelle `panda-dl`, donc il reçoit une nouvelle adresse `run.app`, distincte de l'ancien site.

Le projet GCP reste volontairement `project-017b13a1-e57e-4723-a2b` par défaut afin de réutiliser le worker privé existant sans réauthentifier Google. Le code et le repo sont entièrement séparés. Un nouveau projet GCP pourra être créé ensuite en migrant le worker/VPC.
