# fpfix — fingerprint terminal fix

Stops attendance scans being lost during a network outage.

On 2026-11-05 SP Wielbark published 34 fingerprint events over ~24 hours and none were
stored, while every other site recorded normally. Three faults combined:

* DNS failed, so the broker connection dropped;
* the reconnect loop gave up after 12 attempts (~7 minutes) and never retried, turning a
  blip into a day-long outage;
* QoS 1 only survives as long as the broker keeps the session, and `SessionExpiryInterval`
  was 120 seconds, so everything queued was discarded.

None of it was visible, because the publish result was never checked and the log said
"published successfully" either way.

## What changes

| | |
|---|---|
| `MQTT_MAX_RECONNECT_COUNT` | `12` → `0` (never give up) |
| `MQTT_SESSION_EXPIRY` | `120` → `3600` seconds |
| `modules/spool.py` | new — events held on disk until the broker acknowledges them |
| publish path | result code checked; log no longer claims success for lost events |
| GPIO edge polarity | read after the sensor is up, and used consistently by both code paths |

## Install

    python3 -c "import urllib.request as u;u.urlretrieve('https://raw.githubusercontent.com/ItcMk/pastebin/main/fpfix/install.py','/tmp/i.py')" && python3 /tmp/i.py

Then:

    /etc/init.d/S99python restart

Safe to re-run. Backs up what it replaces, and refuses if the device's files are not the
version it expects.
