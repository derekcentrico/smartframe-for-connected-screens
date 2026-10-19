"""
Home Assistant helpers: motion state and status lines.
"""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .config import HA_URL, HA_TOKEN

logger = logging.getLogger(__name__)


def _frame_prefix(frame_name: str) -> str:
    """Convert frame name to a valid env-var segment (e.g. ``living-room`` -> ``LIVING_ROOM``)."""
    return re.sub(r"[^A-Z0-9]", "_", frame_name.strip().upper())


def _ha_headers():
    if not HA_URL or not HA_TOKEN:
        return None
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


def _ha_get_state(entity_id: str):
    """Low-level HA state fetch; returns JSON dict or None."""
    if not HA_URL or not HA_TOKEN:
        return None
    headers = _ha_headers()
    if not headers:
        return None
    try:
        url = f"{HA_URL.rstrip('/')}/api/states/{entity_id}"
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code != 200:
            logger.warning("HA returned %d for entity %s", r.status_code, entity_id)
            return None
        return r.json()
    except Exception:
        logger.warning("Failed to fetch HA state for %s", entity_id, exc_info=True)
        return None


def fetch_motion_state(frame_name: str):
    """
    Get motion/display state for a frame.

    Uses ``HA_<FRAME>_MOTION_ENTITY``; falls back to ``HA_MOTION_ENTITY``.
    Returns ``{"state": "on"}`` when not configured or HA is unavailable.
    """
    prefix = _frame_prefix(frame_name)
    motion_env = f"HA_{prefix}_MOTION_ENTITY"

    entity_id = os.environ.get(motion_env)
    if not entity_id:
        entity_id = os.environ.get("HA_MOTION_ENTITY")

    if not entity_id:
        return {"state": "on"}

    data = _ha_get_state(entity_id)
    if not data:
        return {"state": "on"}

    st = str(data.get("state", "")).lower()
    if st in ("off", "unavailable", "unknown"):
        return {"state": "off"}
    if st == "on":
        return {"state": "on"}

    return {"state": "on"}


def fetch_ha_status(frame_name: str):
    """
    Fetch up to 3 status lines for a frame from HA in parallel.

    Env vars per frame:
      ``HA_<FRAME>_STATUS_ENTITY_1..3``
      ``HA_<FRAME>_STATUS_LABEL_1..3``
    """
    prefix = _frame_prefix(frame_name)

    if not HA_URL or not HA_TOKEN:
        return []

    headers = _ha_headers()
    if not headers:
        return []

    entity_configs = []
    for idx in (1, 2, 3):
        ent_key = f"HA_{prefix}_STATUS_ENTITY_{idx}"
        entity_id = os.environ.get(ent_key)
        if entity_id:
            entity_configs.append((idx, entity_id))

    if not entity_configs:
        return []

    def _fetch_one(idx, entity_id):
        label_key = f"HA_{prefix}_STATUS_LABEL_{idx}"
        data = _ha_get_state(entity_id)
        if not data:
            return None

        state = str(data.get("state", ""))
        if state.lower() in ("unavailable", "unknown", ""):
            return None

        attrs = data.get("attributes", {}) or {}
        label = os.environ.get(label_key) or attrs.get("friendly_name") or entity_id

        return {
            "_idx": idx,
            "entity_id": entity_id,
            "label": label,
            "value": state,
        }

    items = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fetch_one, idx, eid): idx
            for idx, eid in entity_configs
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    items.append(result)
            except Exception:
                logger.warning("HA status fetch failed", exc_info=True)

    items.sort(key=lambda x: x["_idx"])

    # Remove internal sort key before returning
    for item in items:
        item.pop("_idx", None)

    return items
