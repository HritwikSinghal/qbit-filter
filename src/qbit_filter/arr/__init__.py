"""Radarr / Sonarr integration -- pluggable enrichment layer over the qBit store.

Boundaries:
- ``client.py`` is the only place that imports ``httpx``.
- ``models.py`` exposes trimmed dataclasses (just the fields we consume).
- ``sync.py`` owns the async polling loop.
- ``index.py`` is pure: snapshot + qBit torrents -> ``hash -> ArrMatch`` map.
The rest of the app reads through ``state/arr_store.ArrStore``.
"""
