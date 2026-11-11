"""
FingerBoard Application Configuration
AWS IoT + Fingerprint + GPS System
"""

import os

# Base paths
BASE_DIR = "/opt/app"
CERT_DIR = os.path.join(BASE_DIR, "certificates")
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
# /var/log is a tmpfs on these devices - it is emptied by every reboot, which is exactly
# when the previous days' logs matter most. The real log lives beside the application on
# persistent storage so that LOG_BACKUP_COUNT below means what it says.
# /var/log/python_app.log still exists and now holds only whatever the app prints to
# stdout, which the init script redirects there.
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# Logging Configuration
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FILE = os.path.join(LOGS_DIR, "python_app.log")
LOG_ROTATION = "D"  # Daily rotation
LOG_BACKUP_COUNT = 7  # Keep 7 days of logs
# Adding a stdout handler duplicates every line into the init script's redirect and
# breaks rotation; see setup_logger() in main.py. Leave this off on a device.
LOG_TO_STDOUT = False

# The GPS chatter ([GPS] lines written straight to stdout) is useful when bringing a
# board up and is pure noise afterwards - these terminals are indoors and never get a
# fix, so it was thousands of identical lines a day burying everything worth reading.
GPS_VERBOSE = False
# How often to repeat the "no satellite fix" warning, in seconds. It was every poll.
GPS_NO_FIX_WARN_INTERVAL = 3600

# AWS IoT MQTT Configuration
MQTT_BROKER = "a4cqnd4uv0o14-ats.iot.eu-west-1.amazonaws.com"
MQTT_PORT = 8883
MQTT_PROTOCOL = 5  # MQTTv5

# AWS IoT Certificates (device-specific)
MQTT_CA_CERT = os.path.join(CERT_DIR, "AmazonRootCA1.pem")
MQTT_CLIENT_CERT = os.path.join(CERT_DIR, "client-cert.pem")
MQTT_PRIVATE_KEY = os.path.join(CERT_DIR, "private-key.pem")

# MQTT Connection Settings
MQTT_FIRST_RECONNECT_DELAY = 1
MQTT_RECONNECT_RATE = 2
# 0 means never stop trying. It was 12, which with this backoff gave up after roughly
# seven minutes and then never reconnected at all, so a DNS blip became a day-long outage
# at Wielbark on 2026-11-05 and 34 scans were lost. A terminal with nobody at it has
# nothing better to do than keep retrying.
MQTT_MAX_RECONNECT_COUNT = 0
MQTT_MAX_RECONNECT_DELAY = 60
# How long the broker keeps our session, and with it any unacknowledged QoS 1 message,
# while we are away. Was 120 seconds, so anything beyond a two minute outage was discarded
# by the broker. AWS IoT caps this, so it is a cushion for short drops rather than a
# substitute for the on-disk spool below.
MQTT_SESSION_EXPIRY = 3600  # seconds

# On-disk store-and-forward queue (modules/spool.py). An event is written here before it
# is published and removed only once the broker acknowledges it, so an outage of any
# length - or an application restart - costs nothing.
MQTT_SPOOL_DIR = os.path.join(BASE_DIR, "spool")
MQTT_SPOOL_MAX_FILES = 500        # roughly 75 KB per fingerprint event
MQTT_SPOOL_MAX_AGE_SECONDS = 7 * 24 * 3600
MQTT_SPOOL_RETRY_SECONDS = 60     # how often to re-send what is still unacknowledged

# MQTT Topics (will be prefixed with device ID)
# Format: device/{DEVICE_ID}/{topic}
MQTT_TOPICS = {
    "register": "register",
    "reboot": "reboot",
    "reminder": "reminder",
    "response": "response",
    "renew_certificate": "renew_certificate",
    "fingerprint_event": "FINGERPRINT_EVENT",
    "register_fingerprint": "REGISTER_FINGERPRINT",
    "restart": "RESTART",
    "gpio_event": "GPIO_EVENT",
    "gps_event": "GPS_EVENT",
    "relay_control": "RELAY_CONTROL",  # Control relays with duration
    "relay_status": "RELAY_STATUS",    # Publish relay state changes
    "tunnel_notify": "$aws/things/{DEVICE_ID}/tunnels/notify",  # AWS IoT Secure Tunneling notifications
    "ota_update": "OTA_UPDATE",        # Receive OTA update commands (scripts via MQTT, assets via URL)
    "ota_status": "OTA_STATUS",        # Publish OTA update status
}

# GPIO Pin Configuration
# Fingerprint reader - WAKEUP signal from R503 via ISO7720DR
GPIO_READ_PIN = 33  # R503 WAKEUP → ISO7720DR → GPIO 33
#GPIO_READ_PIN = 54  # R503 WAKEUP → ISO7720DR → GPIO 33
GPIO_DEBOUNCE_MS = 500  # Debounce time in milliseconds

# GPIO monitoring (pin -> (event_name, sound_file, inverted, debounce_seconds, verification_delay_ms))
# inverted=True means trigger on rising edge (LOW->HIGH) instead of falling edge (HIGH->LOW)
# debounce_seconds: minimum time between triggers
# verification_delay_ms: delay to verify pin state0. is stable (helps filter ESD noise)
GPIO_MONITORS = {
    73: ("LOST_BATTERY", os.path.join(SOUNDS_DIR, "battery_bad.mp3"), True, 30, 5000),  # 30s debounce, 500ms verification
    72: ("LOST_MAINS", os.path.join(SOUNDS_DIR, "power_bad.mp3"), True, 30, 5000),      # 30s debounce, 500ms verification
    67: ("INTRUSION", os.path.join(SOUNDS_DIR, "intrusion.mp3"), True, 30, 1000),       # 30s debounce, 500ms verification
    55: ("DOOR_OPEN", os.path.join(SOUNDS_DIR, "door_open.mp3"), False, 10, 400),      # 10s debounce, 100ms verification
}

# Relay Control Pins
RELAY_PINS = [58, 59]  # GPIO output pins for relay control
RELAY_58_AUTO_TRIGGER_DURATION = 60  # seconds (1 minute for door open from pin 55)

# Relay Pin Name Abstraction (for MQTT communication)
RELAY_PIN_NAMES = {
    "main": 58,
    "aux": 59,
}

# Reverse mapping (pin number -> name)
RELAY_PIN_TO_NAME = {
    58: "main",
    59: "aux",
}

# Sound Files
SOUNDS = {
    "reminder": os.path.join(SOUNDS_DIR, "reminder.mp3"),
    "reminder_speech": os.path.join(SOUNDS_DIR, "reminder_speech.mp3"),
    "place_finger": os.path.join(SOUNDS_DIR, "place_finger.mp3"),
    "registered_finger": os.path.join(SOUNDS_DIR, "registered_finger.mp3"),
    "startup": os.path.join(SOUNDS_DIR, "reminder.mp3"),  # Play on startup
    "confirm": os.path.join(SOUNDS_DIR, "confirm.mp3"),
    "fingerprint_ok": os.path.join(SOUNDS_DIR, "fingerprint_ok.mp3"),
    "fingerprint_bad": os.path.join(SOUNDS_DIR, "fingerprint_bad.mp3"),
    "error": os.path.join(SOUNDS_DIR, "error.mp3"),
    "remove_finger": os.path.join(SOUNDS_DIR, "remove_finger.mp3"),
    "no_internet": os.path.join(SOUNDS_DIR, "no_internet.mp3"),
    "door_open": os.path.join(SOUNDS_DIR, "door_open.mp3"),
}

# Device ID file
DEVICE_ID_FILE = "/opt/app/id.txt"

# Fingerprint Reader Configuration
FINGERPRINT_SERIAL_PORT = "/dev/ttyS3"  # R503 sensor UART port
# How long to keep asking the sensor for an image while it answers 0x02 (no finger).
# The GPIO edge fires the instant contact is made, so the finger may need a moment to
# settle. After this, the capture is abandoned rather than uploading the stale image
# left in the sensor's buffer from the previous person.
CAPTURE_SETTLE_TIMEOUT = 2.0  # seconds

# How many good samples an enrolment must collect. Must match the server's
# app.fingerprint.enrollment.samples-per-finger, because the panel treats reaching that
# count as the enrolment being finished - send fewer and it waits for ever.
ENROLLMENT_SAMPLES = 4        # default when the server does not say
ENROLLMENT_SAMPLES_MIN = 2    # bounds for whatever the server does say
ENROLLMENT_SAMPLES_MAX = 8
# A ceiling on retries, so a sensor that reads nothing gives up instead of holding the
# terminal. Each attempt waits up to 30s for a finger, so this bounds an enrolment at a
# few minutes rather than indefinitely.
ENROLLMENT_MAX_ATTEMPTS = 12       # floor; the real cap is max(this, samples * below)
ENROLLMENT_ATTEMPTS_PER_SAMPLE = 3
FINGERPRINT_CAPTURE_ATTEMPTS = 4  # Number of attempts for registration
FINGERPRINT_TIMEOUT = 30  # seconds

# GPS Configuration
GPS_SERIAL_PORT = "/dev/ttyS4"  # Adjust for your hardware
GPS_BAUDRATE = 9600
GPS_UPDATE_INTERVAL = 300 # seconds between GPS updates

# Application Behavior
WAIT_FOR_INTERNET = True  # Wait for internet on startup
BUTTON_PRESS_TIMEOUT = 60  # seconds
POLL_TIMEOUT = 100  # milliseconds for GPIO polling (10ms->100ms = 10x less CPU usage, still very responsive)

# Device Information (auto-detected from /proc/cpuinfo or MAC address)
# Device ID will be loaded at runtime using utils.get_device_id()
DEVICE_ID = None  # Will be set at runtime

# AWS IoT Secure Tunneling Configuration
TUNNEL_ENABLED = True  # Enable AWS IoT Secure Tunneling support
TUNNEL_LOCALPROXY_PATH = "/usr/bin/localproxy"  # Path to local proxy binary
TUNNEL_DESTINATION_PORT = 22  # SSH port (default)
TUNNEL_REGION = "eu-west-1"  # AWS region (should match MQTT broker region)

# Watchdog Configuration
WATCHDOG_ENABLED = False  # Enable hardware/software watchdog for automatic recovery
WATCHDOG_DEVICE = "/dev/watchdog"  # Hardware watchdog device
WATCHDOG_TIMEOUT = 300  # Watchdog timeout in seconds (device will reboot if not kicked)
WATCHDOG_KICK_INTERVAL = 120  # How often to kick the watchdog in seconds (must be < timeout)
WATCHDOG_MONITOR_THREADS = True  # Monitor critical threads (MQTT, GPS, GPIO) for hangs

# OTA Update Configuration
OTA_ENABLED = True  # Enable Over-The-Air updates
OTA_BACKUP_DIR = "/opt/app/.backup"  # Directory to store backups before updates
OTA_MAX_BACKUPS = 3  # Keep last 3 backups
OTA_RESTART_DELAY = 5  # Seconds to wait before restarting after update
OTA_VERIFY_CHECKSUMS = True  # Verify SHA256 checksums for updates
OTA_ALLOWED_PATHS = [  # Whitelist of updateable paths for security
    "/opt/app/main.py",
    "/opt/app/config.py",
    "/opt/app/modules/",
    "/opt/app/sounds/",
    "/opt/app/certificates/",
]
