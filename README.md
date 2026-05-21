# qbit-filter

A local web UI that groups [qBittorrent](https://www.qbittorrent.org/) torrents by movie or TV show. Parses release titles with [guessit](https://github.com/guessit-io/guessit), aggregates files belonging to the same title (e.g. all episodes of a season), and exposes a single-page HTMX UI with live SSE updates, multi-select filter chips (status / category / tags / trackers), and per-torrent management actions (pause, resume, recheck, delete, tag).

Built with FastAPI + Jinja2 + HTMX. Ships as a Docker image and a Nix flake.

## Quick start

### Docker

```sh
cp .env.example .env                 # edit QBITTORRENT_HOST
docker compose up -d                 # http://127.0.0.1:8080
docker compose logs -f qbit-filter
docker compose down
```

The container listens on `8765` internally; compose forwards host `8080 -> 8765`. `.env` is read at runtime via `env_file`, so credential or category changes apply on `docker compose restart` without a rebuild.

### Nix

```sh
cp .env.example .env
nix run .#qbit                       # uv host run on http://127.0.0.1:8765
nix run .#qbit-docker                # docker compose up --build on http://127.0.0.1:8080
```

### Local Python

Requires Python 3.13 and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
.venv/bin/python -m qbit_filter      # http://127.0.0.1:8765
```

## Configuration

All configuration is via `.env`; see `.env.example` for the supported keys (qBittorrent connection, listen address, polling interval, movie / TV category names, log level). qBittorrent instances behind an IP auth bypass accept any non-empty placeholder for `QBITTORRENT_USERNAME` / `QBITTORRENT_PASSWORD`.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
