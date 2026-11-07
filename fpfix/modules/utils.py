import logging
import threading
import subprocess
import sys

import socket
import time
import os
import uuid
import config


class Utils:

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._connectivity_monitor_thread = None
        self._stop_monitoring = threading.Event()
        self._connected = False
        self._sound_process = None
        self._device_id_cache = None  # Cache device ID to avoid repeated file reads

    def play_sound(self, soundfile: str):
        """
        Play a sound file using mpv (lightweight, already in buildroot).
        Falls back to ffplay if mpv not available.
        """
        try:
            # Stop any currently playing sound
            self.stop_sound()

            # Check if file exists
            if not os.path.exists(soundfile):
                self.logger.warning(f"Sound file not found: {soundfile}")
                return

            # Try mpv first (preferred)
            try:
                self._sound_process = subprocess.Popen(
                    ['mpv', '--no-video', '--really-quiet', soundfile],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                return
            except FileNotFoundError:
                pass

            # Fallback to ffplay
            try:
                self._sound_process = subprocess.Popen(
                    ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', soundfile],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                return
            except FileNotFoundError:
                self.logger.error("Neither mpv nor ffplay available for audio playback")

        except Exception as e:
            self.logger.error(f"Failed to play sound: {e}")

    def stop_sound(self):
        """Stop sound playback."""
        try:
            if self._sound_process:
                try:
                    if self._sound_process.poll() is None:
                        self._sound_process.terminate()
                        self._sound_process.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    # Process already gone or hung, force kill
                    try:
                        self._sound_process.kill()
                    except ProcessLookupError:
                        pass  # Already terminated
                finally:
                    self._sound_process = None
        except Exception as e:
            self.logger.error(f"Failed to stop sound: {e}")

    def wait_for_button_press(self, pin, timeout=30) -> bool:
        """
        Wait for a button press with a timeout.
        :param timeout: Timeout in seconds.
        :return: True if button was pressed, False if timeout occurred.
        """
        try:
            if pin.poll(timeout=timeout):
                pin_state = pin.read()
                # Which level means "finger present" is decided at boot from the pin's idle
                # level (see main.py). Hard-coding LOW was wrong on any device that came up
                # idle-LOW: a placed finger was rejected, and since it then stayed down no
                # further edge was ever produced, so the device went silent until the finger
                # was swiped. Use the same rule the GPIO polling loop uses.
                try:
                    expected_active_state = (pin.edge == "rising")
                except Exception as e:
                    self.logger.warning(f"Could not read edge configuration, assuming LOW: {e}")
                    expected_active_state = False
                return pin_state == expected_active_state
        except Exception as e:
            self.logger.error(f"Error while waiting for button press: {e}")
        return False

    def is_connected(self, host="8.8.8.8", port=53, timeout=3):
        """
        Check if there is an internet connection by attempting to connect to a well-known host.
        Default is Google's public DNS server (8.8.8.8) on port 53.

        Args:
            host (str): The host to connect to (default: 8.8.8.8).
            port (int): The port to connect to (default: 53).
            timeout (int): Timeout in seconds for the connection attempt (default: 3).

        Returns:
            bool: True if the connection is successful, False otherwise.
        """
        try:
            # Create a socket and attempt to connect
            socket.setdefaulttimeout(timeout)
            with socket.create_connection((host, port), timeout):
                return True
        except (socket.timeout, socket.error):
            return False

    def wait_for_internet(self):
        """
        Wait until internet connectivity is available with improved monitoring.
        Checks every minute until connected, then starts periodic monitoring every 15 minutes.
        """
        self.logger.info("Waiting for internet connectivity...")

        # Initial connectivity check - every minute until connected
        while not self.is_connected():
            self.logger.warning("No internet connection. Retrying in 1 minute...")
            self.stop_sound()
            self.play_sound(config.SOUNDS["no_internet"])
            time.sleep(60)  # Wait 1 minute before retrying

        self._connected = True
        self.logger.info("Internet connection established!")

        # Start periodic connectivity monitoring
        self.start_connectivity_monitoring()

    def start_connectivity_monitoring(self):
        """
        Start background thread to monitor connectivity every 15 minutes after initial connection.
        """
        if self._connectivity_monitor_thread is None or not self._connectivity_monitor_thread.is_alive():
            self._stop_monitoring.clear()
            self._connectivity_monitor_thread = threading.Thread(
                target=self._connectivity_monitor_loop,
                daemon=True,
                name="ConnectivityMonitor"
            )
            self._connectivity_monitor_thread.start()
            self.logger.info("Started periodic connectivity monitoring (15-minute intervals)")

    def stop_connectivity_monitoring(self):
        """
        Stop the connectivity monitoring thread.
        """
        if self._connectivity_monitor_thread and self._connectivity_monitor_thread.is_alive():
            self._stop_monitoring.set()
            self._connectivity_monitor_thread.join(timeout=5)
            self.logger.info("Stopped connectivity monitoring")

    def _connectivity_monitor_loop(self):
        """
        Background loop that checks connectivity every 15 minutes.
        """
        while not self._stop_monitoring.is_set():
            # Wait for 15 minutes (900 seconds) or until stop signal
            if self._stop_monitoring.wait(timeout=900):  # 15 minutes
                break

            # Check connectivity
            if self.is_connected():
                if not self._connected:
                    self._connected = True
                    self.logger.info("Internet connectivity restored")
            else:
                if self._connected:
                    self._connected = False
                    self.logger.warning("Internet connectivity lost - will continue monitoring")
                    # Optional: play sound notification for connectivity loss
                    try:
                        self.play_sound(config.SOUNDS["no_internet"])
                    except Exception as e:
                        self.logger.error(f"Failed to play connectivity loss sound: {e}")

    def is_internet_connected(self):
        """
        Get the current connectivity status.

        Returns:
            bool: True if internet is currently connected, False otherwise.
        """
        return self._connected

    def get_device_id(self):
        """Get device ID with caching to avoid repeated file I/O"""
        # Return cached value if available
        if self._device_id_cache is not None:
            return self._device_id_cache

        file_path = config.DEVICE_ID_FILE

        # Check if the file exists
        if os.path.exists(file_path):
            # If the file exists, read and return its content
            with open(file_path, 'r') as file:
                content = file.read().strip()
        else:
            # If the file doesn't exist, create it with a random UUID as content
            random_uuid = str(uuid.uuid4())
            with open(file_path, 'w') as file:
                file.write(random_uuid)
            content = random_uuid

        # Cache for future calls
        self._device_id_cache = content
        return content

