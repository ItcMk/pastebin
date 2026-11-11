#!/usr/bin/env python3
"""
Repair /opt/app/config.py after an OTA onto a terminal whose config predates the
new code's settings.

config.py is never shipped by OTA, because it holds per-device values (GPIO_READ_PIN
is 33 on some units and 54 on others). So a terminal that has been flashed for a while
can run new modules against an old config, and the new code reads several settings
WITHOUT a getattr default - config.MQTT_SPOOL_DIR among them - which is a hard crash
at startup rather than a degraded mode.

This only ever APPENDS names that are absent. It never edits a value that is already
there, which is exactly what keeps the per-device settings safe: the device-specific
ones are precisely the ones that already exist.
"""
import ast, importlib.util, os, shutil, sys, time

CONFIG = "/opt/app/config.py"
REFERENCE = "/tmp/config_reference.py"

REQUIRED = [
    "BASE_DIR","DEVICE_ID","FINGERPRINT_SERIAL_PORT","GPIO_DEBOUNCE_MS","GPIO_MONITORS",
    "GPIO_READ_PIN","LOG_BACKUP_COUNT","LOG_FILE","LOG_LEVEL","LOG_ROTATION","MQTT_BROKER",
    "MQTT_CA_CERT","MQTT_CLIENT_CERT","MQTT_FIRST_RECONNECT_DELAY","MQTT_MAX_RECONNECT_COUNT",
    "MQTT_MAX_RECONNECT_DELAY","MQTT_PORT","MQTT_PRIVATE_KEY","MQTT_RECONNECT_RATE",
    "MQTT_SESSION_EXPIRY","MQTT_SPOOL_DIR","MQTT_SPOOL_MAX_AGE_SECONDS","MQTT_SPOOL_MAX_FILES",
    "MQTT_SPOOL_RETRY_SECONDS","OTA_ALLOWED_PATHS","OTA_BACKUP_DIR","OTA_ENABLED",
    "OTA_MAX_BACKUPS","OTA_RESTART_DELAY","OTA_VERIFY_CHECKSUMS","POLL_TIMEOUT",
    "RELAY_58_AUTO_TRIGGER_DURATION","RELAY_PINS","RELAY_PIN_NAMES","RELAY_PIN_TO_NAME",
    "SOUNDS","TUNNEL_DESTINATION_PORT","TUNNEL_ENABLED","TUNNEL_LOCALPROXY_PATH",
    "TUNNEL_REGION","WAIT_FOR_INTERNET",
]


def top_level_names(path):
    tree = ast.parse(open(path).read())
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    found.add(t.id)
    return found


def segments(path):
    """name -> exact source text of its assignment, so dicts and lists survive intact."""
    src = open(path).read()
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = ast.get_source_segment(src, node)
    return out


def main():
    have = top_level_names(CONFIG)
    missing = [n for n in REQUIRED if n not in have]
    if not missing:
        print("config.py already has all %d required settings - nothing to do." % len(REQUIRED))
        return 0

    print("Missing from this device's config.py:")
    for n in missing:
        print("   ", n)

    ref = segments(REFERENCE)
    unknown = [n for n in missing if n not in ref]
    if unknown:
        print("\nREFUSING: no reference value for: %s" % ", ".join(unknown))
        return 1

    backup = CONFIG + ".bak-" + time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(CONFIG, backup)
    print("\nBacked up to", backup)

    with open(CONFIG, "a") as fh:
        fh.write("\n\n# --- appended %s: settings the newer modules require ---\n"
                 % time.strftime("%Y-%m-%d %H:%M:%S"))
        for n in missing:
            fh.write(ref[n] + "\n")
            print("  added:", n)

    # Prove it actually imports and now satisfies every requirement, rather than
    # trusting that appending text produced valid Python.
    spec = importlib.util.spec_from_file_location("cfgcheck", CONFIG)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        shutil.copy2(backup, CONFIG)
        print("\nFAILED to import the patched config (%s) - restored the backup." % e)
        return 1

    still = [n for n in REQUIRED if not hasattr(mod, n)]
    if still:
        shutil.copy2(backup, CONFIG)
        print("\nStill missing after patch: %s - restored the backup." % ", ".join(still))
        return 1

    print("\nOK: config.py imports and has all %d required settings." % len(REQUIRED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
