# FINAL Robust Washing Machine Sensor Node - CircuitPython
# Hardware: Seeed XIAO nRF52840 Sense + LSM6DS3
# This version uses _bleio for ultra-reliable Bluetooth communication.
import time
import json
import math
import struct
import alarm
import board
import busio
import digitalio
import _bleio
from adafruit_lsm6ds.lsm6ds33 import LSM6DS33
from adafruit_lsm6ds import Rate, AccelRange
#import analogio

class LSM6DS3TRC(LSM6DS33):
    CHIP_ID = 0x6A # compatibility fix to make the generic LSM6DS33 driver work with the specific sensor found on your hardware.

# ============================================================================
# Configuration
# ============================================================================

TEST_MODE = True  # Set to False for battery operation (Deep Sleep)
SEND_DEBUG = True  # Enable debug print statements

def load_config():
    with open("/config.json", "r") as f:
        return json.load(f)
CONFIG = load_config()

imu_pwr = digitalio.DigitalInOut(board.IMU_PWR)
imu_pwr.direction = digitalio.Direction.OUTPUT
imu_pwr.value = True

# ============================================================================
# Battery Monitoring => to be done
# ============================================================================

def get_battery_percent():
    """
    Estimate battery percentage for XIAO nRF52840 Sense.
    Uses VBAT_ENABLE to control bridge and VBAT_READ for ADC.
    """
    try:
        # Circuitpython on does not supprt reading supply voltage / analog reference voltage
        return 100
    except Exception as e:
        print(f"Battery Read Failed: {e}")
        return 100

# ============================================================================
# Accelerometer Functions
# ============================================================================

def init_accelerometer():
    try:
        i2c = busio.I2C(board.IMU_SCL, board.IMU_SDA)
        sensor = LSM6DS3TRC(i2c)
        sensor.accelerometer_data_rate = Rate.RATE_104_HZ
        sensor.accelerometer_range = AccelRange.RANGE_2G # Set range to ±2g. We need to decide based on low vibration levels. 
        return sensor
    except Exception as e:
        print(f"Sensor Init Failed: {e}")
        return None

def collect_samples(sensor):
    if not sensor: 
        return []
    
    num_samples = int(CONFIG['sample_duration_sec'] * CONFIG['sample_rate_hz'])
    sample_interval = 1.0 / CONFIG['sample_rate_hz']
    
    samples = []
    for _ in range(num_samples):
        samples.append(sensor.acceleration)
        time.sleep(sample_interval)
    
    return samples

# ============================================================================
# Signal Processing
# ============================================================================

def calculate_magnitude(samples):
    """Calculate magnitude of acceleration for each sample"""
    magnitudes = []
    for x, y, z in samples:
        mag = math.sqrt(x*x + y*y + z*z)
        magnitudes.append(mag)
    return magnitudes

def calculate_rms(magnitudes):
    """Calculate RMS of magnitude values"""
    if not magnitudes:
        return 0.0
    
    sum_squares = sum(m * m for m in magnitudes)
    return math.sqrt(sum_squares / len(magnitudes))

def remove_dc_offset(magnitudes):
    """Remove DC offset (gravity) from signal"""
    if not magnitudes:
        return magnitudes
    
    mean = sum(magnitudes) / len(magnitudes)
    return [m - mean for m in magnitudes]

# FFT disabled to save battery - only RMS threshold check is used

# ============================================================================
# BLE Advertising
# ============================================================================
# Advertising Packet Structure (BLE 4.0 Format):
# 
# 1. Flags (3 bytes):
#    - 0x02 0x01 0x06 = General Discoverable, BR/EDR Not Supported
#
# 2. Device Name (variable length):
#    - Length byte (name_length + 1)
#    - 0x08 = Type (Shortened Local Name)
#    - Name bytes (e.g., "WMS")
#
# 3. Manufacturer Specific Data:
#    - Length byte (payload_length + 3)
#    - 0xFF = Type (Manufacturer Specific Data)
#    - Company ID (2 bytes, little-endian)
#    - Custom Payload (8 bytes):
#      ┌─────────────────┬──────┬────────┬─────────────────────────────┐
#      │ Field           │ Size │ Type   │ Description                 │
#      ├─────────────────┼──────┼────────┼─────────────────────────────┤
#      │ Protocol Ver    │ 1 B  │ uint8  │ Protocol version            │
#      │ Machine Type    │ 1 B  │ uint8  │ Machine type identifier     │
#      │ Machine ID      │ 1 B  │ uint8  │ Unique machine ID           │
#      │ RMS Value       │ 2 B  │ uint16 │ RMS × 1000 (little-endian)  │
#      │ Frequency       │ 2 B  │ uint16 │ Spare (currently unused)    │
#      │ Battery %       │ 1 B  │ uint8  │ Battery percentage (0-100)  │
#      └─────────────────┴──────┴────────┴─────────────────────────────┘
#      Total: 8 bytes
# ============================================================================

def broadcast_data(rms, battery):
    adapter = _bleio.adapter
    adapter.enabled = True
    adapter.stop_advertising()
        
    name = b"WMS"
    
    # Pack custom payload (8 bytes, little-endian)
    payload = struct.pack(
        "<BBBHHB",
        CONFIG['protocol_version'],  # 1 byte: Protocol version
        CONFIG['machine_type'],       # 1 byte: Machine type
        CONFIG['machine_id'],         # 1 byte: Machine ID
        int(rms * 1000),              # 2 bytes: acceleration RMS in mm/s2 
        0,                           # 2 bytes: Frequency (spare)
        battery                      # 1 byte: Battery %
    )
    
    # Construct complete advertising packet
    adv_data = (
        b"\x02\x01\x06" +                    # Flags (General Discoverable)
        bytes([len(name) + 1, 0x08]) + name + # Short Name: "WMS"
        bytes([len(payload) + 3, 0xFF]) +     # Manufacturer Data header
        struct.pack("<H", CONFIG['company_id']) +  # Company ID (2 bytes)
        payload                               # Custom payload (8 bytes)
    )
    if CONFIG.get('print_debug', False):
        print(f"📡 Broadcasting: RMS {rms:.3f}, Battery {battery}%")
    adapter.start_advertising(
        adv_data,
        connectable=False,
        interval=0.1,
        tx_power=CONFIG.get('tx_power', 0) # in dBm, -40 to +8 in steps of 4 for nRF52840
    )

# ============================================================================
# Main Logic
# ============================================================================
print("WM Sensor Node Starting...")
sensor = init_accelerometer()

while True:
    # Measure battery in every cycle
    battery = get_battery_percent()
    print(f"Current Battery: {battery}%")
    
    print("--- Taking measurement ---")
    samples = collect_samples(sensor)
    imu_pwr.value = False  # Power down sensor to save battery
    if samples:
        magnitudes = calculate_magnitude(samples)
        ac_mags = remove_dc_offset(magnitudes)
        rms = calculate_rms(ac_mags)
        broadcast_data(rms, battery=battery)
        
        # Advertise for 1 second before stopping
        time.sleep(CONFIG.get('advertise_interval_sec', 1))

    # In test mode, keep broadcasting every cycle
    if CONFIG.get('test_mode', False):
        time.sleep(1)
    # not in test mode, go to deep sleep. After wakeup, the code will restart from the beginning
    else:
        time_alarm = alarm.time.TimeAlarm(
            monotonic_time=time.monotonic() + CONFIG['wake_interval_sec']
        )
        alarm.exit_and_deep_sleep_until_alarms(time_alarm)
                
