import logging
import threading
import json
import time
from datetime import datetime
from . import L76X
from .mqtt import Mqtt
import config


class GPSMQTTThread(threading.Thread):
    """
    A thread that reads GPS data and sends an MQTT message at configured intervals.
    """
    def __init__(self, mqtt: Mqtt, stop_event: threading.Event, logger: logging.Logger, device_id, watchdog=None):
        super().__init__()
        self.mqtt = mqtt
        self.stop_event = stop_event
        self.gps = L76X.L76X()
        self.logger = logger
        self.device_id = device_id
        self.watchdog = watchdog

    def run(self):
        self.logger.info("Starting GPS MQTT Thread...")

        # Register with watchdog
        if self.watchdog:
            self.watchdog.register_thread("GPS_Thread", threading.current_thread().ident)

        try:
            # Initialize GPS module
            self.gps.L76X_Set_Baudrate(config.GPS_BAUDRATE)
            time.sleep(0.5)  # Wait for serial port to initialize

            # Send initialization commands with delays for GPS to process
            self.gps.L76X_Send_Command(self.gps.SET_POS_FIX_1S)
            time.sleep(0.3)  # Wait for command to be processed
            self.gps.L76X_Send_Command(self.gps.SET_NMEA_OUTPUT)
            time.sleep(0.3)
            self.gps.L76X_Send_Command(self.gps.SET_GALAXIES)
            time.sleep(0.5)

            # Flush input buffer to clear ACK/NAK responses
            self.gps.Uart_FlushInput()
            self.logger.info("GPS initialization complete, buffer flushed")

            while not self.stop_event.is_set():
                # Send heartbeat to watchdog
                if self.watchdog:
                    self.watchdog.heartbeat("GPS_Thread")

                try:
                    self.gps.L76X_Gat_GNRMC()
                    if self.gps.Status == 1:
                        self.logger.info(f"GPS: Positioned - Lat: {self.gps.Lat}, Lon: {self.gps.Lon}")
                        self.publish_gps_data()
                    else:
                        # Said once, then at most hourly. These terminals are indoors and
                        # never get a fix, so repeating it every poll produced hundreds of
                        # identical lines a day and buried the fingerprint traffic the log
                        # exists for.
                        now = time.time()
                        interval = getattr(config, "GPS_NO_FIX_WARN_INTERVAL", 3600)
                        if now - getattr(self, "_last_no_fix_warning", 0) >= interval:
                            self._last_no_fix_warning = now
                            self.logger.warning(
                                "GPS: No satellite fix yet (needs clear sky view). "
                                "Repeated at most once an hour.")
                except Exception as e:
                    self.logger.error(f"GPS read error: {e}")

                time.sleep(config.GPS_UPDATE_INTERVAL)
        except Exception as e:
            self.logger.error(f"Error in GPS MQTT Thread: {e}")
            self.logger.error("Common causes: wrong baud rate, binary ACK/NAK in buffer, or hardware issue")
            self.logger.error(f"Current config: port={config.GPS_SERIAL_PORT}, baud={config.GPS_BAUDRATE}")
        finally:
            self.logger.info("GPS MQTT Thread stopped.")

    def publish_gps_data(self):
        """
        Publishes GPS data to the MQTT broker.
        """
        try:
            # Convert GPS coordinates to Baidu coordinates
            self.gps.L76X_Baidu_Coordinates(self.gps.Lat, self.gps.Lon)

            # Prepare payload
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payload = {
                "dateTime": timestamp,
                "latitude": self.gps.GPS_Lat,
                "longitude": self.gps.GPS_Lon,
                "status": "Positioned" if self.gps.Status == 1 else "No Positioning",
            }

            # Publish to MQTT broker
            self.mqtt.mqtt_client.publish(f"device/{self.device_id}/LOCATION", json.dumps(payload), qos=1)
            self.logger.info(f"Published GPS data: {payload}")
        except Exception as e:
            self.logger.error(f"Failed to publish GPS data: {e}")
