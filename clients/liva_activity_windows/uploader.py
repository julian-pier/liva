from __future__ import annotations

import json
import logging
import ssl
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from . import __version__
from .config import AgentConfig
from .spool import ActivitySpool

LOG = logging.getLogger(__name__)


class BatchUploader:
    def __init__(self, config: AgentConfig, spool: ActivitySpool) -> None:
        self.config = config
        self.spool = spool

    def upload_once(self) -> bool:
        pending = self.spool.pending_with_revisions(self.config.batch_size)
        if not pending:
            return True
        events = [event for event, _revision in pending]
        body = json.dumps({
            "batch_id": str(uuid.uuid4()),
            "device": {"id": self.config.device_id, "name": self.config.device_name, "platform": "windows", "agent_version": __version__},
            "events": events,
        }).encode("utf-8")
        request = Request(
            self.config.server_url + "/api/activity/ingest", data=body, method="POST",
            headers={"Authorization": "Bearer " + self.config.api_key, "Content-Type": "application/json", "User-Agent": f"liva-activity-windows/{__version__}"},
        )
        try:
            tls_context = ssl.create_default_context(cafile=certifi.where())
            with urlopen(request, timeout=15, context=tls_context) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status < 200 or response.status >= 300 or not payload.get("ok"):
                    return False
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            LOG.warning("Activity upload postponed: %s", exc)
            return False
        self.spool.mark_synced_revisions([(event["id"], revision) for event, revision in pending])
        return True
