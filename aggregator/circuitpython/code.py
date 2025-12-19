# Washing Machine Aggregator - CircuitPython
# Hardware: Seeed XIAO ESP32S3 + Grove Wio-E5 LoRa Module (or similar SX1262)
#
# This aggregator:
# 1. Scans for BLE advertisements from sensor nodes
# 2. Parses vibration data from manufacturer-specific data
# 3. Forwards data via LoRa to the central server

import time
import json
import struct
import board
import busio

# BLE imports
from adafruit_ble import BLERadio
from adafruit_ble.advertising import Advertisement
from adafruit_ble.advertising.standard import ManufacturerData

# ============================================================================
# Configuration
# ============================================================================

def load_config():
    """Load configuration from config.json"""
    try:
        with open("/config.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {
            "aggregator_name": "Aggregator_1",
            "aggregator_id": 1,
            "ble_scan_duration_sec": 5,
            "lora_tx_interval_sec": 0,  # 0 = immediate
            "company_id": 0xFFFF,
            "protocol_version": 1
        }

CONFIG = load_config()

# ============================================================================
# Data Storage
# ============================================================================

class SensorReading:
    """Stores a reading from a sensor node"""
    def __init__(self, machine_type, machine_id, rms_x100, freq_x10, battery_percent):
        self.machine_type = machine_type  # 1=washer, 2=dryer
        self.machine_id = machine_id
        self.rms_x100 = rms_x100
        self.freq_x10 = freq_x10
        self.battery_percent = battery_percent
        self.timestamp = time.monotonic()
    
    def __repr__(self):
        type_str = "W" if self.machine_type == 1 else "T"
        return f"[{type_str}] Machine {self.machine_id}: RMS={self.rms_x100/100:.2f}, Freq={self.freq_x10/10:.1f}Hz, Batt={self.battery_percent}%"

# Cache for received sensor data
sensor_cache = {}

# ============================================================================
# LoRa Module Interface
# ============================================================================

class LoRaModule:
    """
    Interface for SX1262-based LoRa module via UART.
    
    Supports modules like:
    - Seeed Grove Wio-E5
    - Waveshare SX1262 modules
    - Generic AT-command based SX1262 modules
    
    For Wio-E5, uses AT commands.
    For simple UART modules, uses transparent/stream mode.
    """
    
    def __init__(self, uart, mode="stream"):
        """
        Initialize LoRa module.
        
        Args:
            uart: busio.UART instance
            mode: "stream" for transparent mode, "at" for AT command mode
        """
        self.uart = uart
        self.mode = mode
        self.initialized = False
        
    def init(self):
        """Initialize the LoRa module"""
        if self.mode == "stream":
            # Stream mode - no initialization needed, just send data
            print("LoRa: Using stream/transparent mode")
            self.initialized = True
            return True
        else:
            # AT command mode initialization
            return self._init_at_mode()
    
    def _init_at_mode(self):
        """Initialize module using AT commands (for Wio-E5 etc.)"""
        print("LoRa: Initializing AT command mode...")
        
        # Test AT communication
        self._send_at("AT")
        time.sleep(0.1)
        
        # Configure LoRa parameters
        # These are for Wio-E5, adjust for your module
        commands = [
            "AT+MODE=TEST",           # Enter test mode for direct LoRa TX/RX
            "AT+TEST=RFCFG,868,SF10,125,12,15,14,ON,OFF,OFF",  # Configure RF
        ]
        
        for cmd in commands:
            response = self._send_at(cmd)
            if response and "+OK" not in response.upper() and "OK" not in response.upper():
                print(f"LoRa init warning: {cmd} -> {response}")
        
        self.initialized = True
        return True
    
    def _send_at(self, command, timeout=1.0):
        """Send AT command and get response"""
        # Clear input buffer
        while self.uart.in_waiting:
            self.uart.read(self.uart.in_waiting)
        
        # Send command
        self.uart.write(f"{command}\r\n".encode())
        
        # Wait for response
        start = time.monotonic()
        response = b""
        while time.monotonic() - start < timeout:
            if self.uart.in_waiting:
                response += self.uart.read(self.uart.in_waiting)
                if b"\r\n" in response:
                    break
            time.sleep(0.01)
        
        return response.decode().strip() if response else None
    
    def send(self, data: bytes):
        """
        Send data via LoRa.
        
        Args:
            data: bytes to send
        """
        if not self.initialized:
            print("LoRa: Not initialized!")
            return False
        
        if self.mode == "stream":
            # Stream mode - just write bytes directly
            self.uart.write(data)
            print(f"LoRa TX: {len(data)} bytes")
            return True
        else:
            # AT command mode - use TEST TX command
            hex_data = data.hex().upper()
            response = self._send_at(f"AT+TEST=TXLRPKT,\"{hex_data}\"", timeout=3.0)
            if response:
                print(f"LoRa TX response: {response}")
            return True
    
    def receive(self, timeout=0.1):
        """
        Check for received LoRa data.
        
        Returns:
            bytes or None
        """
        if self.uart.in_waiting:
            return self.uart.read(self.uart.in_waiting)
        return None


# ============================================================================
# BLE Scanner
# ============================================================================

def scan_for_sensors(ble, duration_sec):
    """
    Scan for BLE advertisements from washing machine sensors.
    
    Returns dict of machine_id -> SensorReading
    """
    found_sensors = {}
    
    print(f"Scanning BLE for {duration_sec}s...")
    
    for advertisement in ble.start_scan(timeout=duration_sec, minimum_rssi=-90):
        # Check for manufacturer data
        if not hasattr(advertisement, 'manufacturer_data') or not advertisement.manufacturer_data:
            continue
        
        # Look for our company ID
        company_id = CONFIG['company_id']
        
        if company_id in advertisement.manufacturer_data:
            mfg_data = advertisement.manufacturer_data[company_id]
            
            # Parse the data (excluding company ID which is in the key)
            # Protocol v2 format: version(1) + machine_type(1) + machine_id(1) + rms(2) + freq(2) + batt(1) = 8 bytes
            if len(mfg_data) >= 8:
                try:
                    version = mfg_data[0]
                    if version != CONFIG['protocol_version']:
                        print(f"Unknown protocol version: {version}")
                        continue
                    
                    machine_type = mfg_data[1]
                    machine_id = mfg_data[2]
                    rms_x100 = struct.unpack('<H', bytes(mfg_data[3:5]))[0]
                    freq_x10 = struct.unpack('<H', bytes(mfg_data[5:7]))[0]
                    battery = mfg_data[7]
                    
                    reading = SensorReading(machine_type, machine_id, rms_x100, freq_x10, battery)
                    found_sensors[machine_id] = reading
                    
                    print(f"  Found: {reading}")
                    
                except Exception as e:
                    print(f"Error parsing advertisement: {e}")
    
    ble.stop_scan()
    
    return found_sensors


# ============================================================================
# LoRa Packet Builder
# ============================================================================

def build_lora_packet(aggregator_id, sensors):
    """
    Build a LoRa packet containing all sensor data.
    
    Packet format (Protocol v2):
    - Byte 0: Aggregator ID
    - Byte 1: Machine count (N)
    - Bytes 2+: N × 7 bytes of machine data:
        - Byte 0: Machine Type (1=washer, 2=dryer)
        - Byte 1: Machine ID
        - Bytes 2-3: RMS × 100 (uint16 LE)
        - Bytes 4-5: Freq × 10 (uint16 LE)
        - Byte 6: Battery %
    
    Returns:
        bytes: The packet data
    """
    packet = bytearray()
    packet.append(aggregator_id)
    packet.append(len(sensors))
    
    for reading in sensors.values():
        packet.append(reading.machine_type)
        packet.append(reading.machine_id)
        packet.extend(struct.pack('<H', reading.rms_x100))
        packet.extend(struct.pack('<H', reading.freq_x10))
        packet.append(reading.battery_percent)
    
    return bytes(packet)


# ============================================================================
# Main
# ============================================================================

def main():
    print("\n" + "=" * 50)
    print("Washing Machine Aggregator")
    print(f"ID: {CONFIG['aggregator_id']}, Name: {CONFIG['aggregator_name']}")
    print("=" * 50 + "\n")
    
    # Initialize BLE
    print("Initializing BLE...")
    ble = BLERadio()
    print(f"  BLE Address: {ble.address_bytes.hex()}")
    
    # Initialize LoRa UART
    # Adjust pins based on your wiring!
    # For XIAO ESP32S3:
    #   TX -> D6 (GPIO43)
    #   RX -> D7 (GPIO44)
    print("Initializing LoRa UART...")
    try:
        uart = busio.UART(
            tx=board.TX,  # Adjust pin as needed
            rx=board.RX,  # Adjust pin as needed
            baudrate=115200,
            timeout=0.1
        )
        lora = LoRaModule(uart, mode="stream")
        lora.init()
        print("  LoRa initialized")
    except Exception as e:
        print(f"  LoRa init failed: {e}")
        print("  Continuing without LoRa (BLE scan only mode)")
        lora = None
    
    print("\nStarting main loop...\n")
    
    # Main loop
    while True:
        # Scan for sensor advertisements
        sensors = scan_for_sensors(ble, CONFIG['ble_scan_duration_sec'])
        
        # Update cache with new readings
        for machine_id, reading in sensors.items():
            sensor_cache[machine_id] = reading
        
        # Count active sensors (received in last 2 minutes)
        now = time.monotonic()
        active_sensors = {
            mid: r for mid, r in sensor_cache.items() 
            if now - r.timestamp < 120
        }
        
        print(f"\nActive sensors: {len(active_sensors)}")
        
        # Send data via LoRa if we have sensors and LoRa is available
        if active_sensors and lora:
            packet = build_lora_packet(CONFIG['aggregator_id'], active_sensors)
            print(f"Sending LoRa packet: {packet.hex()}")
            lora.send(packet)
        
        # Wait before next scan cycle
        # If immediate forwarding (lora_tx_interval_sec=0), continue immediately
        if CONFIG['lora_tx_interval_sec'] > 0:
            print(f"Waiting {CONFIG['lora_tx_interval_sec']}s...")
            time.sleep(CONFIG['lora_tx_interval_sec'])
        else:
            # Small delay between scan cycles
            time.sleep(0.5)


# Run main
if __name__ == "__main__":
    main()
