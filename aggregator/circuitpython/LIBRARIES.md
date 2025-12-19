# Required CircuitPython Libraries for Aggregator

## Hardware Setup
- **Board**: Seeed XIAO ESP32S3 (Sense version)
- **LoRa Module**: SX1262 via board-to-board connector
- **Firmware**: CircuitPython for XIAO S3 Sense

## Library Files (included in lib/)

The following SX1262 driver files are included in the `lib/` folder:

1. **sx1262.py** - High-level SX1262 LoRa driver
2. **sx126x.py** - Base SX126X driver class
3. **_sx126x.py** - Constants and error definitions

These are from the CircuitPython/MicroPython SX126x library.

## Additional Libraries (from Adafruit Bundle)

Copy these libraries to the `CIRCUITPY/lib/` folder if using BLE scanning:

1. **adafruit_ble/** (folder)
   - BLE scanning and advertisement parsing
   
### Installation via circup

```bash
pip install circup
circup install adafruit_ble
```

Or manually download from: https://circuitpython.org/libraries

## CircuitPython Installation for XIAO ESP32S3 Sense

1. Download CircuitPython 9.x for Seeed XIAO ESP32S3 Sense:
   https://circuitpython.org/board/seeed_xiao_esp32s3_sense/

2. Enter bootloader mode:
   - Hold BOOT button, press RESET, release BOOT
   - A new drive should appear

3. Copy the `.bin` or `.uf2` file to the drive

4. The board will reboot and a `CIRCUITPY` drive will appear

## Hardware Pin Configuration

### SX1262 LoRa Module (Board-to-Board Connector)

| Function | GPIO Pin |
|----------|----------|
| SPI SCK  | board.SCK |
| SPI MOSI | board.MOSI |
| SPI MISO | board.MISO |
| CS       | GPIO41 |
| IRQ      | GPIO39 |
| RST      | GPIO42 |
| BUSY     | GPIO40 |
| TX/RX SW | GPIO38 |

### LED Indicator
| Function | GPIO Pin |
|----------|----------|
| Status LED | GPIO21 |

## LoRa Settings (Default)

| Parameter | Value |
|-----------|-------|
| Frequency | 868 MHz |
| Bandwidth | 125 kHz |
| Spreading Factor | 7 |
| Coding Rate | 4/5 |
| Sync Word | 0x12 (private) |
| TX Power | -5 dBm |
| TCXO Voltage | 1.7V |

These can be adjusted in `config.json`.

## Deploying to Device

1. Connect the XIAO to your computer via USB
2. Copy `code.py` to the root of `CIRCUITPY`
3. Copy `config.json` to the root of `CIRCUITPY`
4. Copy the contents of `lib/` to `CIRCUITPY/lib/`
5. The device will automatically restart and run the code
