import json
import os
import time
from typing import Optional


class AuditLogger:
    def __init__(self, log_path: str):
        self.log_path = log_path

    def _write(self, entry: dict):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        entry["timestamp"] = time.time()
        entry["iso_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry["timestamp"]))
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def log_approval(
        self,
        event_id: str,
        npub: str,
        mint_url: str,
        quote_id: str,
        amount: int,
        unit: str,
        success: bool,
        error: Optional[str] = None,
    ):
        self._write({
            "type": "approval",
            "event_id": event_id,
            "npub": npub,
            "mint_url": mint_url,
            "quote_id": quote_id,
            "amount": amount,
            "unit": unit,
            "success": success,
            "error": error,
        })

    def log_event(self, event: dict):
        self._write(event)

    def read_recent(self, count: int = 100) -> list[dict]:
        if not os.path.exists(self.log_path):
            return []
        entries = []
        with open(self.log_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries[-count:]
