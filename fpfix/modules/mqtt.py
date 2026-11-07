import json
import ssl
from datetime import datetime
import paho.mqtt.client as mqtt
from paho.mqtt.client import Client
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes
import os
import time
import threading

from .fingerprint import FingerPrint
from .spool import EventSpool
import config

# Lazy import OTA (only loaded when needed for updates)
_ota_updater_instance = None


class Mqtt:
    """AWS IoT MQTT Client with reconnection and message handling"""

    BROKER_URL = config.MQTT_BROKER
    BROKER_PORT = config.MQTT_PORT

    FIRST_RECONNECT_DELAY = config.MQTT_FIRST_RECONNECT_DELAY
    RECONNECT_RATE = config.MQTT_RECONNECT_RATE
    MAX_RECONNECT_COUNT = config.MQTT_MAX_RECONNECT_COUNT
    MAX_RECONNECT_DELAY = config.MQTT_MAX_RECONNECT_DELAY

    CERTS = {
        "cafile": config.MQTT_CA_CERT,
        "certfile": config.MQTT_CLIENT_CERT,
        "keyfile": config.MQTT_PRIVATE_KEY,
    }

    def __init__(self, polling_control, fingerprint: FingerPrint, pin, utils, logger, relay_controller=None):
        self.polling_control = polling_control
        self.fingerprint = fingerprint
        self.pin = pin
        self.logger = logger
        self.utils = utils
        self.relay_controller = relay_controller
        self.device_id = utils.get_device_id()

        # Store-and-forward. Built before the client, because on_connect drains it and
        # on_publish clears entries from it, and both can fire the moment we connect.
        self.spool = EventSpool(
            config.MQTT_SPOOL_DIR, logger,
            max_files=config.MQTT_SPOOL_MAX_FILES,
            max_age_seconds=config.MQTT_SPOOL_MAX_AGE_SECONDS)
        self._pending_mids = {}          # paho message id -> spooled file awaiting PUBACK
        self._pending_lock = threading.Lock()

        self.mqtt_client = self.setup_mqtt_client()

        # Re-send anything still unacknowledged. Covers the events that were in flight
        # when the application last stopped, and anything a drop left behind.
        self._retry_thread = threading.Thread(
            target=self._retry_spool_forever, name="SpoolRetry", daemon=True)
        self._retry_thread.start()

        # Lazy initialize OTA updater (only when first OTA message received)
        self._ota_updater = None

    @property
    def ota_updater(self):
        """Lazy-load OTA updater only when needed (saves ~100ms startup time)"""
        if self._ota_updater is None:
            from .ota import OTAUpdater
            self._ota_updater = OTAUpdater(self.logger, self.mqtt_client)
            self.logger.info("OTA updater initialized (lazy loaded)")
        return self._ota_updater

    def setup_mqtt_client(self) -> Client:
        """Setup and return an MQTT client."""
        mqtt_client = Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=self.device_id,
            protocol=mqtt.MQTTv5
        )
        mqtt_client.enable_logger(self.logger)

        mqtt_client.on_disconnect = self.on_disconnect
        mqtt_client.on_connect = self.on_connect
        mqtt_client.on_message = self.on_message
        mqtt_client.on_publish = self.on_publish

        mqtt_client.tls_set(
            self.CERTS["cafile"],
            certfile=self.CERTS["certfile"],
            keyfile=self.CERTS["keyfile"],
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
            ciphers=None,
        )

        properties_connect = Properties(PacketTypes.CONNECT)
        properties_connect.SessionExpiryInterval = config.MQTT_SESSION_EXPIRY

        try:
            mqtt_client.connect(self.BROKER_URL, self.BROKER_PORT, clean_start=False, properties=properties_connect)
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT broker: {e}")
            raise

        mqtt_client.loop_start()  # Start MQTT client loop in the background
        return mqtt_client

    def on_disconnect(self, client, userdata, rc, properties=None):
        """
        Reconnect, and keep reconnecting.

        This used to stop after MAX_RECONNECT_COUNT attempts and log "Exiting...", after
        which nothing ever reconnected and the terminal stayed offline until somebody
        restarted it. Every scan in the meantime was still announced as "published
        successfully". That is how Wielbark lost a full day on 2026-11-05: DNS failed for
        longer than the roughly seven minutes the old backoff allowed, and the device
        never came back on its own.

        MAX_RECONNECT_COUNT = 0 now means never give up. Scans captured while we are away
        are held by the spool, so a long outage costs a delay rather than the data.
        """
        self.logger.warning("Disconnected with result code: %s", rc)
        reconnect_count, reconnect_delay = 0, self.FIRST_RECONNECT_DELAY
        while self.MAX_RECONNECT_COUNT <= 0 or reconnect_count < self.MAX_RECONNECT_COUNT:
            time.sleep(reconnect_delay)

            try:
                client.reconnect()
                self.logger.info("Reconnected successfully after %d attempt(s)!",
                                 reconnect_count + 1)
                return
            except Exception as err:
                reconnect_count += 1
                # One line per attempt at this rate would be thousands overnight, and the
                # log is already hard enough to read. Say it clearly, then occasionally.
                if reconnect_count <= 5 or reconnect_count % 30 == 0:
                    pending = self.spool.count()
                    self.logger.error(
                        "%s. Reconnect attempt %d failed, retrying in %ds. %d event(s) "
                        "waiting in the spool.", err, reconnect_count,
                        min(reconnect_delay * self.RECONNECT_RATE, self.MAX_RECONNECT_DELAY),
                        pending)

            reconnect_delay *= self.RECONNECT_RATE
            reconnect_delay = min(reconnect_delay, self.MAX_RECONNECT_DELAY)
        self.logger.error(
            "Reconnect gave up after %s attempts. %d event(s) remain spooled and will be "
            "sent if the application restarts.", reconnect_count, self.spool.count())

    # ------------------------------------------------------------- store and forward

    def on_publish(self, client, userdata, mid):
        """The broker acknowledged a message, so its spooled copy can go."""
        with self._pending_lock:
            path = self._pending_mids.pop(mid, None)
        self.spool.discard(path)

    def publish_tracked(self, topic, payload, properties=None):
        """
        Publish an event that must not be lost.

        The copy goes to disk FIRST, and is removed only when the broker acknowledges it
        in on_publish. Checking the publish result is the other half: paho returns a
        result code rather than raising when there is no connection, so the old code's
        unconditional "published successfully" was true of a message that had gone
        nowhere.
        """
        path = self.spool.write(topic, payload)
        try:
            info = self.mqtt_client.publish(topic, payload, qos=1, properties=properties)
        except Exception as e:
            self.logger.error("Publish to %s raised %s; event is spooled (%s).",
                              topic, e, "yes" if path else "NOT SAVED")
            return False

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.logger.error(
                "Publish to %s was refused (rc=%s, %s). Event is spooled (%s) and will be "
                "retried.", topic, info.rc, mqtt.error_string(info.rc),
                "yes" if path else "NOT SAVED")
            return False

        if path:
            with self._pending_lock:
                self._pending_mids[info.mid] = path
        return True

    def _drain_spool(self, limit=None):
        """Re-send everything still unacknowledged, oldest first."""
        entries = self.spool.pending(limit=limit)
        if not entries:
            return
        self.logger.info("Re-sending %d spooled event(s)...", len(entries))
        sent = 0
        for path, topic, payload in entries:
            with self._pending_lock:
                if path in self._pending_mids.values():
                    continue  # already in flight, waiting on its PUBACK
            try:
                info = self.mqtt_client.publish(topic, payload, qos=1)
            except Exception as e:
                self.logger.error("Re-send of %s failed: %s", topic, e)
                break
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                # Still no usable connection; stop rather than spin the whole queue.
                break
            with self._pending_lock:
                self._pending_mids[info.mid] = path
            sent += 1
        if sent:
            self.logger.info("Re-sent %d spooled event(s), awaiting acknowledgement.", sent)

    def _retry_spool_forever(self):
        while True:
            try:
                time.sleep(config.MQTT_SPOOL_RETRY_SECONDS)
                if self.spool.count():
                    self._drain_spool()
                    self.spool.prune()
            except Exception as e:
                self.logger.error("Spool retry loop error: %s", e)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Callback for MQTT client connection."""
        self.logger.info("Connected to MQTT broker.")
        client.subscribe(f"device/{self.device_id}/register", qos=1)
        client.subscribe(f"device/{self.device_id}/reboot", qos=1)
        client.subscribe(f"device/{self.device_id}/reminder", qos=1)
        client.subscribe(f"device/{self.device_id}/response", qos=1)
        client.subscribe(f"device/{self.device_id}/renew_certificate", qos=1)
        client.subscribe(f"device/{self.device_id}/RELAY_CONTROL", qos=1)

        # Subscribe to AWS IoT Secure Tunneling notifications
        if config.TUNNEL_ENABLED:
            tunnel_topic = f"$aws/things/{self.device_id}/tunnels/notify"
            client.subscribe(tunnel_topic, qos=1)
            self.logger.info(f"Subscribed to tunnel notifications: {tunnel_topic}")

        # Subscribe to OTA update notifications
        if config.OTA_ENABLED:
            ota_topic = f"device/{self.device_id}/OTA_UPDATE"
            client.subscribe(ota_topic, qos=1)
            self.logger.info(f"Subscribed to OTA updates: {ota_topic}")

        # Anything captured while we were away goes out now. Done on a separate thread:
        # this callback runs on paho's network loop, and that loop is what delivers the
        # acknowledgements we would be waiting for.
        if self.spool.count():
            threading.Thread(target=self._drain_spool, name="SpoolDrain",
                             daemon=True).start()

    def on_message(self, client, userdata, message):
        """
        Callback for MQTT messages.

        Performance optimization: Handle messages synchronously in MQTT thread.
        Only long-running operations (OTA updates) use separate threads.
        This prevents thread explosion and reduces context switching.
        """
        raw_payload = message.payload.decode("utf-8")
        self.logger.debug(f"Received message on topic: '{message.topic}', payload: '{raw_payload}'")

        # Handle all messages synchronously except OTA updates
        if message.topic.endswith("register"):
            self.handle_register_message(message)
        elif message.topic.endswith("reboot"):
            self.handle_reboot_message(message)
        elif message.topic.endswith("reminder"):
            self.handle_reminder_message(message)
        elif message.topic.endswith("response"):
            self.handle_response_message(message)
        elif message.topic.endswith("renew_certificate"):
            self.handle_renew_certificate(message)
        elif message.topic.endswith("RELAY_CONTROL"):
            self.handle_relay_control_message(message)
        elif message.topic.endswith("tunnels/notify"):
            self.handle_tunnel_notification(message)
        elif message.topic.endswith("OTA_UPDATE"):
            # OTA updates can be long-running, use thread but reuse if possible
            threading.Thread(target=self.handle_ota_update, args=(message,), daemon=True).start()

    def handle_reminder_message(self, message):
        """Handle 'reminder' MQTT messages."""
        try:
            self.logger.info("Pausing GPIO polling to process 'reminder' message...")
            self.polling_control.clear()  # Stop GPIO polling
            self.fingerprint.turn_on_led()

            data = json.loads(message.payload.decode("utf-8"))

            if data.get('sound', False):
                self.utils.play_sound(config.SOUNDS["reminder_speech"])
                time.sleep(5)
                self.utils.play_sound(config.SOUNDS["reminder"])

            if self.utils.wait_for_button_press(self.pin):
                self.logger.info("Button pressed. Capturing fingerprint...")
                self.utils.stop_sound()
                frame_list = self.fingerprint.capture_fingerprint()
                if frame_list:
                    reminder_id = data.get('reminder_id')
                    self.publish_fingerprint_event(frame_list, reminder_id)
            else:
                self.logger.warning("Timeout waiting for button press.")

            self.logger.info("Resuming GPIO polling after processing 'reminder' message...")
            self.polling_control.set()  # Resume GPIO polling
        except Exception as e:
            self.logger.error(f"Error handling 'reminder' message: {e}")
            self.polling_control.set()

    def handle_reboot_message(self, message):
        """Handle 'reboot' MQTT messages."""
        topic = f"device/{self.device_id}/RESTART"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "timestamp": timestamp,
        }

        # Publish the message
        self.mqtt_client.publish(topic, payload=json.dumps(payload), qos=1)
        os.system("reboot -f")

    def handle_register_message(self, message):
        """Handle 'register' MQTT messages."""
        try:
            self.logger.info("Pausing GPIO polling to process 'register' message...")
            self.polling_control.clear()  # Stop GPIO polling

            # Extract user ID from the message properties
            user_id = self.extract_user_id(message)

            # Capture and publish fingerprint data (up to 2 attempts)
            for attempt in range(4):
                self.logger.info(f"Attempt {attempt + 1}: Waiting for button press to capture fingerprint...")
                self.fingerprint.turn_on_led()
                self.utils.play_sound(config.SOUNDS["place_finger"])
                if self.utils.wait_for_button_press(self.pin):
                    self.logger.info("Button pressed. Capturing fingerprint...")
                    self.utils.stop_sound()
                    frame_list = self.fingerprint.capture_fingerprint()
                    if frame_list:
                        self.publish_register_fingerprint_event(frame_list, user_id)
                else:
                    self.logger.warning("Timeout waiting for button press.")

            self.logger.info("Resuming GPIO polling after processing 'register' message...")
            self.utils.play_sound(config.SOUNDS["registered_finger"])
            self.polling_control.set()  # Resume GPIO polling
        except Exception as e:
            self.logger.error(f"Error handling 'register' message: {e}")
            self.polling_control.set()

    def publish_register_fingerprint_event(self, frame_list: bytearray, user_id):
        """Publish register fingerprint data to the MQTT topic."""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payload = json.dumps({"imgData": frame_list.hex(), "user_id": user_id, "dateTime": timestamp})
            properties = Properties(PacketTypes.PUBLISH)
            properties.TopicAlias = 1

            if self.publish_tracked(f"device/{self.device_id}/REGISTER_FINGERPRINT",
                                    payload, properties):
                self.logger.info("Register fingerprint event published.")
            else:
                self.logger.warning(
                    "Enrolment could not be sent now; it is queued and will be retried. "
                    "The operator's screen will not show it until it lands.")
            self.fingerprint.turn_off_led()

        except Exception as e:
            self.logger.error(f"Failed to publish fingerprint event: {e}")

    def publish_fingerprint_event(self, frame_list: bytearray, reminder_id: str = None):
        """Publish fingerprint data to the MQTT topic."""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if reminder_id:
                payload = json.dumps({"imgData": frame_list.hex(), "dateTime": timestamp, "reminder_id": reminder_id})
            else:
                payload = json.dumps({"imgData": frame_list.hex(), "dateTime": timestamp})
            properties = Properties(PacketTypes.PUBLISH)
            properties.TopicAlias = 1

            if self.publish_tracked(f"device/{self.device_id}/FINGERPRINT_EVENT",
                                    payload, properties):
                self.logger.info("Fingerprint event published.")
            else:
                self.logger.warning(
                    "Fingerprint event could not be sent now; it is queued and will be "
                    "retried until the broker accepts it.")

        except Exception as e:
            self.logger.error(f"Failed to publish fingerprint event: {e}")

    def handle_response_message(self, message):
        data = json.loads(message.payload.decode("utf-8"))
        if data.get('status') == 'error':
            self.fingerprint.on_fail()
        else:
            self.fingerprint.on_success()

    def handle_renew_certificate(self, message):
        print("Updating certificates...")
        # Parse the message payload as JSON
        payload = json.loads(message.payload.decode())
        # Validate payload keys
        required_keys = ["cafile", "certfile", "keyfile"]
        for key in required_keys:
            if key not in payload:
                raise ValueError(f"Missing required key in payload: {key}")

        # Write new certificates to their respective files
        with open(self.CERTS["cafile"], "w") as cafile:
            cafile.write(payload["cafile"])
            print(f"Updated CA file: {self.CERTS['cafile']}")

        with open(self.CERTS["certfile"], "w") as certfile:
            certfile.write(payload["certfile"])
            print(f"Updated client certificate file: {self.CERTS['certfile']}")

        with open(self.CERTS["keyfile"], "w") as keyfile:
            keyfile.write(payload["keyfile"])
            print(f"Updated private key file: {self.CERTS['keyfile']}")

        # Reload MQTT client with new certificates
        self.mqtt_client.loop_stop()
        self.mqtt_client = self.setup_mqtt_client()

    def extract_user_id(self, message) -> str:
        """
        Extract the user ID from the MQTT message properties.
        :param message: The MQTT message object.
        :return: The user ID as a string.
        """
        try:
            return dict(message.properties.UserProperty)["user_id"]
        except Exception as e:
            self.logger.error(f"Failed to extract user ID from message: {e}")
            return "unknown_user"

    def handle_relay_control_message(self, message):
        """
        Handle 'RELAY_CONTROL' MQTT messages.

        Expected JSON payload format:
        {
            "relay": "main" or "aux" (or 58/59),
            "action": "activate" or "deactivate",
            "duration": seconds (0 or negative = emergency/indefinite),
            "emergency": true/false (optional, alternative to duration=0)
        }
        """
        if self.relay_controller is None:
            self.logger.error("Relay controller not initialized, cannot handle relay control message")
            return

        try:
            payload = json.loads(message.payload.decode("utf-8"))

            relay_identifier = payload.get("relay")
            action = payload.get("action", "activate")
            duration = payload.get("duration", 0)
            emergency = payload.get("emergency", False)

            # Resolve relay pin from name or number
            relay_pin = self._resolve_relay_pin(relay_identifier)
            if relay_pin is None:
                self.logger.error(f"Invalid relay identifier: {relay_identifier}")
                return

            # Handle emergency mode
            if emergency:
                duration = 0  # Emergency mode = indefinite

            relay_name = config.RELAY_PIN_TO_NAME.get(relay_pin, str(relay_pin))
            self.logger.info(f"Relay control: {relay_name} (pin {relay_pin}), action={action}, duration={duration}")

            if action == "activate":
                success = self.relay_controller.activate_relay(relay_pin, duration)
            elif action == "deactivate":
                success = self.relay_controller.deactivate_relay(relay_pin, force=True)
            else:
                self.logger.error(f"Invalid relay action: {action}")
                return

            # Publish status update
            if success:
                self.publish_relay_status(relay_pin, action, duration)

        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse relay control message: {e}")
        except Exception as e:
            self.logger.error(f"Error handling relay control message: {e}")

    def _resolve_relay_pin(self, identifier):
        """
        Resolve relay pin from logical name or pin number.

        Args:
            identifier: Either a logical name ("main", "aux") or pin number (58, 59)

        Returns:
            int: GPIO pin number, or None if invalid
        """
        # If identifier is a string, try to resolve from name mapping
        if isinstance(identifier, str):
            if identifier in config.RELAY_PIN_NAMES:
                return config.RELAY_PIN_NAMES[identifier]

        # If identifier is an int, validate it's a valid relay pin
        if isinstance(identifier, int):
            if identifier in config.RELAY_PINS:
                return identifier

        return None

    def publish_relay_status(self, relay_pin: int, action: str, duration: int = 0):
        """Publish relay status change to MQTT."""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state = self.relay_controller.get_relay_state(relay_pin)
            relay_name = config.RELAY_PIN_TO_NAME.get(relay_pin, str(relay_pin))

            payload = json.dumps({
                "relay": relay_name,  # Use logical name
                "relay_pin": relay_pin,  # Also include pin number for reference
                "action": action,
                "duration": duration,
                "state": state,
                "timestamp": timestamp
            })

            self.mqtt_client.publish(
                f"device/{self.device_id}/RELAY_STATUS",
                payload,
                qos=1
            )
            self.logger.info(f"Relay status published: {relay_name} (pin {relay_pin}), action={action}")

        except Exception as e:
            self.logger.error(f"Failed to publish relay status: {e}")

    def handle_tunnel_notification(self, message):
        """
        Handle AWS IoT Secure Tunneling notification.

        Expected JSON payload format:
        {
            "clientAccessToken": "destination-token",
            "clientMode": "destination",
            "region": "us-east-1",
            "services": ["SSH"]
        }
        """
        try:
            payload = json.loads(message.payload.decode("utf-8"))

            client_token = payload.get("clientAccessToken")
            region = payload.get("region", config.TUNNEL_REGION)
            services = payload.get("services", ["SSH"])

            if not client_token:
                self.logger.error("Tunnel notification missing clientAccessToken")
                return

            self.logger.info(f"Received tunnel notification - Region: {region}, Services: {services}")

            # Launch the local proxy in a separate thread to avoid blocking
            threading.Thread(
                target=self._start_tunnel_proxy,
                args=(client_token, region),
                daemon=True
            ).start()

        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse tunnel notification: {e}")
        except Exception as e:
            self.logger.error(f"Error handling tunnel notification: {e}")

    def _start_tunnel_proxy(self, token, region):
        """
        Launch the AWS IoT Secure Tunneling local proxy.

        Args:
            token: Destination access token
            region: AWS region
        """
        import subprocess

        try:
            # Check if local proxy binary exists
            if not os.path.exists(config.TUNNEL_LOCALPROXY_PATH):
                self.logger.error(f"Local proxy binary not found at {config.TUNNEL_LOCALPROXY_PATH}")
                return

            # Build command: localproxy -r <region> -d localhost:22 -t <token> --destination-client-type V1
            cmd = [
                config.TUNNEL_LOCALPROXY_PATH,
                "-r", region,
                "-d", f"localhost:{config.TUNNEL_DESTINATION_PORT}",
                "-t", token,
                "--destination-client-type", "V1"
            ]

            self.logger.info(f"Starting local proxy for region {region}, destination port {config.TUNNEL_DESTINATION_PORT}")

            # Launch the local proxy process
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            self.logger.info(f"Local proxy started with PID {process.pid}")

            # Monitor the process in a non-blocking way
            # The proxy will run until the tunnel is closed by AWS or encounters an error
            stdout, stderr = process.communicate()

            if process.returncode == 0:
                self.logger.info("Local proxy exited successfully")
            else:
                self.logger.error(f"Local proxy exited with code {process.returncode}")
                if stderr:
                    self.logger.error(f"Local proxy stderr: {stderr}")

        except Exception as e:
            self.logger.error(f"Failed to start local proxy: {e}", exc_info=True)

    def handle_ota_update(self, message):
        """
        Handle OTA update request from MQTT.

        Expected JSON payload format:
        {
            "type": "script" or "asset",
            "target_path": "/opt/app/main.py",
            "content": "base64_encoded_content",  # For type=script
            "url": "https://example.com/file",    # For type=asset
            "checksum": "sha256_hash",
            "version": "1.0.1",
            "restart_required": true
        }
        """
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            self.logger.info(f"Received OTA update request: type={payload.get('type')}, target={payload.get('target_path')}")

            # Delegate to OTA updater
            self.ota_updater.handle_ota_update(payload)

        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse OTA update message: {e}")
        except Exception as e:
            self.logger.error(f"Error handling OTA update: {e}")
