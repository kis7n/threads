"""История постов — чтобы не повторяться и знать, что уже улетело в Threads."""

import json
import logging
import os
import time
from datetime import datetime, timezone

import config

log = logging.getLogger(__name__)

STATUS_PUBLISHED = "published"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class History:
    def __init__(self, path=None):
        self.path = path or config.HISTORY_PATH
        self._entries = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._entries, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, text, angle=None, status=STATUS_PUBLISHED, post_id=None, note=None):
        self._entries.append(
            {
                "text": text,
                "angle": angle,
                "status": status,
                "post_id": post_id,
                "note": note,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ts": time.time(),
            }
        )
        self._save()

    def published_texts(self, limit=40):
        """Тексты уже опубликованных постов, свежие первыми."""
        published = [
            entry["text"]
            for entry in reversed(self._entries)
            if entry.get("status") == STATUS_PUBLISHED and entry.get("text")
        ]
        return published[:limit]

    def published_count(self):
        return sum(1 for e in self._entries if e.get("status") == STATUS_PUBLISHED)

    def __len__(self):
        return len(self._entries)
