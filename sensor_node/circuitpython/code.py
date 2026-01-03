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
import analogio
class LSM6DS3TRC(LSM6DS33):
    CHIP_ID = 0x6A # compatibility fix to make the generic LSM6DS33 driver work with the specific sensor found on your hardware.

# ============================================================================
# Configuration
# ============================================================================

TEST_MODE = True  # Set to False for battery operation (Deep Sleep)
def load_config():
    try:
        with open("/config.json", "r") as f:
            return json.load(f)
    except Exception:
        return {
            "machine_id": 1,
            "machine_type": 1, 
            "wake_interval_sec": 180,
            "sample_duration_sec": 0.2,
            "sample_rate_hz": 100,
            "company_id": 0xFFFF,
            "protocol_version": 2
        }
CONFIG = load_config()

# ============================================================================
# Battery Monitoring => to be done
# ============================================================================

def get_battery_percent():
    """
    Estimate battery percentage for XIAO nRF52840 Sense.
    Uses VBAT_ENABLE to control bridge and VBAT_READ for ADC.
    """
    try:
        # Enable battery voltage divider bridge (P0.14)
        # Using raw pin identifiers to be most compatible with all XIAO versions
        vbatt_enable = digitalio.DigitalInOut(board.P0_14)
        vbatt_enable.direction = digitalio.Direction.OUTPUT
        vbatt_enable.value = False # LOW to enable
        
        # Read voltage from P0.31
        vbatt_adc = analogio.AnalogIn(board.P0_31)
        # Standard calculation for XIAO nRF52840 bridge (1M/1M divider)
        # reference_voltage is usually 3.3V on this board
        voltage = (vbatt_adc.value * vbatt_adc.reference_voltage / 65535) * 2
        
        # Cleanup pins to save power
        vbatt_enable.value = True # HIGH to disable bridge
        vbatt_enable.deinit()
        vbatt_adc.deinit()
        
        # Map voltage: 3.0V (100%) to 2.0V (0%) for CR2032 as per user comment
        # Note: If using LiPo, 4.2V is 100%, 3.2V is 0%
        if voltage > 3.0: # Likely LiPo or very fresh CR2032
            percent = int((voltage - 3.2) / (4.2 - 3.2) * 100)
        else:
            percent = int((voltage - 2.0) / (3.0 - 2.0) * 100)
            
        return max(0, min(100, percent))
    except Exception as e:
        print(f"Battery Read Failed: {e}")
        return 100

# ============================================================================
# Accelerometer Functions
# ============================================================================

def init_accelerometer():
    imu_pwr = digitalio.DigitalInOut(board.IMU_PWR)
    imu_pwr.direction = digitalio.Direction.OUTPUT
    imu_pwr.value = True
    time.sleep(0.1)
    try:
        i2c = busio.I2C(board.IMU_SCL, board.IMU_SDA)
        sensor = LSM6DS3TRC(i2c)
        sensor.accelerometer_data_rate = Rate.RATE_104_HZ
        sensor.accelerometer_range = AccelRange.RANGE_4G
        return sensor
    except Exception as e:
        print(f"Sensor Init Failed: {e}")
        return None

def collect_samples(sensor):
    if not sensor: return []
    
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
# BLE 
# ============================================================================

def broadcast_data(rms, mean, battery=100):
    adapter = _bleio.adapter
    adapter.enabled = True
    adapter.stop_advertising()
    name = b"WM-FINAL"
    payload = struct.pack(
        "<BBBHHHB",
        CONFIG['protocol_version'],
        CONFIG['machine_type'],
        CONFIG['machine_id'],
        int(rms * 100),
        int(mean * 100),
        0, # Frequency spare
        battery
    )
    
    # Construct raw packet
    adv_data = (
        b"\x02\x01\x06" +                    # Flags
        bytes([len(name) + 1, 0x08]) + name + # Short Name
        bytes([len(payload) + 3, 0xFF]) +     # MFR Data Type
        struct.pack("<H", CONFIG['company_id']) + 
        payload
    )
    print(f"📡 Sending: RMS {rms:.3f} | Mean {mean:.3f}")
    adapter.start_advertising(
        adv_data,
        connectable=False,
        interval=0.1
    )

# ============================================================================
# Main Logic
# ============================================================================

def main():
    print("WM Sensor Node Starting...")
    sensor = init_accelerometer()
    
    while True:
        # Measure battery in every cycle
        battery = get_battery_percent()
        print(f"Current Battery: {battery}%")
        
        if TEST_MODE:
            # Continuous broadcast mode
            samples = collect_samples(sensor)
            if samples:
                magnitudes = calculate_magnitude(samples)
                mean = sum(magnitudes) / len(magnitudes)
                ac_mags = remove_dc_offset(magnitudes)
                rms = calculate_rms(ac_mags)
                broadcast_data(rms, mean, battery=battery)
            time.sleep(0.1) # Small delay for stability
        else:
            # Intermittent battery-saving mode
            print("--- Sleeping for 5s ---")
            _bleio.adapter.stop_advertising()
            time.sleep(5)
            
            print("--- Taking measurement ---")
            samples = collect_samples(sensor)
            if samples:
                magnitudes = calculate_magnitude(samples)
                mean = sum(magnitudes) / len(magnitudes)
                ac_mags = remove_dc_offset(magnitudes)
                rms = calculate_rms(ac_mags)
                broadcast_data(rms, mean, battery=battery)
                
                # Advertise for 1 second before stopping
                time.sleep(1)
                
if __name__ == "__main__":
    main()
