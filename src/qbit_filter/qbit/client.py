"""qBittorrent client factory with IP-auth-bypass workaround."""

from __future__ import annotations

import logging

import qbittorrentapi
from qbittorrentapi import LoginFailed

from qbit_filter.config import Settings

logger = logging.getLogger(__name__)


def connect(settings: Settings) -> qbittorrentapi.Client:
    """Return a logged-in qBittorrent client.

    qBittorrent with IP-auth-bypass returns 204 No Content on the login
    endpoint instead of the expected ``"Ok."`` body. The library raises
    ``LoginFailed`` even though the SID cookie was set. Validate the
    session via ``app_version()`` and only re-raise if THAT also fails.
    """
    client = qbittorrentapi.Client(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
        VERIFY_WEBUI_CERTIFICATE=False,
    )
    try:
        client.auth_log_in()
    except LoginFailed as exc:
        try:
            version = client.app_version()
        except Exception:
            logger.error("qBit auth failed and app_version() also failed")
            raise exc from None
        logger.info("qBit auth-bypass detected; verified via app_version=%s", version)
        return client
    logger.info("qBit auth ok")
    return client
