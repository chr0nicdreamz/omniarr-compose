# OmniArr

OmniArr is a lightweight, self-hosted media manager built with FastAPI and SQLite that unifies Movie and TV Show tracking, Prowlarr searching, qBittorrent download automation, media server library refreshes (Plex, Jellyfin, and Emby), and Radarr/Sonarr v3 API compatibility for Seerr (formerly Jellyseerr/Overseerr).

## Features

- Unified Library and Pagination: Fast SQLite (WAL mode) backend with independent pagination for Movies and TV Shows, instant title filtering, and on-demand AJAX episode drawers.
- Automated Search and Grab: Connects to Prowlarr to search indexers on a schedule or on demand, automatically sending best-matched releases to qBittorrent.
- Smart Import and Renaming: Copies or moves completed downloads into structured media folders and triggers targeted library scans on Plex, Jellyfin, and/or Emby.
- Native Seerr Integration: Implements Radarr v3 and Sonarr v3 endpoints (/api/v3/*) so Seerr can connect directly as both your Movie and TV backend.
- Built-in Credential Security: Masks API keys and tokens in the web UI with reveal toggles, clipboard copy, and one-click OmniArr API key rotation.
- Widescreen UI and Poster Density Controls: Fluid widescreen layouts with selectable poster grid columns (Auto, 6, 8, 10, 12) and expanded multi-column media detail views.
- Release Calendar and Air-Date Syncing: Color-coded calendar with All Media / Movies / TV Shows and status filtering, backed by Plex metadata, TMDB (US digital/physical dates), and iTunes fallback.
- Automation, TRaSH Presets and Bulk Operations: Scheduled hunting and quality-cutoff upgrades, one-click TRaSH-Guides custom format presets, bulk library selection and storage drive moves, Tdarr queue triggers, and a live qBittorrent download ticker.

## Quick Start (Docker Compose)

1. Clone the repository:

git clone https://github.com/chr0nicdreamz/omniarr-release.git omniarr
cd omniarr

2. Configure docker-compose.yml to match your local download and media directories:

services:
  omniarr:
    build: .
    container_name: omniarr
    restart: unless-stopped
    ports:
      - "8585:8585"
    environment:
      - OMNIARR_API_KEY=change_me_to_a_secure_key
      - PROWLARR_URL=http://prowlarr:9696
      - PROWLARR_API_KEY=
      - QBIT_URL=http://qbittorrent:8080
      - QBIT_USER=admin
      - QBIT_PASS=adminadmin
      - QBIT_REMOTE_PATH=/downloads
      - QBIT_LOCAL_MOUNT=/downloads
      - PLEX_URL=http://plex:32400
      - PLEX_TOKEN=
      - JELLYFIN_URL=
      - JELLYFIN_API_KEY=
      - EMBY_URL=
      - EMBY_API_KEY=
    volumes:
      - ./config:/app/config
      - /path/to/downloads:/downloads
      - /path/to/media/movies:/media/movies
      - /path/to/media/tv:/media/tv

3. Start the container:

docker compose up -d --build

4. Open the Web UI: Navigate to http://<your-server-ip>:8585, open Media Management to configure your root folders, and open Connect and General to verify your service connections and rotate your OMNIARR_API_KEY.

## Bare-Metal / LXC Installation (Python 3.11+)

git clone https://github.com/chr0nicdreamz/omniarr-release.git /opt/omniarr
cd /opt/omniarr
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8585 --workers 2

Note: OmniArr uses SQLite WAL mode and an exclusive file lock for background jobs, making it safe to run with --workers 2.

## Environment Variables

All settings below act as initial defaults and can be updated anytime from the web UI under Connect and General and Media Management (persisted in /app/config/omniarr.db).

- OMNIARR_API_KEY (default: change_me_to_a_secure_key): API key required by external clients such as Seerr (X-Api-Key or ?apikey=). Can be rotated in the UI.
- PROWLARR_URL (default: http://localhost:9696): Base URL of your Prowlarr instance.
- PROWLARR_API_KEY (default: ""): Prowlarr API key used for indexer syncing and automated searches.
- QBIT_URL (default: http://localhost:8080): Base URL of your qBittorrent Web UI.
- QBIT_USER (default: ""): qBittorrent Web UI username.
- QBIT_PASS (default: ""): qBittorrent Web UI password.
- QBIT_REMOTE_PATH (default: /downloads): Download path reported by qBittorrent (used for remote path mapping).
- QBIT_LOCAL_MOUNT (default: /downloads): Corresponding local path mounted inside OmniArr where completed downloads reside.
- PLEX_URL (default: http://localhost:32400): Base URL of your Plex Media Server.
- PLEX_TOKEN (default: ""): X-Plex-Token for triggering library scans on grab/import.
- PLEX_NOTIFY_GRAB (default: false): Trigger Plex scan when a release is grabbed (true / false).
- PLEX_NOTIFY_IMPORT (default: true): Trigger Plex scan when a file finishes importing (true / false).
- JELLYFIN_URL (default: ""): Base URL of your Jellyfin server (optional).
- JELLYFIN_API_KEY (default: ""): Jellyfin API key for triggering library refreshes.
- EMBY_URL (default: ""): Base URL of your Emby server (optional).
- EMBY_API_KEY (default: ""): Emby API key for triggering library refreshes.

## Connecting OmniArr to Seerr

OmniArr exposes Radarr v3 and Sonarr v3 compatible endpoints, allowing a single OmniArr instance to handle both Movies and TV Shows in Seerr.

1. In OmniArr, go to Connect and General -> General and Seerr API Security and copy your OmniArr API Key (click Rotate Key first if you have not replaced the default key).
2. Add Movies (Radarr Slot):
   - Open Seerr -> Settings -> Services -> Radarr -> Add Radarr Server.
   - Hostname or IP Address: <omniarr-ip-or-hostname>
   - Port: 8585
   - API Key: Paste your OmniArr API Key
   - SSL: Unchecked (unless behind an HTTPS reverse proxy)
   - URL Base: Leave blank
   - Click Test, select your Quality Profile and Root Folder, and click Save Changes.
3. Add TV Shows (Sonarr Slot):
   - Open Seerr -> Settings -> Services -> Sonarr -> Add Sonarr Server.
   - Enter the exact same Hostname, Port (8585), and API Key.
   - Click Test, select your Quality Profile, Root Folder, and Language Profile, and click Save Changes.

## Troubleshooting

- Service connection tests fail or status dots stay red/gray: Confirm the service URL includes http:// or https:// without extra path segments, run the live Test button in Settings, verify API keys or credentials, and save the configuration.
- Completed downloads do not import: Check that QBIT_REMOTE_PATH and QBIT_LOCAL_MOUNT (or your path mappings) match the actual paths accessible to OmniArr and that hardlink/copy permissions are valid.
- Movies do not show up on the Calendar: Open Calendar, select All Media or Movies, and click Sync Air Dates so OmniArr can pull release dates from Plex metadata, TMDB, or iTunes.
- Automated hunts or upgrades do not trigger: Check Settings -> Profiles and Custom Formats to ensure automated hunting (and quality-cutoff upgrades, if desired) is enabled, ensure items are monitored, and verify seeder/size filters are not excluding valid releases.
- Stale UI behavior after updates: Hard-refresh the browser (Ctrl+F5 or Cmd+Shift+R) and check service logs (journalctl -u omniarr -n 100 --no-pager or docker compose logs -f omniarr) if a backend error occurs.
