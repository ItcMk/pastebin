import math
import time
import sys
from .micropyGPS import MicropyGPS
import serial

try:
    import config as _config
except Exception:                      # importable standalone, e.g. in a bring-up script
    _config = None


def _gps_trace(message):
    """
    Bring-up chatter, off on a deployed device.

    These lines go straight to stdout, which the init script redirects into the log file.
    Indoors the receiver never gets a fix, so this was several thousand identical lines a
    day sitting on top of the fingerprint traffic the log is actually kept for. Set
    GPS_VERBOSE in config.py when bringing a board up.
    """
    if getattr(_config, "GPS_VERBOSE", False):
        sys.stdout.write(message)

g = MicropyGPS(+8)
Temp = '0123456789ABCDEF*'
BUFFSIZE = 1100

pi = 3.14159265358979324
a = 6378245.0
ee = 0.00669342162296594323
x_pi = 3.14159265358979324 * 3000.0 / 180.0


class L76X(object):
    Lon = 0.0
    Lat = 0.0
    Lon_area = 'E'
    Lat_area = 'W'
    Time_H = 0
    Time_M = 0
    Time_S = 0
    Status = 0
    Lon_Baidu = 0.0
    Lat_Baidu = 0.0
    Lon_Google = 0.0
    Lat_Google = 0.0

    GPS_Lon = 0
    GPS_Lat = 0

    # Startup mode
    SET_HOT_START = '$PMTK101'
    SET_WARM_START = '$PMTK102'
    SET_COLD_START = '$PMTK103'
    SET_FULL_COLD_START = '$PMTK104'

    # Standby mode -- Exit requires high level trigger
    SET_PERPETUAL_STANDBY_MODE = '$PMTK161'

    SET_PERIODIC_MODE = '$PMTK225'
    SET_NORMAL_MODE = '$PMTK225,0'
    SET_PERIODIC_BACKUP_MODE = '$PMTK225,1,1000,2000'
    SET_PERIODIC_STANDBY_MODE = '$PMTK225,2,1000,2000'
    SET_PERPETUAL_BACKUP_MODE = '$PMTK225,4'
    SET_ALWAYSLOCATE_STANDBY_MODE = '$PMTK225,8'
    SET_ALWAYSLOCATE_BACKUP_MODE = '$PMTK225,9'

    # Set the message interval,100ms~10000ms
    SET_POS_FIX = '$PMTK220'
    SET_POS_FIX_100MS = '$PMTK220,100'
    SET_POS_FIX_200MS = '$PMTK220,200'
    SET_POS_FIX_400MS = '$PMTK220,400'
    SET_POS_FIX_800MS = '$PMTK220,800'
    SET_POS_FIX_1S = '$PMTK220,1000'
    SET_POS_FIX_2S = '$PMTK220,2000'
    SET_POS_FIX_4S = '$PMTK220,4000'
    SET_POS_FIX_8S = '$PMTK220,8000'
    SET_POS_FIX_10S = '$PMTK220,10000'

    # Switching time output
    SET_SYNC_PPS_NMEA_OFF = '$PMTK255,0'
    SET_SYNC_PPS_NMEA_ON = '$PMTK255,1'

    #
    SET_GALAXIES = '$PAIR066,1,1,1,1,1,0'

    # To restore the system default setting
    SET_REDUCTION = '$PMTK314,-1'

    # Set NMEA sentence output frequencies
    SET_NMEA_OUTPUT = '$PMTK314,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,1,0'
    # Baud rate
    SET_NMEA_BAUDRATE = '$PMTK251'
    SET_NMEA_BAUDRATE_115200 = '$PMTK251,115200'
    SET_NMEA_BAUDRATE_57600 = '$PMTK251,57600'
    SET_NMEA_BAUDRATE_38400 = '$PMTK251,38400'
    SET_NMEA_BAUDRATE_19200 = '$PMTK251,19200'
    SET_NMEA_BAUDRATE_14400 = '$PMTK251,14400'
    SET_NMEA_BAUDRATE_9600 = '$PMTK251,9600'
    SET_NMEA_BAUDRATE_4800 = '$PMTK251,4800'

    def __init__(self):
        self.port = "/dev/ttyS4"

    def L76X_Send_Command(self, data):
        Check = ord(data[1])
        for i in range(2, len(data)):
            Check = Check ^ ord(data[i])
        data = data + Temp[16]
        data = data + Temp[(Check // 16)]
        data = data + Temp[(Check % 16)]
        self.Uart_SendString(data.encode())
        self.Uart_SendByte('\r'.encode())
        self.Uart_SendByte('\n'.encode())
        _gps_trace(data + "\n")   # the raw $PMTK/$PAIR command, bring-up only

    def L76X_Gat_GNRMC(self):
        data = ''
        sentences_received = []
        max_attempts = 20  # Try to read up to 20 sentences before giving up
        attempts = 0

        _gps_trace(f"[GPS] Starting to read GPS data (max {max_attempts} sentences)...\n")
        sys.stdout.flush()

        while attempts < max_attempts:
            if g.valid:
                self.Status = 1
            else:
                self.Status = 0

            x = self.Uart_ReceiveByte()

            # Check for timeout (empty read)
            if x == b'':
                _gps_trace(f"[GPS] No data received (timeout after {attempts} attempts)\n")
                _gps_trace(f"[GPS] Check hardware connection and baud rate\n")
                sys.stdout.flush()
                self.Status = 0
                return

            if x == b'$':
                while (x != b'\r'):
                    # Handle decoding errors gracefully (skip non-ASCII bytes)
                    try:
                        char = x.decode('ascii')
                        data += char
                        g.update(char)
                    except (UnicodeDecodeError, AttributeError):
                        # Skip invalid bytes (likely binary ACK/NAK responses)
                        pass
                    x = self.Uart_ReceiveByte()

                    # Check for timeout in sentence read
                    if x == b'':
                        _gps_trace(f"[GPS] Incomplete sentence: {data}\n")
                        sys.stdout.flush()
                        self.Status = 0
                        return

                data += '\r\n'
                sentences_received.append(data.strip())
                attempts += 1

                if attempts % 5 == 0:  # Progress update every 5 sentences
                    _gps_trace(f"[GPS] Read {attempts} sentences, looking for GNGLL/GPGLL...\n")
                    sys.stdout.flush()

                if '$GNGLL' in data or '$GPGLL' in data:
                    _gps_trace(f"[GPS] Found GLL sentence after {attempts} attempts\n")
                    sys.stdout.flush()
                    break

                data = ''  # Reset for next sentence

        if self.Status == 1 and g.valid:
            self.Lat = g.latitude[0] + g.latitude[1] / 100
            self.Lon = g.longitude[0] + g.longitude[1] / 100
            if g.latitude[2] != 'N':
                self.Lat = self.Lat * (-1)
            if g.longitude[2] != 'E':
                self.Lon = self.Lon * (-1)
            self.Time_H = g.timestamp[0]
            self.Time_M = g.timestamp[1]
            self.Time_S = g.timestamp[2]

        _gps_trace(f"[GPS] Status: {self.Status}, Valid: {g.valid}, Sentences: {len(sentences_received)}\n")
        if self.Status == 0 and sentences_received:
            _gps_trace(f"[GPS] Sample: {sentences_received[0]}\n")
        sys.stdout.flush()

    def transformLat(self, x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * pi) + 320 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
        return ret

    def transformLon(self, x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
        return ret

    def bd_encrypt(self):
        x = self.Lon_Goodle
        y = self.Lat_Goodle
        z = math.sqrt(x * x + y * y) + 0.00002 * math.sin(y * x_pi)
        theta = math.atan2(y, x) + 0.000003 * math.cos(x * x_pi)
        self.Lon_Baidu = z * math.cos(theta) + 0.0065
        self.Lat_Baidu = z * math.sin(theta) + 0.006

    def transform(self):
        dLat = self.transformLat(self.GPS_Lon - 105.0, self.GPS_Lat - 35.0)
        dLon = self.transformLon(self.GPS_Lon - 105.0, self.GPS_Lat - 35.0)
        radLat = self.GPS_Lat / 180.0 * pi
        magic = math.sin(radLat)
        magic = 1 - ee * magic * magic
        math.sqrtMagic = math.sqrt(magic)
        dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * math.sqrtMagic) * pi)
        dLon = (dLon * 180.0) / (a / math.sqrtMagic * math.cos(radLat) * pi)
        self.Lat_Goodle = self.GPS_Lat + dLat
        self.Lon_Goodle = self.GPS_Lon + dLon

    def L76X_Baidu_Coordinates(self, U_Lat, U_Lon):
        self.GPS_Lat = U_Lat % 1 * 100 / 60 + math.floor(U_Lat)
        self.GPS_Lon = U_Lon % 1 * 100 / 60 + math.floor(U_Lon)
        self.transform()
        self.bd_encrypt()

    def L76X_Google_Coordinates(self, U_Lat, U_Lon):
        self.GPS_Lat = U_Lat % 1 / 60 + U_Lat / 1
        self.GPS_Lon = U_Lon % 1 / 60 + U_Lon / 1
        self.transform()

    def L76X_Set_Baudrate(self, Baudrate):
        self.Uart_Set_Baudrate(Baudrate)

    def Uart_Set_Baudrate(self, Baudrate=9600):
        # GPS modules use 8N1 format: 8 data bits, no parity, 1 stop bit
        self.serial = serial.Serial(
            port=self.port,
            baudrate=Baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=5,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False
        )
        _gps_trace(f"[GPS] Serial port opened: {self.port} @ {Baudrate} baud (8N1)\n")
        sys.stdout.flush()

    def Uart_SendByte(self, value):
        self.serial.write(value)

    def Uart_SendString(self, value):
        self.serial.write(value)

    def Uart_ReceiveByte(self):
        return self.serial.read(1)

    def Uart_ReceiveString(self, value):
        data = self.serial.read(value)
        return data

    def Uart_FlushInput(self):
        """Flush input buffer to clear any pending data (ACK/NAK responses)"""
        if hasattr(self, 'serial') and self.serial:
            self.serial.reset_input_buffer()
