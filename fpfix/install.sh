#!/bin/sh
# Fingerprint terminal fix: stop losing scans during a network outage.
#   - reconnect never gives up (was: 12 tries ~7 min, then offline forever)
#   - every event is written to disk before publishing, removed only on broker ACK
#   - publish result is checked; the log stops claiming success for lost events
#   - GPIO edge polarity read after the sensor is up, and used consistently
# Safe to re-run. Refuses if the device's files are not the version this patches.
set -e
BASE="https://raw.githubusercontent.com/ItcMk/pastebin/main/fpfix"
APP="/opt/app"
TMP="/tmp/fpfix.$$"
mkdir -p "$TMP/modules"
trap 'rm -rf "$TMP"' EXIT

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    else wget -q -O "$2" "$1"; fi
}

echo "Downloading..."
for f in main.py modules/mqtt.py modules/utils.py modules/spool.py SHA256SUMS EXPECTED_BEFORE; do
    fetch "$BASE/$f" "$TMP/$f" || { echo "FAILED to download $f"; exit 1; }
done

python3 - "$TMP" "$APP" <<'PYEOF'
import hashlib, os, py_compile, shutil, sys, time
tmp, app = sys.argv[1], sys.argv[2]
def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.exists(p) else "MISSING"

def read_sums(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if line:
            h, name = line.split(None, 1)
            out[name] = h
    return out

want_new = read_sums(os.path.join(tmp, "SHA256SUMS"))
want_old = read_sums(os.path.join(tmp, "EXPECTED_BEFORE"))

# 1. downloads intact?
for name, h in want_new.items():
    got = sha(os.path.join(tmp, name))
    if got != h:
        print("ABORT: download of %s is corrupt" % name); sys.exit(1)

# 2. already installed?
if all(sha(os.path.join(app, n)) == h for n, h in want_new.items()):
    print("Already up to date - nothing to do."); sys.exit(0)

# 3. is this device the version we patch?
drift = [(n, sha(os.path.join(app, n))[:12], h[:12])
         for n, h in want_old.items()
         if sha(os.path.join(app, n)) not in (h, want_new.get(n))]
if drift:
    print("REFUSED - these files are not the version this patch expects:")
    for n, g, w in drift:
        print("   %-18s device %s...  expected %s..." % (n, g, w))
    print("Nothing changed. Handle this device separately.")
    sys.exit(1)

# 4. back up, install, compile
stamp = time.strftime("%Y%m%d-%H%M%S")
backup = os.path.join(app, "backup-" + stamp)
for name in want_new:
    src, dst = os.path.join(tmp, name), os.path.join(app, name)
    if os.path.exists(dst):
        b = os.path.join(backup, name)
        os.makedirs(os.path.dirname(b), exist_ok=True)
        shutil.copy2(dst, b)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    py_compile.compile(dst, doraise=True)
    print("  updated %s" % name)

# 5. config.py is device-specific (GPIO_READ_PIN differs per unit), so edit only the
#    MQTT block rather than replacing the file.
cfg = os.path.join(app, "config.py")
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
        sys.exit(1)
    shutil.copy2(cfg, cfg + ".bak-" + stamp)
    open(cfg, "w", encoding="utf-8").write(src.replace(old, new, 1))
    py_compile.compile(cfg, doraise=True)
    print("  config.py updated")

for root, dirs, _ in os.walk(app):
    for d in list(dirs):
        if d == "__pycache__":
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
print("\nINSTALLED. Backup: %s" % backup)
PYEOF

echo
echo "Now restart:  /etc/init.d/S99python restart"
