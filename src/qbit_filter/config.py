"""Environment-driven configuration."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # MOVIE_CATEGORIES / TV_CATEGORIES arrive as CSV strings ("movies,films").
        # Without this, pydantic-settings tries to JSON-decode them on behalf of
        # the frozenset[str] fields before _parse_csv has a chance to run.
        enable_decoding=False,
    )

    qbittorrent_host: str = "http://localhost:8080"
    qbittorrent_username: str = "admin"
    qbittorrent_password: str = "adminadmin"

    listen_host: str = "127.0.0.1"
    listen_port: int = 8765

    poll_interval_seconds: float = 1.0

    movie_categories: frozenset[str] = Field(default=frozenset({"movies", "films"}))
    tv_categories: frozenset[str] = Field(default=frozenset({"tv", "shows", "anime"}))

    log_level: str = "INFO"

    # Enables the dev-only `/dev/version` poll + browser livereload script.
    # Set automatically by `nix run .#qbit-dev`; off in prod / docker.
    dev_mode: bool = False

    # Radarr / Sonarr integration. Empty URL disables that source; both being
    # empty turns the integration into a no-op (background task exits, no
    # sidebar section, *arr-aware rules surface zero candidates).
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    # *arr poll cadence. Library state changes far less often than qBit state,
    # so 60s is comfortable; the matching index also rebuilds on every qBit
    # store.rid bump so newly-added torrents pick up arr metadata quickly.
    arr_poll_interval_seconds: float = 60.0
    # When True (default), fall back to normalised-title+year lookup against
    # the arr movie/series list for torrents that don't have a tag and aren't
    # in arr's queue or recent history. Keeps coverage high at the cost of
    # rare false matches (e.g. "Avatar" 2009 vs "Avatar: The Last Airbender").
    # Set to False for strict downloadId-only matching.
    arr_title_fallback: bool = True

    @field_validator("movie_categories", "tv_categories", mode="before")
    @classmethod
    def _parse_csv(cls, v: object) -> frozenset[str]:
        if isinstance(v, str):
            return frozenset(p.strip().lower() for p in v.split(",") if p.strip())
        if isinstance(v, set | frozenset | list | tuple):
            return frozenset(str(p).strip().lower() for p in v if str(p).strip())
        return frozenset()
