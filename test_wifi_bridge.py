"""
Test script to simulate WiFi bridge sending data to server
"""

import requests
import struct
import time

def create_test_packet(aggregator_id=1, machines=None):
    """
    Create a test LoRa packet
    
    Args:
        aggregator_id: Aggregator ID (1-255)
        machines: List of dicts with keys: type, id, rms, freq, battery
                  Example: [{'type': 1, 'id': 1, 'rms': 2.5, 'freq': 50.0, 'battery': 85}]
    """
    if machines is None:
        # Create heartbeat packet (0 machines)
        packet = bytes([aggregator_id, 0])
    else:
        # Create data packet
        packet = bytes([aggregator_id, len(machines)])
        
        for machine in machines:
            machine_type = machine['type']  # 1=washer, 2=dryer
            machine_id = machine['id']
            rms_x100 = int(machine['rms'] * 100)
            freq_x10 = int(machine['freq'] * 10)
            battery = machine['battery']
            
            # Pack machine data (7 bytes per machine)
            packet += bytes([machine_type, machine_id])
            packet += struct.pack('<H', rms_x100)  # 2 bytes, little-endian
            packet += struct.pack('<H', freq_x10)  # 2 bytes, little-endian
            packet += bytes([battery])
    
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
    packet = create_test_packet(aggregator_id=1, machines=None)
    send_to_server(packet)


def test_single_machine():
    """Test sending data for a single machine"""
    print("\n=== Testing Single Machine ===")
    machines = [
        {
            'type': 1,      # Washer
            'id': 1,        # Machine ID 1
            'rms': 2.5,     # 2.5 m/s² (running)
            'freq': 50.0,   # 50.0 Hz
            'battery': 85   # 85%
        }
    ]
    packet = create_test_packet(aggregator_id=1, machines=machines)
    send_to_server(packet)


def test_multiple_machines():
    """Test sending data for multiple machines"""
    print("\n=== Testing Multiple Machines ===")
    machines = [
        {
            'type': 1,      # Washer
            'id': 1,
            'rms': 3.2,     # Running
            'freq': 48.5,
            'battery': 90
        },
        {
            'type': 2,      # Dryer
            'id': 2,
            'rms': 0.5,     # Idle/Free
            'freq': 0.0,
            'battery': 75
        },
        {
            'type': 1,      # Washer
            'id': 3,
            'rms': 2.8,     # Running
            'freq': 51.2,
            'battery': 60
        }
    ]
    packet = create_test_packet(aggregator_id=1, machines=machines)
    send_to_server(packet)


def test_cycle_simulation():
    """Simulate a complete washing cycle"""
    print("\n=== Testing Complete Cycle Simulation ===")
    
    # Machine starts idle
    print("\n1. Machine is FREE (idle)")
    machines = [{'type': 1, 'id': 1, 'rms': 0.3, 'freq': 0.0, 'battery': 95}]
    send_to_server(create_test_packet(1, machines))
    time.sleep(2)
    
    # Machine starts running
    print("\n2. Machine starts RUNNING")
    machines = [{'type': 1, 'id': 1, 'rms': 2.5, 'freq': 50.0, 'battery': 94}]
    send_to_server(create_test_packet(1, machines))
    time.sleep(2)
    
    # Machine still running (simulate updates during cycle)
    print("\n3. Machine still RUNNING (mid-cycle)")
    machines = [{'type': 1, 'id': 1, 'rms': 3.0, 'freq': 49.5, 'battery': 93}]
    send_to_server(create_test_packet(1, machines))
    time.sleep(2)
    
    # Machine cycle done (low vibration but not yet opened)
    print("\n4. Machine DONE (cycle finished)")
    machines = [{'type': 1, 'id': 1, 'rms': 0.5, 'freq': 0.0, 'battery': 92}]
    send_to_server(create_test_packet(1, machines))
    time.sleep(2)
    
    # Machine door opened and becomes free
    print("\n5. Machine FREE again (door opened)")
    machines = [{'type': 1, 'id': 1, 'rms': 0.2, 'freq': 0.0, 'battery': 92}]
    send_to_server(create_test_packet(1, machines))


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
            test_single_machine()
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
        test_single_machine()
        time.sleep(1)
        test_multiple_machines()
        time.sleep(1)
        test_cycle_simulation()
    
    print("\n=== All tests completed ===")
