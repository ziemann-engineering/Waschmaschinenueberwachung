# Required CircuitPython Libraries

Download from: https://circuitpython.org/libraries

## For Seeed XIAO nRF52840 Sense with CircuitPython 9.x

Copy these libraries to the `CIRCUITPY/lib/` folder:

### Required Libraries

1. **adafruit_lsm6ds/** (folder)
   - Contains LSM6DS3 driver for the onboard accelerometer
   
2. **adafruit_ble/** (folder)
   - BLE support for advertising
   
3. **adafruit_register/** (folder)
   - Required by adafruit_lsm6ds

### Installation

1. Download the CircuitPython Library Bundle for 9.x:
   https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases

2. Extract and copy these folders to `CIRCUITPY/lib/`:
   - `adafruit_lsm6ds/`
   - `adafruit_ble/`
   - `adafruit_register/`

### Alternative: Using circup

```bash
pip install circup
circup install adafruit_lsm6ds adafruit_ble
```

## Memory Considerations

The XIAO nRF52840 has limited RAM. If you encounter memory issues:

1. Use `.mpy` files (pre-compiled) instead of `.py` files
2. Remove unnecessary libraries from the bundle
3. Consider the C firmware implementation for production

## CircuitPython Installation

1. Download CircuitPython 9.x for Seeed XIAO nRF52840:
   https://circuitpython.org/board/seeeduino_xiao_nrf52840/

2. Enter bootloader mode:
   - Double-tap the reset button quickly
   - A new drive `XIAO-SENSE` should appear

3. Copy the `.uf2` file to the drive

4. The board will reboot and a `CIRCUITPY` drive will appear
