"""
core/webhook.py — Webhook POST module for termux-cron.

Sends task execution results to a configured Webhook URL via HTTP POST
with a JSON payload. Uses only Python stdlib (urllib.request) to keep
dependencies minimal.
"""

from __future__ import annotations

import json
import logging
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

#: Default timeout in seconds for webhook POST requests.
_WEBHOOK_TIMEOUT: int = 5


def post_webhook(url: str, payload: dict) -> bool:
    """POST *payload* as JSON to *url* and return whether it succeeded.

    Parameters
    ----------
    url : str
        Target Webhook URL (e.g. a Discord webhook endpoint).
    payload : dict
        Task result payload. Expected keys per SPEC::

            {
                "task": str,
                "started_at": str (ISO8601),
                "finished_at": str (ISO8601),
                "exit_code": int | None,
                "duration_ms": int | None,
                "output": str | None
            }

    Returns
    -------
    bool
        ``True`` when the server responds with a **2xx** HTTP status,
        ``False`` on any network error, non-2xx status, timeout, or
        other exception.
    """
    if not url or not isinstance(url, str):
        logger.warning("webhook: invalid URL %r", url)
        return False

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "termux-cron/1.0",
            },
        )

        with urllib_request.urlopen(req, timeout=_WEBHOOK_TIMEOUT) as resp:
            logger.info("webhook: POST %s -> %d OK", url, resp.status)
            return True
    except HTTPError as exc:
        logger.warning("webhook: POST %s -> %d (non-2xx)", url, exc.code)
    except URLError as exc:
        logger.warning("webhook: POST %s failed (URLError: %s)", url, exc)
    except TimeoutError as exc:
        logger.warning("webhook: POST %s timed out (%ss)", url, _WEBHOOK_TIMEOUT)
    except ValueError as exc:
        logger.warning("webhook: POST %s invalid URL: %s", url, exc)
    except Exception as exc:
        logger.exception("webhook: POST %s raised unexpected %s", url, exc)

    return False
