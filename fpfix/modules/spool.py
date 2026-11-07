"""
On-disk store-and-forward queue for MQTT events.

Why this exists
---------------
A scan that is captured but never reaches the server is an employee who clocked in and
is reported absent. That happened for real: SP Wielbark published 34 fingerprint events
between 2026-11-05 15:06 and 2026-11-06 15:02 UTC and not one of them was stored, while
every other site recorded normally. Three things combined:

  * DNS on that device failed ("Name or service not known"), so the broker connection
    dropped;
  * the reconnect loop gave up after a fixed number of attempts and never tried again,
    so a drop of a few minutes became an outage of a day;
  * QoS 1 only survives a disconnect for as long as the broker keeps the session, and
    SessionExpiryInterval was 120 seconds, so everything queued was discarded.

None of it was visible, because the publish result was never inspected and the log said
"published successfully" either way.

QoS 1 and a longer session expiry help with a brief drop. They cannot help with an outage
longer than the broker's session limit, and they cannot survive the application
restarting. Only a copy on disk does both, which is what this module keeps.

The contract
------------
An event is written here BEFORE it is published and removed only when the broker
acknowledges it (PUBACK, surfaced by paho as on_publish). Anything still on disk is
therefore either in flight or lost, and is safe to send again - the server derives
attendance from the scan it receives, so a duplicate costs a repeated row rather than a
wrong answer, while a dropped event costs a person their shift.

The spool is bounded. A device that is offline for a week must not fill its filesystem
and take the sensor down with it, so the oldest entries are dropped once a limit is
reached, and that is logged as the data loss it is.
"""

import json
import os
import threading
import time


class EventSpool:
    """A bounded directory of events awaiting acknowledgement, oldest first."""

    def __init__(self, directory, logger, max_files=500, max_age_seconds=7 * 24 * 3600):
        self.directory = directory
        self.logger = logger
        self.max_files = max_files
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._counter = 0
        self._usable = self._ensure_directory()

    # ------------------------------------------------------------------ setup

    def _ensure_directory(self):
        try:
            os.makedirs(self.directory, exist_ok=True)
            probe = os.path.join(self.directory, ".writable")
            with open(probe, "w") as handle:
                handle.write("ok")
            os.remove(probe)
            return True
        except Exception as e:
            # Not fatal. Publishing still works; it just stops being recoverable after an
            # outage, and saying so once is better than failing every scan.
            self.logger.error(
                "Event spool at %s is not writable (%s). Events will NOT survive an "
                "outage or a restart.", self.directory, e)
            return False

    @property
    def usable(self):
        return self._usable

    # ------------------------------------------------------------------ writing

    def write(self, topic, payload):
        """Persist one event and return its path, or None if it could not be stored."""
        if not self._usable:
            return None
        with self._lock:
            try:
                self._counter = (self._counter + 1) % 1000
                # Monotonic-ish name so a directory listing sorts into send order.
                name = "%016.4f-%03d.json" % (time.time(), self._counter)
                path = os.path.join(self.directory, name)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump({"topic": topic, "payload": payload, "created": time.time()}, handle)
                # Rename is atomic, so a reader never sees a half-written event even if
                # the device loses power mid-write.
                os.replace(tmp, path)
                self._prune_locked()
                return path
            except Exception as e:
                self.logger.error("Could not spool event for %s: %s", topic, e)
                return None

    def discard(self, path):
        """Remove an event the broker has acknowledged."""
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.warning("Could not remove spooled event %s: %s", path, e)

    # ------------------------------------------------------------------ reading

    def pending(self, limit=None):
        """Spooled events, oldest first, as (path, topic, payload)."""
        if not self._usable:
            return []
        out = []
        for path in self._paths():
            if limit is not None and len(out) >= limit:
                break
            try:
                with open(path, encoding="utf-8") as handle:
                    record = json.load(handle)
                out.append((path, record["topic"], record["payload"]))
            except Exception as e:
                # A corrupt entry must not wedge the queue behind it.
                self.logger.warning("Dropping unreadable spooled event %s: %s", path, e)
                self.discard(path)
        return out

    def count(self):
        return len(self._paths())

    def _paths(self):
        try:
            names = [n for n in os.listdir(self.directory) if n.endswith(".json")]
        except Exception:
            return []
        return [os.path.join(self.directory, n) for n in sorted(names)]

    # ------------------------------------------------------------------ bounds

    def prune(self):
        with self._lock:
            self._prune_locked()

    def _prune_locked(self):
        paths = self._paths()

        if self.max_age_seconds:
            cutoff = time.time() - self.max_age_seconds
            stale = []
            for path in paths:
                try:
                    if os.path.getmtime(path) < cutoff:
                        stale.append(path)
                except Exception:
                    continue
            if stale:
                self.logger.error(
                    "Discarding %d spooled event(s) older than %d days - these scans are "
                    "lost and will never reach the server.",
                    len(stale), self.max_age_seconds // 86400)
                for path in stale:
                    self.discard(path)
                paths = [p for p in paths if p not in stale]

        if self.max_files and len(paths) > self.max_files:
            excess = paths[:len(paths) - self.max_files]
            self.logger.error(
                "Event spool is full (%d > %d). Discarding the %d oldest event(s) - these "
                "scans are lost and will never reach the server.",
                len(paths), self.max_files, len(excess))
            for path in excess:
                self.discard(path)
