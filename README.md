# Omniarr

Setup & Wiring Instructions
1. Deploy the Container: Run docker compose up -d --build inside the omniarr/ directory. Access the UI at http://<docker-host-ip>:8585.
2. Connect Seerr: In Seerr's Settings → Services, add both a Radarr server and a Sonarr server pointing to http://omniarr:8585 (or your host IP) using your OMNIARR_API_KEY. Seerr will read the quality profiles and root folders directly from OmniArr's compatibility endpoints.
3. Sync Prowlarr: Ensure your indexers are configured in Prowlarr; OmniArr queries Prowlarr's /api/v1/search directly so you don't have to manage per-app indexer syncs.
