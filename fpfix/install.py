#!/usr/bin/env python3
"""
Fingerprint terminal fix - stop losing scans during a network outage.

Pure Python on purpose: busybox wget on these devices has no TLS ("not an http or ftp
url"), while Python already speaks TLS to AWS IoT.

Safe to re-run. Backs up what it replaces, verifies every download against SHA256SUMS,
and refuses if the device's files are not the version this patches.
"""
import hashlib
import os
import py_compile
import shutil
import ssl
import sys
import tempfile
import time
import urllib.request

# raw.githubusercontent caches aggressively, so a freshly pushed file can be served stale
# for several minutes. Set FPFIX_BASE to a commit-pinned URL to get an immutable, uncached
# copy - the SHA256 check below turns a stale fetch into a clean abort rather than a bad
# install, but pinning avoids the puzzle entirely.
BASE = os.environ.get(
    "FPFIX_BASE", "https://raw.githubusercontent.com/ItcMk/pastebin/main/fpfix").rstrip("/")
APP = sys.argv[1] if len(sys.argv) > 1 else "/opt/app"
FILES = ["main.py", "modules/mqtt.py", "modules/utils.py", "modules/spool.py",
         "modules/gps.py", "modules/L76X.py"]


def fetch(url):
    try:
        ctx = ssl.create_default_context()
        return urllib.request.urlopen(url, context=ctx, timeout=30).read()
    except Exception as e:
        print("  download failed: %s" % e)
        raise


def sha(path):
    if not os.path.exists(path):
        return "MISSING"
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def read_sums(text):
    out = {}
    for line in text.decode().splitlines():
        line = line.strip()
        if line:
            h, name = line.split(None, 1)
            out[name] = h
    return out


def main():
    print("Downloading from %s ..." % BASE)
    try:
        want_new = read_sums(fetch(BASE + "/SHA256SUMS"))
        want_old = read_sums(fetch(BASE + "/ACCEPTED_BEFORE"))
    except Exception:
        print("ABORT: could not reach GitHub. Check DNS/connectivity.")
        return 1

    tmp = tempfile.mkdtemp(prefix="fpfix-")
    try:
        for name in FILES:
            data = fetch("%s/%s" % (BASE, name))
            got = hashlib.sha256(data).hexdigest()
            if got != want_new.get(name):
                print("ABORT: %s downloaded corrupt (%s... != %s...)"
                      % (name, got[:12], str(want_new.get(name))[:12]))
                return 1
            dst = os.path.join(tmp, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(data)
        print("  %d file(s) downloaded and verified" % len(FILES))

        if all(sha(os.path.join(APP, n)) == h for n, h in want_new.items()):
            print("Already up to date - nothing to do.")
            return 0

        drift = []
        for n, h in want_old.items():
            allowed = set(h.split(",")) | {want_new.get(n)}
            got = sha(os.path.join(APP, n))
            if got not in allowed:
                drift.append((n, got[:12], h.split(",")[0][:12]))
        if drift:
            print("REFUSED - these files are not the version this patch expects:")
            for n, g, w in drift:
                print("   %-18s device %s...  expected %s..." % (n, g, w))
            print("Nothing changed. Handle this device separately.")
            return 1

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(APP, "backup-" + stamp)
        for name in FILES:
            dst = os.path.join(APP, name)
            if os.path.exists(dst):
                b = os.path.join(backup, name)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(dst, b)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(tmp, name), dst)
            py_compile.compile(dst, doraise=True)
            print("  updated %s" % name)

        # config.py is device-specific (GPIO_READ_PIN differs per unit), so only the MQTT
        # block is edited rather than replacing the file.
        cfg = os.path.join(APP, "config.py")
        src = open(cfg, encoding="utf-8").read()
        if "MQTT_SPOOL_DIR" in src:
            print("  config.py already updated")
        else:
            old = ("MQTT_MAX_RECONNECT_COUNT = 12\n"
                   "MQTT_MAX_RECONNECT_DELAY = 60\n"
                   "MQTT_SESSION_EXPIRY = 120  # seconds")
            new = ("MQTT_MAX_RECONNECT_COUNT = 0\n"
                   "MQTT_MAX_RECONNECT_DELAY = 60\n"
                   "MQTT_SESSION_EXPIRY = 3600  # seconds\n\n"
                   'MQTT_SPOOL_DIR = os.path.join(BASE_DIR, "spool")\n'
                   "MQTT_SPOOL_MAX_FILES = 500\n"
                   "MQTT_SPOOL_MAX_AGE_SECONDS = 7 * 24 * 3600\n"
                   "MQTT_SPOOL_RETRY_SECONDS = 60")
            if old not in src:
                print("WARNING: config.py MQTT block not as expected - left unchanged.")
                print("         The new code needs MQTT_SPOOL_* settings; add them by hand.")
                return 1
            shutil.copy2(cfg, cfg + ".bak-" + stamp)
            open(cfg, "w", encoding="utf-8").write(src.replace(old, new, 1))
            py_compile.compile(cfg, doraise=True)
            print("  config.py updated")

        # Logging block: move the real log off the tmpfs that a reboot empties, and
        # quieten the GPS chatter so seven days of history is mostly signal.
        src = open(cfg, encoding="utf-8").read()
        if "LOG_TO_STDOUT" in src:
            print("  config.py logging block already updated")
        else:
            old_log = 'LOGS_DIR = "/var/log"'
            new_log = ('LOGS_DIR = os.path.join(BASE_DIR, "logs")')
            if old_log not in src:
                print("WARNING: config.py LOGS_DIR not as expected - left unchanged.")
            else:
                src = src.replace(old_log, new_log, 1)
                src = src.replace(
                    "LOG_BACKUP_COUNT = 7  # Keep 7 days of logs",
                    "LOG_BACKUP_COUNT = 7  # Keep 7 days of logs\n"
                    "LOG_TO_STDOUT = False\n"
                    "GPS_VERBOSE = False\n"
                    "GPS_NO_FIX_WARN_INTERVAL = 3600", 1)
                open(cfg, "w", encoding="utf-8").write(src)
                py_compile.compile(cfg, doraise=True)
                print("  config.py logging block updated")

        for root, dirs, _ in os.walk(APP):
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)

        print("\nINSTALLED. Backup: %s" % backup)
        print("Now restart:  /etc/init.d/S99python restart")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
