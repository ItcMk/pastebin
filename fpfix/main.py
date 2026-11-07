#!/usr/bin/env python3
"""
FingerBoard Application - AWS IoT Fingerprint Reader with GPS
Main entry point

Location: /opt/app/main.py
Started by: /etc/init.d/S99python
Logs to: /var/log/python_app.log
"""

import sys
import signal
import time
import logging
from logging.handlers import TimedRotatingFileHandler
from threading import Event, current_thread as get_current_thread
from periphery import GPIO

# Import configuration
import config

# Import modules
from modules.fingerprint import FingerPrint
from modules.timeUtil import TimeUtil
from modules.utils import Utils
from modules.monitorGpio import GPIOMonitorThread
from modules.mqtt import Mqtt
from modules.gps import GPSMQTTThread
from modules.relay import RelayController
from modules.watchdog import Watchdog

# Global state
running = True
polling_control = Event()
polling_control.set()  # Initially allow polling

# GPIO will be initialized in setup()
Read_GPIO = None

# Watchdog will be initialized in setup()
watchdog = None


def setup_logger():
    """Configure logging with rotation"""
    logger = logging.getLogger(__name__)
    logger.setLevel(getattr(logging, config.LOG_LEVEL))

    # Create handler with rotation
    handler = TimedRotatingFileHandler(
        config.LOG_FILE,
        when=config.LOG_ROTATION,
        interval=1,
        backupCount=config.LOG_BACKUP_COUNT
    )

    # Set format
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Also log to stdout (goes to /var/log/python_app.log via init script)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger


# Setup logger
logger = setup_logger()


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global running
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    running = False
    polling_control.set()  # Wake up any waiting threads

    # Raise KeyboardInterrupt to break out of blocking calls
    raise KeyboardInterrupt("Signal received")


def gpio_polling_loop(mqtt_client):
    """Poll for fingerprint detection via GPIO"""
    global running, watchdog

    last_trigger_time = 0  # Track last button press time for debouncing
    last_pin_state = Read_GPIO.read()  # Track pin state changes

    # Determine expected active state based on edge configuration
    expected_active_state = (Read_GPIO.edge == "rising")  # True if rising edge (LOW->HIGH)

    # Register this thread with watchdog
    if watchdog:
        watchdog.register_thread("GPIO_Polling", get_current_thread().ident)

    try:
        logger.info(f"Starting GPIO polling loop (edge: {Read_GPIO.edge}, active_state: {'HIGH' if expected_active_state else 'LOW'})...")
        logger.info(f"Initial pin state: {last_pin_state}")

        while running:
            try:
                # Send heartbeat to watchdog
                if watchdog:
                    watchdog.heartbeat("GPIO_Polling")

                # Wait if polling is paused (with timeout to check running flag)
                polling_control.wait(timeout=0.5)

                if not running:
                    break

                # Wait for edge event (short timeout for responsiveness)
                if Read_GPIO.poll(timeout=config.POLL_TIMEOUT):
                    pin_state = Read_GPIO.read()
                    current_time = time.time() * 1000  # milliseconds

                    # Check if pin is in active state (depends on edge type configured)
                    is_active = (pin_state == expected_active_state)

                    # Debounce: Only accept trigger if enough time has passed since last trigger
                    if is_active and polling_control.is_set():
                        if (current_time - last_trigger_time) >= config.GPIO_DEBOUNCE_MS:
                            logger.info(f"Finger detected (pin state: {pin_state}). Capturing fingerprint...")
                            last_trigger_time = current_time

                            # Verify state is still active after a small delay
                            time.sleep(0.05)  # 50ms verification delay
                            current_state = Read_GPIO.read()
                            if current_state == expected_active_state:  # Still in active state
                                frame_list = fingerprint.capture_fingerprint()
                                if frame_list:
                                    mqtt_client.publish_fingerprint_event(frame_list)
                            else:
                                logger.debug(f"Finger detection verification failed (state changed: {current_state})")

                    last_pin_state = pin_state

            except (KeyboardInterrupt, SystemExit):
                logger.info("Interrupt received in polling loop")
                running = False
                break

    except Exception as e:
        logger.error(f"Error in GPIO polling loop: {e}", exc_info=True)
    finally:
        logger.info("GPIO polling loop stopped")


def setup():
    """Initialize application components"""
    global timeutil, utils, fingerprint, relay_controller, Read_GPIO, watchdog

    logger.info("=" * 60)
    logger.info("FingerBoard Application Starting")
    logger.info("=" * 60)

    # Initialize watchdog first for maximum protection
    watchdog = Watchdog(logger)
    watchdog.start()

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Initialize GPIO for fingerprint button
    try:
        Read_GPIO = GPIO(config.GPIO_READ_PIN, "in")

        # Try pull-down first (sensor might output HIGH when finger detected)
        try:
            Read_GPIO.bias = "pull_down"
            logger.info(f"Configured pull-down resistor on GPIO pin {config.GPIO_READ_PIN}")
        except Exception as e:
            logger.warning(f"Could not set pull-down on GPIO {config.GPIO_READ_PIN}: {e}")
            logger.warning("Pin will float - may need external resistor")

        logger.info(f"Initialized GPIO pin {config.GPIO_READ_PIN} for fingerprint detection")
    except Exception as e:
        logger.error(f"Failed to initialize GPIO pin {config.GPIO_READ_PIN}: {e}")
        raise

    # Initialize utility classes
    timeutil = TimeUtil(logger)
    utils = Utils(logger)
    fingerprint = FingerPrint(logger, utils)

    # Flash R503 LED blue during startup
    fingerprint.fp.led_control(ctrl=1, color=2, speed=80)

    # Edge detection is configured HERE, after the sensor is up and answering.
    #
    # WAKEUP's idle level only means anything once the R503 is powered and driving it.
    # Sampling it before connecting to the sensor read a line nothing was driving yet - and
    # because the pull-down silently fails on sysfs GPIO ("does not support line bias
    # property"), that was a read of a floating pin. Whatever it caught then fixed the
    # polarity for the whole session, so two identical devices could disagree about which
    # edge means "finger down", and one of them rejected every placed finger.
    try:
        time.sleep(0.3)  # let WAKEUP settle now that the sensor is answering
        initial_state = Read_GPIO.read()
        logger.info(f"Initial GPIO pin {config.GPIO_READ_PIN} state: {initial_state} ({'HIGH' if initial_state else 'LOW'})")
        if initial_state:
            Read_GPIO.edge = "falling"  # Idle HIGH, detect LOW transition
            logger.info("Configured for FALLING edge (HIGH->LOW when finger detected)")
        else:
            Read_GPIO.edge = "rising"  # Idle LOW, detect HIGH transition
            logger.info("Configured for RISING edge (LOW->HIGH when finger detected)")
    except Exception as e:
        logger.error(f"Failed to configure edge detection on GPIO {config.GPIO_READ_PIN}: {e}")
        raise

    # Initialize relay controller
    relay_controller = RelayController(logger)
    logger.info("Relay controller initialized")

    # Load device ID
    config.DEVICE_ID = utils.get_device_id()
    logger.info(f"Device ID: {config.DEVICE_ID}")

    # Play startup sound
    utils.play_sound(config.SOUNDS["startup"])

    # Wait for internet if configured
    if config.WAIT_FOR_INTERNET:
        utils.wait_for_internet()

    # Check and update system time
    timeutil.check_and_update_date()

    # Stop startup sound and LED
    utils.stop_sound()
    fingerprint.turn_off_led()

    logger.info("Initialization complete")


def main_loop():
    """Main application loop"""
    global running

    logger.info("Starting main application loop...")

    # Create stop event for threads
    stop_event = Event()

    # Define callback for pin 55 (door open request) to trigger relay 58 (main)
    def pin55_callback():
        """Auto-trigger relay 58 (main) for 1 minute when door open button is pressed"""
        logger.info("Door open request detected (pin 55), activating main relay (pin 58) for 60 seconds")
        relay_controller.activate_relay(58, config.RELAY_58_AUTO_TRIGGER_DURATION)

    # Initialize MQTT client with relay controller
    mqtt_client = Mqtt(
        polling_control,
        fingerprint,
        Read_GPIO,
        utils,
        logger,
        relay_controller
    )

    # Start GPIO monitor threads for each monitored pin
    gpio_threads = []
    for pin, (postfix, sound, inverted, debounce_sec, verify_delay_ms) in config.GPIO_MONITORS.items():
        # Add callback for pin 55 (door open request)
        callback = pin55_callback if pin == 55 else None

        thread = GPIOMonitorThread(
            pin, postfix, sound, stop_event,
            mqtt_client, config.DEVICE_ID, utils, callback, inverted,
            debounce_sec, verify_delay_ms
        )
        thread.start()
        gpio_threads.append(thread)
        logger.info(f"Started GPIO monitor thread for pin {pin} ({postfix})")

    # Start GPS thread
    gps_thread = GPSMQTTThread(mqtt_client, stop_event, logger, config.DEVICE_ID, watchdog)
    gps_thread.start()
    logger.info("Started GPS thread")

    # Run GPIO polling loop in main thread
    try:
        gpio_polling_loop(mqtt_client)
    except Exception as e:
        logger.error(f"Error in main loop: {e}", exc_info=True)
    finally:
        # Signal threads to stop
        logger.info("Stopping background threads...")
        stop_event.set()
        running = False

        # Wait for GPIO monitor threads
        for thread in gpio_threads:
            thread.join(timeout=5)

        # Wait for GPS thread
        gps_thread.join(timeout=5)

        logger.info("All background threads stopped")


def cleanup():
    """Cleanup resources"""
    logger.info("Cleaning up resources...")

    try:
        # Stop watchdog first to prevent reboot during cleanup
        if watchdog:
            watchdog.stop()
            logger.info("Watchdog stopped")

        # Cleanup relay controller
        if 'relay_controller' in globals():
            relay_controller.cleanup()
            logger.info("Relay controller cleaned up")

        # Close GPIO
        if Read_GPIO:
            Read_GPIO.close()
            logger.info("GPIO closed")

        # Stop any playing sounds
        utils.stop_sound()

    except Exception as e:
        logger.error(f"Error during cleanup: {e}", exc_info=True)

    logger.info("Cleanup complete")


def main():
    """Main entry point"""
    try:
        setup()
        main_loop()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1
    finally:
        cleanup()

    logger.info("FingerBoard application stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
