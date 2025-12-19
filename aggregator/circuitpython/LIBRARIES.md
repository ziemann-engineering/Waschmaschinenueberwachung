# Required CircuitPython Libraries for Aggregator

Download from: https://circuitpython.org/libraries

## For Seeed XIAO ESP32S3 with CircuitPython 9.x

Copy these libraries to the `CIRCUITPY/lib/` folder:

### Required Libraries

1. **adafruit_ble/** (folder)
   - BLE scanning and advertisement parsing
   
### Installation

1. Download the CircuitPython Library Bundle for 9.x:
   https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases

2. Extract and copy these folders to `CIRCUITPY/lib/`:
   - `adafruit_ble/`

### Alternative: Using circup

```bash
pip install circup
circup install adafruit_ble
```

## CircuitPython Installation for XIAO ESP32S3

1. Download CircuitPython 9.x for Seeed XIAO ESP32S3:
   https://circuitpython.org/board/seeed_xiao_esp32s3/

2. Enter bootloader mode:
   - Hold BOOT button, press RESET, release BOOT
   - A new drive should appear

3. Copy the `.bin` or `.uf2` file to the drive

4. The board will reboot and a `CIRCUITPY` drive will appear

## LoRa Module Wiring

### For Grove Wio-E5 (UART AT commands):
| Wio-E5 | XIAO ESP32S3 |
|--------|--------------|
| VCC    | 3V3          |
| GND    | GND          |
| TX     | D7 (RX)      |
| RX     | D6 (TX)      |

### For Generic SX1262 UART Module (Stream mode):
| Module | XIAO ESP32S3 |
|--------|--------------|
| VCC    | 3V3          |
| GND    | GND          |
| TXD    | D7 (RX)      |
| RXD    | D6 (TX)      |

### For SPI-based SX1262 (requires different code):
Use the Arduino/PlatformIO version instead, or adapt with adafruit_rfm9x library.

## Notes

- The XIAO ESP32S3 has built-in BLE support
- Make sure the LoRa module is configured for the same frequency/SF/BW as the receiver
- Default: 868MHz, SF7, BW125kHz (adjust in module config or AT commands)
