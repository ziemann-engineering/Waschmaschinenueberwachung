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
    def __init__(self, sensor_id, assignment_active, rms_x1000, freq_x10, battery_voltage, rssi):
        self.sensor_id = sensor_id
        self.assignment_active = assignment_active
        self.rms_x1000 = rms_x1000
        self.freq_x10 = freq_x10
        self.battery_voltage = battery_voltage
        self.rssi = rssi  # BLE RSSI in dBm
        self.timestamp = time.monotonic()
    
    def __repr__(self):
        voltage = 1.0 + self.battery_voltage / 100
        return f"Sensor {self.sensor_id.hex().upper()}: RMS={self.rms_x1000/1000:.3f}, Freq={self.freq_x10/10:.1f}Hz, Batt={voltage:.2f}V, RSSI={self.rssi}dBm"

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
        print("BLE not available, skipping scan")
        return found_sensors
        
    print(f"Scanning for MFR Data {hex(TARGET_ID)} (timeout={duration_sec}s)...")
    
    scan_count = 0
    try:
        for advertisement in ble.start_scan(timeout=duration_sec, minimum_rssi=-100, buffer_size=1024):
            scan_count += 1
            
            reading = parse_mfr_data(advertisement, TARGET_ID)
            if reading:
                found_sensors[reading.sensor_id] = reading
           
    finally:
        ble.stop_scan()
        if found_sensors:
            print(f"Scan complete. Found {len(found_sensors)} unique sensors ({scan_count} packets).")
            for reading in found_sensors.values():
                voltage = 1.0 + reading.battery_voltage / 100
                print(f"Sensor {reading.sensor_id.hex().upper()}: RMS {reading.rms_x1000 / 1000:.3f} | Batt {voltage:.2f}V | RSSI {reading.rssi}dBm")
    
    return found_sensors

# ============================================================================
# LoRa Packet Building
# ============================================================================

def build_lora_packet(readings):
    """
    Build LoRa packet from sensor readings.
    
    Packet format (Protocol v4):
    - 4 bytes: Waveshare address header (0x00 0x00 for broadcast + 2 channel bytes)
    - Byte 0: Aggregator name length (L)
    - L bytes: Aggregator name (UTF-8)
    - Next byte: Machine count (N)
        - N × 13 bytes: Sensor data
            - Bytes 0-5: Static BLE address
            - Byte 6: Flags (bit 0 = assignment active)
            - Bytes 7-8: RMS × 1000 (uint16, little-endian)
            - Bytes 9-10: Freq × 10 (uint16, little-endian)
            - Byte 11: Battery voltage (1.00 V + value × 10 mV)
            - Byte 12: RSSI (int8, signed dBm)
    
    Returns bytes
    """
    aggregator_name = CONFIG.get("aggregator_name", "").strip()
    encoded_name = aggregator_name.encode("utf-8")
    if not encoded_name or len(encoded_name) > 32:
        raise ValueError("aggregator_name must encode to 1-32 bytes")
    
    # Start with Waveshare header (4 bytes: address + channel)
    # Using broadcast address 0x00 0x00 and default channel bytes
    packet = bytearray([0x00, 0x00, 0x00, 0x00])
    
    # Aggregator name and machine count
    packet.append(len(encoded_name))
    packet.extend(encoded_name)
    packet.append(len(readings))
    
    # Add each machine's data
    for reading in readings.values():
        packet.extend(reading.sensor_id)
        packet.append(0x01 if reading.assignment_active else 0x00)
        packet.extend(struct.pack('<H', reading.rms_x1000))
        packet.extend(struct.pack('<H', reading.freq_x10))
        packet.append(reading.battery_voltage)
        packet.extend(struct.pack('b', reading.rssi))  # signed int8
    
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
    
    scan_duration = CONFIG.get("ble_scan_duration_sec", 5)  # Short scan bursts
    tx_interval = CONFIG.get("lora_tx_interval_sec", 60)  # How often to transmit
    
    print("Starting main loop...")
    print(f"BLE scan bursts: {scan_duration}s, LoRa TX interval: {tx_interval}s")
    blink_led(2)  # Ready indication
    
    last_tx_time = 0
    sensor_cache = {}  # Accumulate readings between transmissions
    
    while True:
        try:
            # Continuous BLE scanning in short bursts
            if BLE_AVAILABLE and ble:
                new_readings = scan_for_sensors(ble, scan_duration)
                
                # Update cache with new readings
                for key, reading in new_readings.items():
                    sensor_cache[key] = reading
                
                # Remove stale readings (older than tx_interval * 2)
                current_time = time.monotonic()
                stale_threshold = tx_interval * 2
                stale_keys = [k for k, v in sensor_cache.items() 
                             if current_time - v.timestamp > stale_threshold]
                for k in stale_keys:
                    del sensor_cache[k]
                
                # Check if it's time to transmit
                if current_time - last_tx_time >= tx_interval:
                    if sensor_cache:
                        packet = build_lora_packet(sensor_cache)
                        print(f"\nTransmitting {len(sensor_cache)} readings via LoRa...")
                    else:
                        packet = build_lora_packet({})
                        print("\nTransmitting keepalive (no sensors found)...")
                    
                    # Send the packet
                    led.value = True
                    sx.send(packet)
                    led.value = False
                    
                    print(f"Sent {len(packet)} bytes: {packet.hex()}")
                    last_tx_time = current_time
                    blink_led(1, 0.05)  # Short blink for TX
                
                # Brief pause before next scan burst
                time.sleep(0.1)
            
            # If no BLE, send keepalive periodically
            else:
                current_time = time.monotonic()
                if current_time - last_tx_time >= tx_interval:
                    print("Sending keepalive (BLE not available)...")
                    led.value = True
                    sx.send(build_lora_packet({}))
                    led.value = False
                    
                    last_tx_time = current_time
                    blink_led(1, 0.05)
                
                # Wait before checking again
                time.sleep(1)
            
        except Exception as e:
            print(f"Error in main loop: {e}")
            blink_led(5, 0.1, 0.1)  # Error indication
            time.sleep(1)


def parse_mfr_data(advertisement, target_company_id=0xFFFF):
    """
    Parse the manufacturer data from a BLE advertisement.
    Parses the v3 sensor packet with a transient assignment flag.
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
        # Extract RSSI from advertisement
        rssi = getattr(advertisement, "rssi", -128)  # Default to -128 if not available

        protocol_version = data[0]
        if protocol_version != CONFIG.get("protocol_version", 2):
            print(f"Ignoring sensor protocol v{protocol_version}; expected v{CONFIG.get('protocol_version', 2)}")
            return None

        if len(data) == 7:
            sensor_id = bytes(advertisement.address.address_bytes)
            if len(sensor_id) != 6:
                print(f"Ignoring sensor with {len(sensor_id)}-byte BLE address")
                return None
            assignment_active = bool(data[1] & 0x01)
            rms_x1000 = data[2] | (data[3] << 8)
            freq_x10 = data[4] | (data[5] << 8)
            battery_voltage = data[6]
        else:
            print(f"Ignoring v3 sensor payload with {len(data)} bytes; expected 7")
            return None

        reading = SensorReading(
            sensor_id=sensor_id,
            assignment_active=assignment_active,
            rms_x1000=rms_x1000,
            freq_x10=freq_x10,
            battery_voltage=battery_voltage,
            rssi=rssi
        )

        return reading

    except Exception as e:
        print(f"Failed to parse manufacturer data: {e}")
        return None


# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    main()
