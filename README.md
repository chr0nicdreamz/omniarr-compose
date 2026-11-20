# OmniArr

OmniArr is a lightweight, self-hosted media manager built with FastAPI and SQLite that unifies **Movie** and **TV Show** tracking, Prowlarr searching, qBittorrent download automation, media server library refreshes (**Plex**, **Jellyfin**, and **Emby**), and **Radarr/Sonarr v3 API compatibility** for **Seerr** (formerly Jellyseerr/Overseerr).

---

## Features

- **Unified Library & Pagination:** Fast SQLite (WAL mode) backend with independent pagination for Movies and TV Shows, instant title filtering, and on-demand AJAX episode drawers.
- **Automated Search & Grab:** Connects to **Prowlarr** to search indexers on a schedule or on demand, automatically sending best-matched releases to **qBittorrent**.
- **Smart Import & Renaming:** Copies or moves completed downloads into structured media folders and triggers targeted library scans on **Plex**, **Jellyfin**, and/or **Emby**.
- **Native Seerr Integration:** Implements Radarr v3 and Sonarr v3 endpoints (`/api/v3/*`) so Seerr can connect directly as both your Movie and TV backend.
- **Built-in Credential Security:** Masks API keys and tokens in the web UI with reveal toggles, clipboard copy, and one-click OmniArr API key rotation.

---

## Quick Start (Docker Compose)

1. **Clone the repository:**

```bash
git clone https://github.com/chr0nicdreamz/omniarr-release.git omniarr
cd omniarr
```

2. **Configure `docker-compose.yml`** to match your local download and media directories:

```yaml
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
```

3. **Start the container:**

```bash
docker compose up -d --build
```

4. **Open the Web UI:**
   Navigate to `http://<your-server-ip>:8585`, open **Media Management** to configure your root folders, and open **Connect & General** to verify your service connections and rotate your `OMNIARR_API_KEY`.

---

## Bare-Metal / LXC Installation (Python 3.11+)

```bash
git clone https://github.com/chr0nicdreamz/omniarr-release.git /opt/omniarr
cd /opt/omniarr
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8585 --workers 2
```

> **Note:** OmniArr uses SQLite WAL mode and an exclusive file lock for background jobs, making it safe to run with `--workers 2`.

---

## Environment Variables

All settings below act as initial defaults and can be updated anytime from the web UI under **Connect & General** and **Media Management** (persisted in `/app/config/omniarr.db`).

| Variable | Default | Description |
| :--- | :--- | :--- |
| `OMNIARR_API_KEY` | `change_me_to_a_secure_key` | API key required by external clients such as Seerr (`X-Api-Key` or `?apikey=`). Can be rotated in the UI. |
| `PROWLARR_URL` | `http://localhost:9696` | Base URL of your Prowlarr instance. |
| `PROWLARR_API_KEY` | `""` | Prowlarr API key used for indexer syncing and automated searches. |
| `QBIT_URL` | `http://localhost:8080` | Base URL of your qBittorrent Web UI. |
| `QBIT_USER` | `""` | qBittorrent Web UI username. |
| `QBIT_PASS` | `""` | qBittorrent Web UI password. |
| `QBIT_REMOTE_PATH` | `/downloads` | Download path reported by qBittorrent (used for remote path mapping). |
| `QBIT_LOCAL_MOUNT` | `/downloads` | Corresponding local path mounted inside OmniArr where completed downloads reside. |
| `PLEX_URL` | `http://localhost:32400` | Base URL of your Plex Media Server. |
| `PLEX_TOKEN` | `""` | `X-Plex-Token` for triggering library scans on grab/import. |
| `PLEX_NOTIFY_GRAB` | `false` | Trigger Plex scan when a release is grabbed (`true` / `false`). |
| `PLEX_NOTIFY_IMPORT` | `true` | Trigger Plex scan when a file finishes importing (`true` / `false`). |
| `JELLYFIN_URL` | `""` | Base URL of your Jellyfin server (optional). |
| `JELLYFIN_API_KEY` | `""` | Jellyfin API key for triggering library refreshes. |
| `EMBY_URL` | `""` | Base URL of your Emby server (optional). |
| `EMBY_API_KEY` | `""` | Emby API key for triggering library refreshes. |

---

## Connecting OmniArr to Seerr

OmniArr exposes Radarr v3 and Sonarr v3 compatible endpoints, allowing a single OmniArr instance to handle both **Movies** and **TV Shows** in Seerr.

1. In OmniArr, go to **Connect & General → General & Seerr API Security** and copy your **OmniArr API Key** (click **↻ Rotate Key** first if you have not replaced the default key).
2. **Add Movies (Radarr Slot):**
   - Open **Seerr → Settings → Services → Radarr → Add Radarr Server**.
   - **Hostname or IP Address:** `<omniarr-ip-or-hostname>`
   - **Port:** `8585`
   - **API Key:** Paste your **OmniArr API Key**
   - **SSL:** Unchecked *(unless behind an HTTPS reverse proxy)*
   - **URL Base:** Leave blank
   - Click **Test**, select your **Quality Profile** and **Root Folder**, and click **Save Changes**.
3. **Add TV Shows (Sonarr Slot):**
   - Open **Seerr → Settings → Services → Sonarr → Add Sonarr Server**.
   - Enter the exact same **Hostname**, **Port (`8585`)**, and **API Key**.
   - Click **Test**, select your **Quality Profile**, **Root Folder**, and **Language Profile**, and click **Save Changes**.
