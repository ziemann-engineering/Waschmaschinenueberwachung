# Washing Machine Monitoring System

A low-power IoT system to monitor washing machines and dryers, showing availability on a web interface.

## Architecture Overview

```
┌─────────────────┐     BLE      ┌─────────────────┐     LoRa 868MHz     ┌─────────────────┐     HTTP     ┌─────────────────┐
│  Sensor Nodes   │  ─────────►  │   Aggregators   │  ────────────────►  │  WiFi Bridge    │  ─────────► │     Server      │
│  (up to 20/agg) │  advertising │   (up to 15)    │     < 1 km          │                 │             │                 │
└─────────────────┘              └─────────────────┘                     └─────────────────┘             └─────────────────┘
    XIAO nRF52840                   XIAO ESP32S3                          XIAO ESP32S3                    Python + Flask
    + LSM6DS3                       + SX1262 LoRa                         + SX1262 LoRa                   HTTP Endpoint
    CR2032 powered                  USB powered                           + WiFi                          Web Interface
```

## Components

### 1. Sensor Node (`/sensor_node`)
- **Hardware**: Seeed XIAO nRF52840 Sense (onboard LSM6DS3 accelerometer)
- **Power**: CR2032 coin cell (~220mAh)
- **Firmware**: CircuitPython (prototype), later C/C++ for production
- **Function**: 
  - Wake every 3 minutes
  - Sample accelerometer at 100 Hz for 200ms (20 samples)
  - Calculate RMS acceleration (FFT disabled to save power)
  - Broadcast data via BLE advertising
  - Deep sleep between measurements

### 2. Aggregator (`/aggregator`)
- **Hardware**: Seeed XIAO ESP32S3 + SX1262 LoRa module
- **Power**: USB powered (no constraints)
- **Firmware**: CircuitPython
- **Function**:
  - Continuously scan for BLE advertisements from sensor nodes
  - Forward received data via LoRa immediately
  - Add the configured aggregator name to packets

### 3. WiFi Bridge (`/wifi_bridge`)
- **Hardware**: Seeed XIAO ESP32S3 + SX1262 LoRa module
- **Power**: USB powered
- **Firmware**: CircuitPython
- **Function**:
  - Receive LoRa packets from aggregators
  - Forward data to server via HTTP POST
  - Bridge between LoRa and WiFi networks

### 4. Server (`/server`)
- **Hardware**: Any PC/Raspberry Pi
- **Software**: Python 3.10+
- **Function**:
  - Receive data via HTTP endpoint
  - Determine machine state based on vibration data
  - Serve web interface showing machine status
  - Store history in SQLite database
  - Send notifications when machines are done

## Machine States

| State | Color | Condition |
|-------|-------|-----------|
| **Running** | 🔴 Red | RMS > threshold |
| **Stopping** | 🟡 Yellow | RMS < threshold for < 10 min |
| **Likely Done** | 🟢 Yellow-Green | RMS < threshold for 10 min - 2 hours |
| **Free** | 🔵 Blue-Green | RMS < threshold for > 2 hours |
| **Offline** | ⚫ Gray | No data received for > 5 minutes |

## Configuration

Each device uses a JSON config file stored on its filesystem:

### Sensor Node (`config.json`)
```json
{
  "machine_id": 1,
  "wake_interval_sec": 180,
  "sample_duration_sec": 0.2,
  "sample_rate_hz": 100
}
```

### Aggregator (`config.json`)
```json
{
  "aggregator_name": "D2",
  "ble_scan_duration_sec": 5,
  "lora_tx_interval_sec": 0
}
```

### Server (`config.json`)
```json
{
  "serial_port": "COM3",
  "serial_baud": 115200,
  "web_port": 8080,
  "thresholds": {
    "running_rms": 0.5,
    "done_minutes": 10,
    "free_minutes": 120
  }
}
```

## Protocol Specifications

### BLE Advertising Format (Sensor → Aggregator)
Manufacturer-specific data in BLE advertisement (no connection needed):

| Byte | Field | Description |
|------|-------|-------------|
| 0-1 | Company ID | 0xFFFF (reserved for testing) |
| 2 | Protocol Version | 0x02 |
| 3 | Machine ID | 1-255 |
| 4-5 | RMS × 100 | uint16, little-endian (e.g., 150 = 1.50 m/s²) |
| 6-7 | Dominant Freq × 10 | uint16, little-endian (0 when FFT disabled) |
| 8 | Battery voltage | `0..255` = `1.00..3.55 V` in 10 mV steps |
| 9 | Flags | Bit 0: low battery warning |

Total: 10 bytes in manufacturer data

### LoRa Packet Format (Aggregator → Server)
Binary packet, variable length:

| Byte | Field | Description |
|------|-------|-------------|
| 0 | Name Length | UTF-8 byte length (1-32) |
| 1..L | Aggregator Name | UTF-8 name, for example `D2` |
| 1+L | Machine Count | Number of sensors in this packet (N) |
| 2+L | Sensor Data | N × 13 bytes |

Sensor Data (13 bytes each):
| Offset | Field | Description |
|--------|-------|-------------|
| 0-5 | Sensor ID | Six-byte BLE address |
| 6 | Flags | Bit 0: assignment active |
| 7-8 | RMS × 1000 | uint16, little-endian |
| 9-10 | Dominant Freq × 10 | uint16, little-endian |
| 11 | Battery voltage | `0..255` = `1.00..3.55 V` in 10 mV steps |
| 12 | RSSI | Signed BLE RSSI in dBm |

The frame also includes the four-byte Waveshare header before this payload and a CRC-32 after it.

### LoRa Parameters
- **Frequency**: 868.0 MHz (EU ISM band)
- **Spreading Factor**: SF10 (good range vs. time tradeoff)
- **Bandwidth**: 125 kHz
- **Coding Rate**: 4/5
- **TX Power**: 14 dBm
- **Preamble**: 8 symbols

Estimated time on air for 122 bytes @ SF10: ~370 ms

## Power Budget (Sensor Node)

CR2032 capacity: ~220 mAh

**Optimized settings:** Wake every 3 min, sample 200ms, FFT disabled

| Phase | Duration | Current | Charge per cycle |
|-------|----------|---------|------------------|
| Deep Sleep | 179.7 s | 1.5 µA | 0.075 µAh |
| Wake + Init | 50 ms | 5 mA | 0.069 µAh |
| Sampling | 200 ms | 3 mA | 0.167 µAh |
| RMS Calc | 10 ms | 10 mA | 0.028 µAh |
| BLE TX | 50 ms | 15 mA | 0.208 µAh |
| **Total/cycle** | 180 s | - | **~0.55 µAh** |

Cycles per day: 480
Daily consumption: ~0.26 mAh
**Estimated battery life: ~6-12 months** (CircuitPython)

With C firmware: potentially 12-18 months

## Installation

### Sensor Node
1. Install CircuitPython 9.x on XIAO nRF52840
2. Copy `sensor_node/` contents to CIRCUITPY drive
3. Edit `config.json` with machine ID
4. Connect CR2032 battery

### Aggregator
### Aggregator
1. Install CircuitPython 9.x on XIAO ESP32S3
2. Copy `aggregator/circuitpython/` contents to CIRCUITPY drive
3. Edit `config.json` with the aggregator name
4. Connect LoRa UART module and power via USB

### Server
1. Install Python 3.10+
2. `pip install -r server/requirements.txt`
3. Edit `server/config.json` with correct COM port
4. Connect Waveshare USB-TO-LoRa-xF adapter
5. Run `python server/main.py`
6. Open http://localhost:8080 in browser

## Web Interface

- **Main page** (`/`): All aggregators with machine counts
- **Aggregator page** (`/aggregator/<name>`): Machines for one aggregator
- **API** (`/api/status`): JSON status for all machines
- **Subscribe** (`/subscribe`): Register for done notifications

## Directory Structure

```
/
├── README.md                 # This file
├── sensor_node/
│   ├── circuitpython/        # CircuitPython firmware
│   │   ├── code.py
│   │   ├── config.json
│   │   └── LIBRARIES.md
│   └── c_firmware/           # Future C implementation (TODO)
├── aggregator/
│   ├── circuitpython/        # CircuitPython firmware
│   │   ├── code.py
│   │   ├── config.json
│   │   └── LIBRARIES.md
│   └── platformio/           # Alternative Arduino/PlatformIO version
│       ├── platformio.ini
│       ├── src/main.cpp
│       └── include/config.h
└── server/
    ├── requirements.txt
    ├── config.json
    ├── main.py
    ├── lora_receiver.py
    ├── state_machine.py
    ├── database.py
    ├── notifications.py
    ├── static/
    │   └── style.css
    └── templates/
        ├── index.html
        └── aggregator.html
```

## Hardware Setup

### Waveshare USB-TO-LoRa-xF Configuration
The server automatically configures the Waveshare module on startup:
- Spreading Factor: SF10 (good range)
- Bandwidth: 125 kHz
- Channel: 18 (868 MHz)

To manually configure via serial terminal (115200 baud):
```
+++                    # Enter AT mode
AT+SF=10              # Set spreading factor
AT+BW=0               # Set bandwidth (0=125kHz)
AT+TXCH=18            # Set TX channel (868MHz)
AT+RXCH=18            # Set RX channel
AT+MODE=1             # Stream mode
AT+EXIT               # Exit AT mode
```

## License

MIT License - See LICENSE file

## Contributing

Contributions welcome! Please read CONTRIBUTING.md first.
