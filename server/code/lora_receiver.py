"""
LoRa Receiver Module
Handles serial communication with Waveshare USB-TO-LoRa-xF adapter

Waveshare USB-TO-LoRa-xF specs:
- Default baud rate: 115200
- Default mode: Stream/transparent mode
- Frequency: 868MHz (EU) or 433MHz
- Default: SF7, BW125kHz, CR4/5

The module operates in transparent mode - any bytes sent to serial
are transmitted via LoRa, and received LoRa data appears on serial.
"""

import serial
import binascii
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, List
import logging

logger = logging.getLogger(__name__)


def decode_battery_voltage(encoded_voltage: int) -> float:
    """Decode a battery byte where 0 is 1.00 V and each step is 10 mV."""
    return round(1.0 + encoded_voltage / 100.0, 2)


@dataclass
class MachineReading:
    """Single machine reading from LoRa packet"""
    aggregator_name: str
    sensor_id: str
    assignment_active: bool
    rms: float              # m/s²
    dominant_freq: float    # Hz
    battery_voltage: float  # V
    timestamp: float        # Unix timestamp
    rssi: int = -128        # BLE RSSI in dBm
    machine_type: int = 0
    machine_id: int = 0


def decode_forwarded_packet(packet: bytes, timestamp: Optional[float] = None):
    """Decode an aggregator frame including Waveshare header and CRC-32."""
    if len(packet) < 10:
        raise ValueError("Packet too short")

    expected_crc = struct.unpack('<I', packet[-4:])[0]
    actual_crc = binascii.crc32(packet[:-4]) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ValueError("CRC mismatch")

    payload = packet[4:-4]
    name_length = payload[0]
    if name_length == 0 or name_length > 32 or len(payload) < name_length + 2:
        raise ValueError("Invalid aggregator name length")
    try:
        aggregator_name = payload[1:1 + name_length].decode('utf-8')
    except UnicodeError as exception:
        raise ValueError("Invalid aggregator name encoding") from exception
    machine_count = payload[1 + name_length]
    expected_length = 2 + name_length + machine_count * 13
    if len(payload) != expected_length:
        raise ValueError(
            f"Invalid payload length: expected {expected_length}, got {len(payload)}"
        )

    reading_time = time.time() if timestamp is None else timestamp
    readings = []
    offset = 2 + name_length
    for _ in range(machine_count):
        readings.append(MachineReading(
            aggregator_name=aggregator_name,
            sensor_id=payload[offset:offset + 6].hex().upper(),
            assignment_active=bool(payload[offset + 6] & 0x01),
            rms=struct.unpack('<H', payload[offset + 7:offset + 9])[0] / 1000.0,
            dominant_freq=struct.unpack('<H', payload[offset + 9:offset + 11])[0] / 10.0,
            battery_voltage=decode_battery_voltage(payload[offset + 11]),
            timestamp=reading_time,
            rssi=struct.unpack('b', payload[offset + 12:offset + 13])[0]
        ))
        offset += 13

    return aggregator_name, readings


class WaveshareLoRaConfig:
    """
    Configuration helper for Waveshare USB-TO-LoRa-xF module.
    Uses AT commands to configure the module.
    """
    
    def __init__(self, serial_port: serial.Serial):
        self.serial = serial_port
        
    def enter_at_mode(self) -> bool:
        """Enter AT command mode by sending +++"""
        self.serial.write(b"+++\r\n")
        time.sleep(0.5)
        response = self._read_response()
        return "OK" in response or "++" in response
        
    def exit_at_mode(self) -> bool:
        """Exit AT command mode"""
        return self._send_command("AT+EXIT")
        
    def _send_command(self, cmd: str, timeout: float = 1.0) -> bool:
        """Send AT command and check for OK response"""
        self.serial.write(f"{cmd}\r\n".encode())
        time.sleep(0.1)
        response = self._read_response(timeout)
        logger.debug(f"AT: {cmd} -> {response}")
        return "OK" in response or "+OK" in response.upper()
        
    def _read_response(self, timeout: float = 1.0) -> str:
        """Read response from serial"""
        start = time.time()
        response = b""
        while time.time() - start < timeout:
            if self.serial.in_waiting:
                response += self.serial.read(self.serial.in_waiting)
            time.sleep(0.01)
        return response.decode(errors='ignore')
        
    def configure(self, sf: int = 10, bw: int = 0, channel: int = 18) -> bool:
        """
        Configure LoRa parameters to match aggregator settings.
        
        Args:
            sf: Spreading factor (7-12), default 10 for range
            bw: Bandwidth (0=125kHz, 1=250kHz, 2=500kHz)
            channel: Channel number (18 = 868MHz for HF version)
        
        Returns:
            True if configuration successful
        """
        if not self.enter_at_mode():
            logger.error("Failed to enter AT mode")
            return False
            
        success = True
        
        # Set spreading factor
        if not self._send_command(f"AT+SF={sf}"):
            logger.error(f"Failed to set SF={sf}")
            success = False
            
        # Set bandwidth
        if not self._send_command(f"AT+BW={bw}"):
            logger.error(f"Failed to set BW={bw}")
            success = False
            
        # Set TX/RX channel
        if not self._send_command(f"AT+TXCH={channel}"):
            logger.error(f"Failed to set TXCH={channel}")
            success = False
        if not self._send_command(f"AT+RXCH={channel}"):
            logger.error(f"Failed to set RXCH={channel}")
            success = False
            
        # Set stream mode
        if not self._send_command("AT+MODE=1"):
            logger.error("Failed to set stream mode")
            success = False
            
        # Exit AT mode
        self.exit_at_mode()
        
        return success


class LoRaReceiver:
    """
    Receives data from Waveshare USB-TO-LoRa-xF adapter via serial.
    
    The Waveshare module works in stream/transparent mode by default.
    Data received via LoRa appears directly on the serial port.
    
    Expected packet format (binary):
        - 4-byte Waveshare header
        - 1-byte aggregator name length
        - UTF-8 aggregator name
        - 1-byte machine count
        - N × 13-byte sensor records
        - CRC-32
    """
    
    # Waveshare default baud rate is 115200
    def __init__(self, port: str, baud_rate: int = 115200, configure: bool = True):
        self.port = port
        self.baud_rate = baud_rate
        self.configure_on_start = configure
        self.serial: Optional[serial.Serial] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.callback: Optional[Callable[[MachineReading], None]] = None
        self.last_packet_time = 0.0
        self.packets_received = 0
        
    def set_callback(self, callback: Callable[[MachineReading], None]):
        """Set callback function for received readings"""
        self.callback = callback
    
    @property
    def is_connected(self) -> bool:
        """Check if LoRa receiver is connected"""
        return self.serial is not None and self.serial.is_open
        
    def start(self) -> bool:
        """Start receiving data in background thread.
        
        Returns:
            True if started successfully, False if connection failed
        """
        if self.running:
            return self.is_connected
            
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                timeout=1.0
            )
            logger.info(f"Opened serial port {self.port} at {self.baud_rate} baud")
        except serial.SerialException as e:
            logger.error(f"Failed to open serial port: {e}")
            logger.warning("LoRa receiver not found - running without hardware")
            self.running = True  # Still mark as running to prevent repeated start attempts
            return False
        
        # Optionally configure the Waveshare module
        if self.configure_on_start:
            logger.info("Configuring Waveshare LoRa module...")
            config = WaveshareLoRaConfig(self.serial)
            if config.configure(sf=10, bw=0, channel=18):
                logger.info("LoRa module configured successfully")
            else:
                logger.warning("LoRa module configuration failed, using defaults")
            
        self.running = True
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()
        logger.info("LoRa receiver started")
        return True
        
    def stop(self):
        """Stop receiving"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.serial:
            self.serial.close()
        logger.info("LoRa receiver stopped")
    
    def get_stats(self) -> dict:
        """Get receiver statistics"""
        return {
            "connected": self.is_connected,
            "packets_received": self.packets_received,
            "last_packet_time": self.last_packet_time,
            "port": self.port,
            "baud_rate": self.baud_rate
        }
        
    def _receive_loop(self):
        """Main receive loop running in background thread"""
        buffer = bytearray()
        last_data_time = time.time()
        
        while self.running:
            try:
                # Read available data
                if self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    buffer.extend(data)
                    last_data_time = time.time()
                    
                    logger.debug(f"Received {len(data)} bytes, buffer now {len(buffer)} bytes")
                    
                    # Try to parse complete packets
                    while len(buffer) >= 10:
                        if buffer[:4] != b'\x00\x00\x00\x00':
                            logger.warning("Invalid Waveshare header, skipping byte")
                            del buffer[0]
                            continue

                        name_length = buffer[4]
                        if name_length == 0 or name_length > 32:
                            logger.warning("Invalid aggregator name length, skipping byte")
                            del buffer[0]
                            continue
                        if len(buffer) < 6 + name_length:
                            break

                        machine_count = buffer[5 + name_length]
                        if machine_count > 30:
                            logger.warning(f"Invalid machine count {machine_count}, skipping byte")
                            del buffer[0]
                            continue

                        packet_len = 10 + name_length + machine_count * 13
                        
                        if len(buffer) >= packet_len:
                            # Extract and parse packet
                            packet = bytes(buffer[:packet_len])
                            del buffer[:packet_len]
                            self._parse_packet(packet)
                            self.packets_received += 1
                            self.last_packet_time = time.time()
                        else:
                            break  # Wait for more data
                else:
                    # Clear stale buffer data after timeout
                    if buffer and time.time() - last_data_time > 2.0:
                        logger.warning(f"Clearing stale buffer: {buffer.hex()}")
                        buffer.clear()
                    time.sleep(0.01)  # Small delay when no data
                    
            except serial.SerialException as e:
                logger.error(f"Serial error: {e}")
                time.sleep(1.0)
            except Exception as e:
                logger.exception(f"Error in receive loop: {e}")
                
    def _parse_packet(self, packet: bytes):
        """Parse a complete name-based LoRa packet."""
        try:
            aggregator_name, readings = decode_forwarded_packet(packet)
            logger.debug(
                f"Received packet: aggregator={aggregator_name}, machines={len(readings)}"
            )

            if not readings:
                logger.info(f"Received heartbeat from aggregator {aggregator_name}")
                return

            for reading in readings:
                logger.info(
                    f"Sensor {reading.sensor_id} from {aggregator_name}: "
                    f"RMS={reading.rms:.2f} m/s², "
                    f"Freq={reading.dominant_freq:.1f} Hz, "
                    f"Batt={reading.battery_voltage:.2f} V"
                )
                
                if self.callback:
                    self.callback(reading)
                
        except Exception as e:
            logger.exception(f"Failed to parse packet: {e}")


# For testing without hardware
class MockLoRaReceiver(LoRaReceiver):
    """Mock receiver that generates fake data for testing"""
    
    def __init__(self, *args, **kwargs):
        super().__init__("MOCK", 9600)
        self._mock_connected = True
        self.mock_machines = [
            ("D1", 1, 1, True),
            ("D1", 1, 2, False),
            ("D1", 2, 3, True),
            ("D2", 1, 1, False),
            ("D2", 2, 2, True),
        ]
    
    @property
    def is_connected(self) -> bool:
        """Mock is always connected"""
        return self._mock_connected
        
    def start(self) -> bool:
        self.running = True
        self.thread = threading.Thread(target=self._mock_loop, daemon=True)
        self.thread.start()
        logger.info("Mock LoRa receiver started")
        return True
        
    def _mock_loop(self):
        """Generate mock readings"""
        import random
        
        while self.running:
            for aggregator_name, machine_type, machine_id, is_running in self.mock_machines:
                if is_running:
                    rms = random.uniform(1.0, 3.0)
                    freq = random.uniform(10, 25)
                else:
                    rms = random.uniform(0.01, 0.1)
                    freq = random.uniform(0, 5)
                    
                reading = MachineReading(
                    aggregator_name=aggregator_name,
                    sensor_id=f"0000000000{machine_id:02X}",
                    assignment_active=False,
                    machine_type=machine_type,
                    machine_id=machine_id,
                    rms=rms,
                    dominant_freq=freq,
                    battery_voltage=round(random.uniform(2.7, 3.2), 2),
                    timestamp=time.time()
                )
                
                if self.callback:
                    self.callback(reading)
                    
            time.sleep(5)  # Simulate 5-second intervals
