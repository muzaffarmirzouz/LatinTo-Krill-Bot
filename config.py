import logging
import os

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi. Railway Variables (yoki .env) ga BOT_TOKEN=... qo'shing."
    )


def _parse_int(value: str, name: str):
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        _log.warning("%s ichidagi '%s' butun son emas — o'tkazib yuborildi.", name, value)
        return None


_legacy_raw = (os.getenv("CHANNEL_ID") or "").strip()
LEGACY_CHANNEL_ID = _parse_int(_legacy_raw, "CHANNEL_ID") if _legacy_raw else None
LEGACY_CHANNEL_ID = LEGACY_CHANNEL_ID or None

OPERATOR_IDS = [
    n
    for n in (_parse_int(x, "ADMIN_IDS") for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip())
    if n is not None
]

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@namanganliklar_uz")
