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
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class MachineReading:
    """Single machine reading from LoRa packet"""
    aggregator_id: int
    machine_type: int       # 1=washer, 2=dryer
    machine_id: int
    rms: float              # m/s²
    dominant_freq: float    # Hz
    battery_percent: int
    timestamp: float        # Unix timestamp


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
    - Byte 0: Aggregator ID
    - Byte 1: Machine count (N)
    - Bytes 2+: N × 6 bytes of machine data
      - Byte 0: Machine ID
      - Bytes 1-2: RMS × 100 (uint16, little-endian)
      - Bytes 3-4: Freq × 10 (uint16, little-endian)
      - Byte 5: Battery %
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
                    while len(buffer) >= 2:
                        # Validate aggregator ID (should be 1-255)
                        if buffer[0] == 0 or buffer[0] > 250:
                            # Invalid packet start, skip byte
                            logger.warning(f"Invalid aggregator ID {buffer[0]}, skipping byte")
                            del buffer[0]
                            continue
                        
                        # Validate machine count (should be reasonable)
                        machine_count = buffer[1]
                        if machine_count == 0 or machine_count > 30:
                            # Invalid count, skip byte
                            logger.warning(f"Invalid machine count {machine_count}, skipping byte")
                            del buffer[0]
                            continue
                        
                        # Protocol v2: 7 bytes per machine (type + id + rms + freq + batt)
                        packet_len = 2 + (machine_count * 7)
                        
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
        """Parse a complete LoRa packet (Protocol v2)"""
        try:
            aggregator_id = packet[0]
            machine_count = packet[1]
            
            logger.debug(f"Received packet: aggregator={aggregator_id}, machines={machine_count}")
            
            timestamp = time.time()
            offset = 2
            
            for i in range(machine_count):
                if offset + 7 > len(packet):
                    logger.warning("Packet truncated")
                    break
                    
                machine_type = packet[offset]
                machine_id = packet[offset+1]
                rms_x100 = struct.unpack('<H', packet[offset+2:offset+4])[0]
                freq_x10 = struct.unpack('<H', packet[offset+4:offset+6])[0]
                battery = packet[offset+6]
                
                reading = MachineReading(
                    aggregator_id=aggregator_id,
                    machine_type=machine_type,
                    machine_id=machine_id,
                    rms=rms_x100 / 100.0,
                    dominant_freq=freq_x10 / 10.0,
                    battery_percent=battery,
                    timestamp=timestamp
                )
                
                type_str = "W" if machine_type == 1 else "T"
                logger.info(
                    f"[{type_str}] Machine {aggregator_id}/{machine_id}: "
                    f"RMS={reading.rms:.2f} m/s², "
                    f"Freq={reading.dominant_freq:.1f} Hz, "
                    f"Batt={reading.battery_percent}%"
                )
                
                if self.callback:
                    self.callback(reading)
                    
                offset += 7
                
        except Exception as e:
            logger.exception(f"Failed to parse packet: {e}")


# For testing without hardware
class MockLoRaReceiver(LoRaReceiver):
    """Mock receiver that generates fake data for testing"""
    
    def __init__(self, *args, **kwargs):
        super().__init__("MOCK", 9600)
        self._mock_connected = True
        self.mock_machines = [
            (1, 1, True),   # Aggregator 1, Machine 1, running
            (1, 2, False),  # Aggregator 1, Machine 2, idle
            (1, 3, True),   # Aggregator 1, Machine 3, running
            (2, 1, False),  # Aggregator 2, Machine 1, idle
            (2, 2, True),   # Aggregator 2, Machine 2, running
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
            for agg_id, machine_id, is_running in self.mock_machines:
                if is_running:
                    rms = random.uniform(1.0, 3.0)
                    freq = random.uniform(10, 25)
                else:
                    rms = random.uniform(0.01, 0.1)
                    freq = random.uniform(0, 5)
                    
                reading = MachineReading(
                    aggregator_id=agg_id,
                    machine_id=machine_id,
                    rms=rms,
                    dominant_freq=freq,
                    battery_percent=random.randint(70, 100),
                    timestamp=time.time()
                )
                
                if self.callback:
                    self.callback(reading)
                    
            time.sleep(5)  # Simulate 5-second intervals
