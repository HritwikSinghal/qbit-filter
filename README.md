# qbit-filter

A local web UI for [qBittorrent](https://www.qbittorrent.org/) built around an
extensible set of cleanup rule presets. Groups torrents by movie / TV show,
runs a rule against the catalogue, shows the matched groups in a keeper /
flagged compare strip, and bulk-deletes after confirmation.

Built with FastAPI + Jinja2 + HTMX. Ships as a Docker image and a Nix flake.

## Quick start

### Docker

```sh
cp .env.example .env                 # edit QBITTORRENT_HOST + optional RADARR/SONARR
docker compose up -d                 # http://127.0.0.1:8080
docker compose logs -f qbit-filter
docker compose down
```

Container listens on `8765` internally; compose forwards host `8080 -> 8765`.
`.env` is read at runtime via `env_file` -- credential / category / arr-key
changes apply on `docker compose restart` without a rebuild.

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

Set `DEV_MODE=true` in `.env` for uvicorn `--reload` on source changes and a
livereload script that reloads the page on worker restart.

## Configuration

All configuration is via `.env`; see [`.env.example`](.env.example) for the
full key list. qBittorrent instances behind an IP auth bypass accept any
non-empty placeholder for `QBITTORRENT_USERNAME` / `QBITTORRENT_PASSWORD`.

Radarr / Sonarr integration is optional -- leave both URLs blank to disable.

## Further reading

- [`CLAUDE.md`](CLAUDE.md) -- project goal, architecture quick reference,
  conventions.
- [`docs/progress.md`](docs/progress.md) -- live status, recent changes,
  pickup priorities.
- [`docs/todo.md`](docs/todo.md) -- wishlist and open ideas.
- [`docs/web-ui-design-principles.md`](docs/web-ui-design-principles.md) --
  reusable UI design reference.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
