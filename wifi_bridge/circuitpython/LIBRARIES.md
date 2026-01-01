# WiFi Bridge Required Libraries

This WiFi Bridge component requires the following CircuitPython libraries:

## Core Libraries (included with CircuitPython)
- time
- json  
- board
- digitalio
- gc
- binascii
- base64
- wifi
- ssl
- socketpool

## External Libraries (download from CircuitPython Bundle)

Download from: https://circuitpython.org/libraries

### Required Bundle Libraries:
1. **adafruit_requests.mpy** - HTTP requests
   - Copy to: `/lib/`

## Custom Libraries (included in this project)

### LoRa Driver (already included in lib/ folder):
1. **sx1262.py** - Main SX1262 driver
2. **sx126x.py** - Base SX126x class  
3. **_sx126x.py** - Constants and register definitions

## Installation Instructions

1. Install CircuitPython on your Seeed XIAO ESP32S3 Sense
2. Copy the contents of this `circuitpython/` folder to the device
3. Download `adafruit_requests.mpy` from the CircuitPython bundle
4. Copy `adafruit_requests.mpy` to the `/lib/` folder on the device
5. Edit `config.json` with your WiFi credentials and server settings
6. Connect your SX1262 LoRa module according to the pin configuration

## Pin Configuration

Default pins for Seeed XIAO ESP32S3 Sense:
- CS (Chip Select): GPIO41
- IRQ (Interrupt): GPIO39
- RST (Reset): GPIO42  
- BUSY: GPIO40
- MOSI: GPIO35 (SPI)
- MISO: GPIO36 (SPI)
- SCK: GPIO37 (SPI)

Modify pins in `config.json` if using different connections.