"""Rotation pointer + processed-checkout memory, persisted in state/state.json.

The file is committed back to the repo by the GitHub Action, so the rotation keeps
its place across runs no matter whose computer is on.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

DEFAULT_PATH = os.environ.get("STATE_PATH") or os.path.join("state", "state.json")
KEEP_PROCESSED_DAYS = 60


class State:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self.next_index = 0
        self.processed = {}      # checkout_id -> ISO timestamp it was assigned
        self.assigned_count = {}  # rep name -> total leads ever assigned
        self.last_run = None
        self._load()
        self._snapshot = self._fingerprint()

    def _fingerprint(self):
        """Everything that actually matters, so an idle run doesn't rewrite the file
        (and doesn't create a pointless commit every 15 minutes)."""
        return json.dumps(
            {"next_index": self.next_index,
             "processed": sorted(self.processed),
             "assigned_count": self.assigned_count},
            sort_keys=True)

    def _load(self):
        if not os.path.exists(self.path):
            log.info("No state file at %s - starting a fresh rotation", self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("state.json unreadable (%s); starting fresh", exc)
            return
        self.next_index = int(data.get("next_index", 0))
        self.processed = dict(data.get("processed", {}))
        self.assigned_count = dict(data.get("assigned_count", {}))
        self.last_run = data.get("last_run")

    def save(self, force=False):
        self._prune()
        if not force and self._fingerprint() == self._snapshot and os.path.exists(self.path):
            log.info("Nothing changed - leaving %s untouched", self.path)
            return
        self.last_run = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {
            "next_index": self.next_index,
            "last_run": self.last_run,
            "assigned_count": self.assigned_count,
            "processed": self.processed,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def _prune(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_PROCESSED_DAYS)
        kept = {}
        for checkout_id, stamp in self.processed.items():
            try:
                if datetime.fromisoformat(stamp) >= cutoff:
                    kept[checkout_id] = stamp
            except (TypeError, ValueError):
                kept[checkout_id] = stamp
        self.processed = kept

    # ------------------------------------------------------------- rotation
    def take_turn(self, reps):
        """Return the next rep in the rotation and advance the pointer."""
        if not reps:
            raise RuntimeError("No reps configured in config.json")
        index = self.next_index % len(reps)
        self.next_index = (index + 1) % len(reps)
        return reps[index]

    def mark_processed(self, checkout_id, rep_name):
        self.processed[str(checkout_id)] = datetime.now(timezone.utc).isoformat()
        self.assigned_count[rep_name] = self.assigned_count.get(rep_name, 0) + 1

    def is_processed(self, checkout_id):
        return str(checkout_id) in self.processed
