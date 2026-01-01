# Washing Machine Aggregator - CircuitPython
# Hardware: Seeed XIAO ESP32S3 with SX1262 LoRa module via board-to-board connector
# Flashed with CircuitPython for Xiao S3 Sense
#
# This aggregator:
# 1. Scans for BLE advertisements from sensor nodes
# 2. Parses vibration data from manufacturer-specific data
# 3. Forwards data via LoRa (SX1262) to the central server

import time
import json
import struct
import board
import microcontroller
import digitalio

# SX1262 LoRa module
from sx1262 import SX1262

# BLE imports
try:
    from adafruit_ble import BLERadio
    from adafruit_ble.advertising import Advertisement
    BLE_AVAILABLE = True
except ImportError:
    print("BLE not available - running in test mode")
    BLE_AVAILABLE = False

# ============================================================================
# Hardware Configuration for XIAO S3 with SX1262
# ============================================================================

# LED indicator
led = digitalio.DigitalInOut(microcontroller.pin.GPIO21)
led.direction = digitalio.Direction.OUTPUT

# TX/RX mode control for LoRa module (if needed by your hardware)
TX_MODE = digitalio.DigitalInOut(microcontroller.pin.GPIO38)
TX_MODE.direction = digitalio.Direction.OUTPUT
TX_MODE.value = True  # Set to TX mode

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
# SX1262 LoRa Module
# ============================================================================

def init_lora():
    """Initialize SX1262 LoRa module"""
    print("Initializing SX1262 LoRa module...")
    
    lora_config = CONFIG.get("lora", {})
    
    sx = SX1262(
        spi_bus=1, 
        clk=board.SCK, 
        mosi=board.MOSI, 
        miso=board.MISO, 
        cs=microcontroller.pin.GPIO41, 
        irq=microcontroller.pin.GPIO39, 
        rst=microcontroller.pin.GPIO42, 
        gpio=microcontroller.pin.GPIO40
    )
    
    # Configure LoRa parameters
    sx.begin(
        freq=lora_config.get("frequency", 868),
        bw=lora_config.get("bandwidth", 125.0),
        sf=lora_config.get("spreading_factor", 7),
        cr=lora_config.get("coding_rate", 5),
        syncWord=lora_config.get("sync_word", 0x12),
        power=lora_config.get("power", -5),
        currentLimit=60.0,
        preambleLength=8,
        implicit=False,
        implicitLen=0xFF,
        crcOn=True,
        txIq=False,
        rxIq=False,
        tcxoVoltage=lora_config.get("tcxo_voltage", 1.7),
        useRegulatorLDO=False,
        blocking=True
    )
    
    print("SX1262 LoRa module initialized")
    return sx

# ============================================================================
# BLE Scanner
# ============================================================================

def scan_for_sensors(ble, duration_sec):
    """
    Scan for BLE advertisements from washing machine sensors.
    
    Returns dict of machine_id -> SensorReading
    """
    found_sensors = {}
    
    if not BLE_AVAILABLE or ble is None:
        return found_sensors
    
    print(f"Scanning BLE for {duration_sec}s...")
    
    for advertisement in ble.start_scan(timeout=duration_sec, minimum_rssi=-90):
        # Check for manufacturer data
        if not hasattr(advertisement, 'manufacturer_data') or not advertisement.manufacturer_data:
            continue
        
        # Look for our company ID
        company_id = CONFIG.get("company_id", 0xFFFF)
        if company_id not in advertisement.manufacturer_data:
            continue
        
        mfr_data = advertisement.manufacturer_data[company_id]
        
        # Parse sensor data
        # Protocol v2: [version, type, machine_id, rms_hi, rms_lo, freq_hi, freq_lo, battery]
        if len(mfr_data) >= 8 and mfr_data[0] == 2:  # Protocol version 2
            machine_type = mfr_data[1]
            machine_id = mfr_data[2]
            rms_x100 = (mfr_data[3] << 8) | mfr_data[4]
            freq_x10 = (mfr_data[5] << 8) | mfr_data[6]
            battery = mfr_data[7]
            
            reading = SensorReading(machine_type, machine_id, rms_x100, freq_x10, battery)
            found_sensors[(machine_type, machine_id)] = reading
            print(f"  Found: {reading}")
        
        # Protocol v1 (legacy): [version, machine_id, rms_hi, rms_lo, freq_hi, freq_lo, battery]
        elif len(mfr_data) >= 7 and mfr_data[0] == 1:
            machine_id = mfr_data[1]
            rms_x100 = (mfr_data[2] << 8) | mfr_data[3]
            freq_x10 = (mfr_data[4] << 8) | mfr_data[5]
            battery = mfr_data[6]
            
            # Default to washer for v1
            reading = SensorReading(1, machine_id, rms_x100, freq_x10, battery)
            found_sensors[(1, machine_id)] = reading
            print(f"  Found (v1): {reading}")
    
    ble.stop_scan()
    return found_sensors

# ============================================================================
# LoRa Packet Building
# ============================================================================

def build_lora_packet(readings):
    """
    Build LoRa packet from sensor readings.
    
    Packet format (Protocol v2):
    - Byte 0: Aggregator ID
    - Byte 1: Machine count (N)
    - N × 7 bytes: Machine data
      - Byte 0: Machine type (1=washer, 2=dryer)
      - Byte 1: Machine ID
      - Bytes 2-3: RMS × 100 (uint16, little-endian)
      - Bytes 4-5: Freq × 10 (uint16, little-endian)  
      - Byte 6: Battery %
    
    Returns bytes
    """
    aggregator_id = CONFIG.get("aggregator_id", 1)
    
    # Start packet with aggregator ID and machine count
    packet = bytearray()
    packet.append(aggregator_id)
    packet.append(len(readings))
    
    # Add each machine's data
    for (machine_type, machine_id), reading in readings.items():
        packet.append(reading.machine_type)
        packet.append(reading.machine_id)
        packet.extend(struct.pack('<H', reading.rms_x100))
        packet.extend(struct.pack('<H', reading.freq_x10))
        packet.append(reading.battery_percent)
    
    return bytes(packet)

# ============================================================================
# LED Indication
# ============================================================================

def blink_led(times=1, on_time=0.1, off_time=0.1):
    """Blink LED for status indication"""
    for _ in range(times):
        led.value = True
        time.sleep(on_time)
        led.value = False
        time.sleep(off_time)

# ============================================================================
# Main Loop
# ============================================================================

def main():
    print("=" * 50)
    print("Washing Machine Aggregator")
    print(f"ID: {CONFIG.get('aggregator_id', 1)}")
    print(f"Name: {CONFIG.get('aggregator_name', 'Unknown')}")
    print("=" * 50)
    
    # Initialize LoRa
    try:
        sx = init_lora()
    except Exception as e:
        print(f"Failed to initialize LoRa: {e}")
        # Blink error pattern
        while True:
            blink_led(3, 0.2, 0.2)
            time.sleep(1)
    
    # Initialize BLE
    ble = None
    if BLE_AVAILABLE:
        try:
            ble = BLERadio()
            print("BLE initialized")
        except Exception as e:
            print(f"BLE init failed: {e}")
    
    scan_duration = CONFIG.get("ble_scan_duration_sec", 5)
    tx_interval = CONFIG.get("lora_tx_interval_sec", 5)
    last_tx_time = 0
    
    print("Starting main loop...")
    blink_led(2)  # Ready indication
    
    while True:
        try:
            # Scan for BLE sensors
            if BLE_AVAILABLE and ble:
                new_readings = scan_for_sensors(ble, scan_duration)
                
                # Update cache with new readings
                for key, reading in new_readings.items():
                    sensor_cache[key] = reading
                
                # Remove stale readings (older than 60 seconds)
                current_time = time.monotonic()
                stale_keys = [k for k, v in sensor_cache.items() 
                             if current_time - v.timestamp > 60]
                for key in stale_keys:
                    del sensor_cache[key]
            
            # Check if it's time to transmit
            current_time = time.monotonic()
            if current_time - last_tx_time >= tx_interval:
                if sensor_cache:
                    # Build and send packet with sensor data
                    packet = build_lora_packet(sensor_cache)
                    
                    print(f"\nTransmitting {len(sensor_cache)} readings via LoRa...")
                    led.value = True
                    sx.send(packet)
                    led.value = False
                    
                    print(f"Sent {len(packet)} bytes: {packet.hex()}")
                else:
                    # Send heartbeat packet (no sensors found)
                    test_packet = bytearray()
                    test_packet.append(CONFIG.get("aggregator_id", 1))  # Aggregator ID
                    test_packet.append(0)  # 0 machines (heartbeat)
                    
                    print("Sending heartbeat (no sensors found)...")
                    led.value = True
                    sx.send(bytes(test_packet))
                    led.value = False
                    
                    print(f"Sent heartbeat: {test_packet.hex()}")
                
                last_tx_time = current_time
                blink_led(1, 0.05)  # Short blink for TX
            
            # Small delay between iterations
            time.sleep(0.1)
            
        except Exception as e:
            print(f"Error in main loop: {e}")
            blink_led(5, 0.1, 0.1)  # Error indication
            time.sleep(1)

# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    main()
