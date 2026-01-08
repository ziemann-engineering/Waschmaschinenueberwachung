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
import binascii

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
    def __init__(self, machine_type, machine_id, rms_x100, mean_x100, freq_x10, battery_percent):
        self.machine_type = machine_type  # 1=washer, 2=dryer
        self.machine_id = machine_id
        self.rms_x100 = rms_x100
        self.mean_x100 = mean_x100
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
    found_sensors = {}
    TARGET_ID = 0xFFFF
    
    if not BLE_AVAILABLE or ble is None:
        return found_sensors
        
    print(f"Scanning for MFR Data {hex(TARGET_ID)} (timeout={duration_sec}s)...")
    
    scan_count = 0
    try:
        for advertisement in ble.start_scan(timeout=duration_sec):
            scan_count += 1
            
            reading = parse_mfr_data(advertisement, TARGET_ID)
            if reading:
                key = (reading.machine_type, reading.machine_id)
                found_sensors[key] = reading
           
    finally:
        ble.stop_scan()
        if found_sensors:
            print(f"Scan complete. Found {len(found_sensors)} unique sensors ({scan_count} packets).")
            for reading in found_sensors.values():
                status = "🔴" if reading.rms > 0.5 else "⚪"
                bar = '█' * min(20, int(reading.rms * 10))
                print(f"  {status} Machine {reading.machine_id}: RMS {reading.rms:.3f} | Mean {reading.mean:.2f} | Batt {reading.battery_percent}% | {bar}")
    
    return found_sensors

# ============================================================================
# LoRa Packet Building
# ============================================================================

def build_lora_packet(readings):
    """
    Build LoRa packet from sensor readings.
    
    Packet format (Protocol v2):
    - 4 bytes: Waveshare address header (0x00 0x00 for broadcast + 2 channel bytes)
    - Byte 0: Aggregator ID
    - Byte 1: Machine count (N)
    - N × 9 bytes: Machine data
      - Byte 0: Machine type (1=washer, 2=dryer)
      - Byte 1: Machine ID
      - Bytes 2-3: RMS × 100 (uint16, little-endian)
      - Bytes 4-5: Mean × 100 (uint16, little-endian)
      - Bytes 6-7: Freq × 10 (uint16, little-endian)  
      - Byte 8: Battery %
    
    Returns bytes
    """
    aggregator_id = CONFIG.get("aggregator_id", 1)
    
    # Start with Waveshare header (4 bytes: address + channel)
    # Using broadcast address 0x00 0x00 and default channel bytes
    packet = bytearray([0x00, 0x00, 0x00, 0x00])
    
    # Aggregator ID and machine count
    packet.append(aggregator_id)
    packet.append(len(readings))
    
    # Add each machine's data
    for (machine_type, machine_id), reading in readings.items():
        packet.append(reading.machine_type)
        packet.append(reading.machine_id)
        packet.extend(struct.pack('<H', reading.rms_x100))
        packet.extend(struct.pack('<H', reading.mean_x100))
        packet.extend(struct.pack('<H', reading.freq_x10))
        packet.append(reading.battery_percent)
    
    # Calculate CRC-32 of the packet data
    crc = binascii.crc32(bytes(packet))
    packet.extend(struct.pack('<I', crc))
    
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
                # Update cache
                for key, reading in new_readings.items():
                    sensor_cache[key] = reading

                # Remove stale readings older than 60s
                current_time = time.monotonic()
                stale_keys = [k for k, v in sensor_cache.items() if current_time - v.timestamp > 60]
                for k in stale_keys:
                    del sensor_cache[k]

            
            # Check if it's time to transmit
            current_time = time.monotonic()
            if sensor_cache and (current_time - last_tx_time >= tx_interval):
                # Build and send packet
                packet = build_lora_packet(sensor_cache)
                
                print(f"\nTransmitting {len(sensor_cache)} readings via LoRa...")
                led.value = True
                sx.send(packet)
                led.value = False
                
                print(f"Sent {len(packet)} bytes: {packet.hex()}")
                last_tx_time = current_time
                
                blink_led(1, 0.05)  # Short blink for TX
            
            # If no BLE, send test packet periodically
            if not BLE_AVAILABLE or not ble:
                if current_time - last_tx_time >= tx_interval:
                    # Send test packet
                    test_packet = bytearray([0x00, 0x00, 0x00, 0x00])  # Waveshare header
                    test_packet.append(CONFIG.get("aggregator_id", 1))  # Aggregator ID
                    test_packet.append(0)  # 0 machines (heartbeat)
                    
                    print("Sending heartbeat...")
                    led.value = True
                    sx.send(bytes(test_packet))
                    led.value = False
                    
                    last_tx_time = current_time
                    blink_led(1, 0.05)
            
            # Small delay between iterations
            time.sleep(0.1)
            
        except Exception as e:
            print(f"Error in main loop: {e}")
            blink_led(5, 0.1, 0.1)  # Error indication
            time.sleep(1)


def parse_mfr_data(advertisement, target_company_id=0xFFFF):
    """
    Parse the manufacturer data from a BLE advertisement.
    Supports both v1 (6 bytes) and v2 (8 bytes) packets.
    """
    mfr = getattr(advertisement, "manufacturer_data", None)
    if not mfr:
        # Aggregator library fallback: Check raw data_dict for 0xFF (255)
        raw_mfr = getattr(advertisement, "data_dict", {}).get(255)
        if raw_mfr and len(raw_mfr) >= 2:
            cid = struct.unpack("<H", raw_mfr[:2])[0]
            mfr = {cid: raw_mfr[2:]}
            
    if not mfr:
        return None

    data = mfr.get(target_company_id, None)
    if not data:
        return None  # No matching company ID

    try:

        protocol_version = data[0]
        machine_type     = data[1]
        machine_id       = data[2]

        # Handle v1 packets (6 bytes)
        if len(data) == 6:
            rms_x100 = data[3] | (data[4] << 8)
            mean_x100 = 981  # Default 9.81
            freq_x10 = data[5]
            battery  = 100  # Default battery for v1
        # Handle v2 packets (8 bytes - no mean)
        elif len(data) == 8:
            rms_x100 = data[3] | (data[4] << 8)
            mean_x100 = 981  # Default 9.81
            freq_x10 = data[5] | (data[6] << 8)
            battery  = data[7]
        # Handle v2 packets (10 bytes - with mean)
        elif len(data) >= 10:
            rms_x100 = data[3] | (data[4] << 8)
            mean_x100 = data[5] | (data[6] << 8)
            freq_x10 = data[7] | (data[8] << 8)
            battery  = data[9]
        else:
            return None  # Too short

        rms = rms_x100 / 100.0
        mean = mean_x100 / 100.0

        reading = SensorReading(
            machine_type=machine_type,
            machine_id=machine_id,
            rms_x100=rms_x100,
            mean_x100=mean_x100,
            freq_x10=freq_x10,
            battery_percent=battery
        )
        reading.rms = rms
        reading.mean = mean

        return reading

    except Exception as e:
        print(f"Failed to parse manufacturer data: {e}")
        return None


# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    main()
