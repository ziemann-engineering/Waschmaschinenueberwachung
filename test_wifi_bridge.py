"""
Test script to simulate WiFi bridge sending data to server
"""

import requests
import binascii
import struct
import time


def encode_battery_voltage(voltage):
    """Encode 1.00-3.55 V as a byte in 10 mV steps."""
    if not 1.0 <= voltage <= 3.55:
        raise ValueError("battery voltage must be between 1.00 V and 3.55 V")
    return round((voltage - 1.0) * 100)


def create_test_packet(aggregator_name='D2', sensors=None):
    """
    Create a test LoRa packet
    
    Args:
                aggregator_name: Aggregator name (1-32 UTF-8 bytes)
                sensors: List of dicts with keys: sensor_id, assignment_active, rms, freq, battery_voltage
                        Example: [{'sensor_id': 'C1A2B3C4D5E6', 'assignment_active': True,
                                             'rms': 2.5, 'freq': 50.0, 'battery_voltage': 2.85}]
    """
    encoded_name = aggregator_name.encode('utf-8')
    if not encoded_name or len(encoded_name) > 32:
        raise ValueError('aggregator_name must encode to 1-32 bytes')
    payload = bytes([len(encoded_name)]) + encoded_name

    if sensors is None:
        payload += bytes([0])
    else:
        payload += bytes([len(sensors)])
        
        for sensor in sensors:
            sensor_id = bytes.fromhex(sensor['sensor_id'])
            if len(sensor_id) != 6:
                raise ValueError('sensor_id must contain six hexadecimal bytes')
            rms_x1000 = int(sensor['rms'] * 1000)
            freq_x10 = int(sensor['freq'] * 10)
            battery = encode_battery_voltage(sensor['battery_voltage'])
            
            payload += sensor_id
            payload += bytes([1 if sensor.get('assignment_active') else 0])
            payload += struct.pack('<H', rms_x1000)
            payload += struct.pack('<H', freq_x10)
            payload += bytes([battery])
            payload += struct.pack('b', sensor.get('rssi', -60))

    packet = bytes([0, 0, 0, 0]) + payload
    packet += struct.pack('<I', binascii.crc32(packet) & 0xffffffff)
    return packet


def send_to_server(packet_data, server_url="http://127.0.0.1:8080/api/lora-data"):
    """Send packet to server (simulating WiFi bridge)"""
    # Convert to hex string (like the WiFi bridge does)
    packet_hex = packet_data.hex()
    
    payload = {
        "packet_data": packet_hex
    }
    
    print(f"Sending packet: {packet_hex}")
    print(f"Packet length: {len(packet_data)} bytes")
    
    try:
        response = requests.post(server_url, json=payload)
        print(f"Server response: {response.status_code}")
        print(f"Response data: {response.json()}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False


def test_heartbeat():
    """Test sending a heartbeat packet"""
    print("\n=== Testing Heartbeat ===")
    packet = create_test_packet(aggregator_name='D2')
    send_to_server(packet)


def test_single_sensor():
    """Test sending data for a single sensor."""
    print("\n=== Testing Single Sensor ===")
    sensors = [
        {
            'sensor_id': 'C1A2B3C4D5E6',
            'assignment_active': True,
            'rms': 2.5,     # 2.5 m/s² (running)
            'freq': 50.0,   # 50.0 Hz
            'battery_voltage': 2.85
        }
    ]
    send_to_server(create_test_packet(aggregator_name='D2', sensors=sensors))


def test_multiple_machines():
    """Test sending data for multiple machines"""
    print("\n=== Testing Multiple Machines ===")
    sensors = [
        {
            'sensor_id': 'C1A2B3C4D5E6',
            'rms': 3.2,     # Running
            'freq': 48.5,
            'battery_voltage': 2.90
        },
        {
            'sensor_id': 'C1A2B3C4D5E7',
            'rms': 0.5,     # Idle/Free
            'freq': 0.0,
            'battery_voltage': 2.75
        },
        {
            'sensor_id': 'C1A2B3C4D5E8',
            'rms': 2.8,     # Running
            'freq': 51.2,
            'battery_voltage': 2.60
        }
    ]
    send_to_server(create_test_packet(aggregator_name='D2', sensors=sensors))


def test_cycle_simulation():
    """Simulate a complete washing cycle"""
    print("\n=== Testing Complete Cycle Simulation ===")
    
    # Machine starts idle
    print("\n1. Machine is FREE (idle)")
    sensors = [{'sensor_id': 'C1A2B3C4D5E6', 'rms': 0.3, 'freq': 0.0, 'battery_voltage': 2.95}]
    send_to_server(create_test_packet('D2', sensors))
    time.sleep(2)
    
    # Machine starts running
    print("\n2. Machine starts RUNNING")
    sensors = [{'sensor_id': 'C1A2B3C4D5E6', 'rms': 2.5, 'freq': 50.0, 'battery_voltage': 2.94}]
    send_to_server(create_test_packet('D2', sensors))
    time.sleep(2)
    
    # Machine still running (simulate updates during cycle)
    print("\n3. Machine still RUNNING (mid-cycle)")
    sensors = [{'sensor_id': 'C1A2B3C4D5E6', 'rms': 3.0, 'freq': 49.5, 'battery_voltage': 2.93}]
    send_to_server(create_test_packet('D2', sensors))
    time.sleep(2)
    
    # Machine cycle done (low vibration but not yet opened)
    print("\n4. Machine DONE (cycle finished)")
    sensors = [{'sensor_id': 'C1A2B3C4D5E6', 'rms': 0.5, 'freq': 0.0, 'battery_voltage': 2.92}]
    send_to_server(create_test_packet('D2', sensors))
    time.sleep(2)
    
    # Machine door opened and becomes free
    print("\n5. Machine FREE again (door opened)")
    sensors = [{'sensor_id': 'C1A2B3C4D5E6', 'rms': 0.2, 'freq': 0.0, 'battery_voltage': 2.92}]
    send_to_server(create_test_packet('D2', sensors))


if __name__ == '__main__':
    import sys
    
    print("WiFi Bridge Test Tool")
    print("=" * 60)
    print("Make sure the server is running: python server/main.py")
    print("=" * 60)
    
    if len(sys.argv) > 1:
        test_type = sys.argv[1]
        if test_type == 'heartbeat':
            test_heartbeat()
        elif test_type == 'single':
            test_single_sensor()
        elif test_type == 'multiple':
            test_multiple_machines()
        elif test_type == 'cycle':
            test_cycle_simulation()
        else:
            print(f"Unknown test: {test_type}")
            print("Available tests: heartbeat, single, multiple, cycle")
    else:
        # Run all tests
        test_heartbeat()
        time.sleep(1)
        test_single_sensor()
        time.sleep(1)
        test_multiple_machines()
        time.sleep(1)
        test_cycle_simulation()
    
    print("\n=== All tests completed ===")
