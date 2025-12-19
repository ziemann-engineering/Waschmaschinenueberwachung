# Washing Machine Sensor Node - CircuitPython
# Hardware: Seeed XIAO nRF52840 Sense + LSM6DS3

import time
import json
import math
import struct
import alarm
import board
import busio
import microcontroller
from adafruit_lsm6ds.lsm6ds3 import LSM6DS3
from _bleio import adapter
import adafruit_ble
from adafruit_ble.advertising import Advertisement

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
        # Default configuration (optimized for battery life)
        return {
            "machine_id": 1,
            "wake_interval_sec": 180,      # 3 minutes
            "sample_duration_sec": 0.2,    # 200ms
            "sample_rate_hz": 100,
            "company_id": 0xFFFF,
            "protocol_version": 1
        }

CONFIG = load_config()

# ============================================================================
# Battery Monitoring
# ============================================================================

def get_battery_percent():
    """
    Estimate battery percentage from voltage.
    CR2032: 3.0V full, 2.0V empty (cutoff ~2.0V)
    Note: XIAO nRF52840 may need analog pin setup for battery reading
    """
    # TODO: Implement actual battery voltage reading
    # For now, return a placeholder
    # On XIAO nRF52840, you may need to read from a specific pin
    # or use the built-in battery monitoring if available
    return 100  # Placeholder

# ============================================================================
# Accelerometer Functions
# ============================================================================

def init_accelerometer():
    """Initialize the LSM6DS3 accelerometer"""
    i2c = busio.I2C(board.IMU_SCL, board.IMU_SDA)
    sensor = LSM6DS3(i2c)
    
    # Configure for our use case
    # ODR (Output Data Rate) options: 12.5, 26, 52, 104, 208, 416, 833, 1666, 3333, 6666 Hz
    # We want 100 Hz, so use 104 Hz
    sensor.accelerometer_data_rate = 104  # Closest to 100 Hz
    
    # Range: 4g is sufficient for vibration detection
    sensor.accelerometer_range = 4  # ±4g
    
    return sensor

def collect_samples(sensor, duration_sec, sample_rate_hz):
    """
    Collect accelerometer samples for specified duration.
    Returns list of (x, y, z) tuples in m/s²
    """
    samples = []
    num_samples = int(duration_sec * sample_rate_hz)
    sample_interval = 1.0 / sample_rate_hz
    
    start_time = time.monotonic()
    next_sample_time = start_time
    
    for i in range(num_samples):
        # Wait for next sample time
        while time.monotonic() < next_sample_time:
            pass
        
        # Read accelerometer (returns m/s²)
        accel = sensor.acceleration
        samples.append(accel)
        
        next_sample_time += sample_interval
    
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

class WashingMachineAdvertisement(Advertisement):
    """Custom BLE advertisement for washing machine sensor data"""
    
    def __init__(self, machine_type, machine_id, rms, dominant_freq, battery_percent, company_id, protocol_version):
        super().__init__()
        
        # Pack the data according to our protocol v2
        # Machine type: 1=washer, 2=dryer
        m_type = min(255, max(1, int(machine_type)))
        # RMS as uint16 (value × 100)
        rms_int = min(65535, int(rms * 100))
        # Frequency as uint16 (value × 10)
        freq_int = min(65535, int(dominant_freq * 10))
        # Battery as uint8 (0-100)
        batt = min(100, max(0, int(battery_percent)))
        
        # Create manufacturer data payload (no flags byte anymore)
        # Format: company_id (2) + version (1) + machine_type (1) + machine_id (1) + rms (2) + freq (2) + batt (1)
        mfg_data = struct.pack(
            "<HBBBHHB",
            company_id,
            protocol_version,
            m_type,
            machine_id,
            rms_int,
            freq_int,
            batt
        )
        
        self.manufacturer_data = {company_id: mfg_data[2:]}  # Exclude company ID (added by library)
        self.connectable = False
        self.flags = 0x06  # General discoverable, BR/EDR not supported

def broadcast_data(machine_type, machine_id, rms, dominant_freq, battery_percent, company_id, protocol_version):
    """Broadcast sensor data via BLE advertising"""
    ble = adafruit_ble.BLERadio()
    
    # Create our custom advertisement
    adv = WashingMachineAdvertisement(
        machine_type=machine_type,
        machine_id=machine_id,
        rms=rms,
        dominant_freq=dominant_freq,
        battery_percent=battery_percent,
        company_id=company_id,
        protocol_version=protocol_version
    )
    
    # Advertise for a short time (enough for aggregator to receive)
    # Advertising interval: 100ms, duration: 500ms (5 advertisements)
    type_str = "Waschmaschine" if machine_type == 1 else "Tumbler"
    print(f"Broadcasting: Type={type_str}, ID={machine_id}, RMS={rms:.2f}, Freq={dominant_freq:.1f}Hz, Batt={battery_percent}%")
    
    ble.start_advertising(adv, interval=0.1)
    time.sleep(0.5)  # Advertise for 500ms
    ble.stop_advertising()

# ============================================================================
# Deep Sleep
# ============================================================================

def enter_deep_sleep(duration_sec):
    """Enter deep sleep for specified duration"""
    print(f"Entering deep sleep for {duration_sec} seconds...")
    
    # Create a time alarm
    time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + duration_sec)
    
    # Enter deep sleep - this will reset the device on wake
    alarm.exit_and_deep_sleep_until_alarms(time_alarm)

# ============================================================================
# Main Loop
# ============================================================================

def main():
    """Main function - runs once per wake cycle"""
    print("\n" + "="*50)
    print("Washing Machine Sensor Node")
    print(f"Machine ID: {CONFIG['machine_id']}")
    print("="*50)
    
    # Check wake reason
    if alarm.wake_alarm:
        print(f"Woke from deep sleep: {type(alarm.wake_alarm).__name__}")
    else:
        print("Initial boot (not from deep sleep)")
    
    try:
        # Initialize accelerometer
        print("Initializing accelerometer...")
        sensor = init_accelerometer()
        
        # Small delay for sensor to stabilize
        time.sleep(0.05)
        
        # Collect samples
        print(f"Collecting samples for {CONFIG['sample_duration_sec']}s at {CONFIG['sample_rate_hz']}Hz...")
        samples = collect_samples(
            sensor,
            CONFIG['sample_duration_sec'],
            CONFIG['sample_rate_hz']
        )
        print(f"Collected {len(samples)} samples")
        
        # Calculate magnitude (combine X, Y, Z)
        magnitudes = calculate_magnitude(samples)
        
        # Remove gravity (DC offset) for vibration analysis
        ac_magnitudes = remove_dc_offset(magnitudes)
        
        # Calculate RMS of AC component (vibration intensity)
        rms = calculate_rms(ac_magnitudes)
        print(f"Vibration RMS: {rms:.3f} m/s²")
        
        # FFT disabled to save battery - frequency set to 0
        dominant_freq = 0.0
        
        # Get battery percentage
        battery_percent = get_battery_percent()
        print(f"Battery: {battery_percent}%")
        
        # Broadcast via BLE
        broadcast_data(
            machine_type=CONFIG['machine_type'],
            machine_id=CONFIG['machine_id'],
            rms=rms,
            dominant_freq=dominant_freq,
            battery_percent=battery_percent,
            company_id=CONFIG['company_id'],
            protocol_version=CONFIG['protocol_version']
        )
        
    except Exception as e:
        print(f"Error during measurement: {e}")
    
    # Enter deep sleep
    enter_deep_sleep(CONFIG['wake_interval_sec'])

# Run main
main()
