"""
WiFi Bridge for Washing Machine Monitoring System

This component:
1. Receives LoRa packets from aggregators
2. Forwards them to the server via HTTP POST

Hardware: Seeed XIAO ESP32S3 Sense with SX1262 LoRa module
or
LilyGo Lora T3S3 E-Paper ESP32-S3 with SX1262 LoRa module

Pin connections (Seeed XIAO ESP32S3 Sense):
- CS (Chip Select): GPIO41
- IRQ (Interrupt): GPIO39
- RST (Reset): GPIO42
- BUSY: GPIO40
- MOSI: GPIO35
- MISO: GPIO36
- SCK: GPIO37

Pin connections (LilyGo Lora T3S3 E-Paper ESP32-S3):
- CS (Chip Select): D7
- IRQ (Interrupt): GPIO33
- RST (Reset): D8
- GPIO: GPIO15
- MOSI: D6
- MISO: D3
- SCK: D5



Author: Washing Machine Monitoring Team
"""

import time
import json
import board
import microcontroller
import digitalio
import gc
import binascii
import wifi
import ssl
import socketpool
import adafruit_requests as requests
from sx1262 import SX1262

import busio
import displayio
from fourwire import FourWire
import terminalio
import adafruit_ssd1680
from adafruit_display_text import label
from adafruit_display_shapes import rect

displayio.release_displays()

print("Starting WiFi Bridge...")

# Load configuration
print("Loading configuration...")
try:
    with open('config.json', 'r') as f:
        config = json.load(f)
        print("Configuration loaded successfully")
except Exception as e:
    print(f"Error loading config: {e}")

# Initialize LoRa
print("Initializing SX1262...")
board_type = config.get("board_type", "seeed_xiao")

if board_type == "lilygo":
    # LilyGo LoRa T3S3 initialization
    lora = SX1262(
        spi_bus=1,
        clk=board.D5,
        mosi=board.D6,
        miso=board.D3,
        cs=board.D7,
        irq=microcontroller.pin.GPIO33,
        rst=board.D8,
        gpio=microcontroller.pin.GPIO15
    )
    led = digitalio.DigitalInOut(board.D37)
    led.direction = digitalio.Direction.OUTPUT
else:
    # Seeed XIAO ESP32S3 initialization
    lora = SX1262(
        spi_bus=0,
        clk=board.SCK,
        mosi=board.MOSI,
        miso=board.MISO,
        cs=getattr(board, f'IO{config["pins"]["cs"]}'),
        irq=getattr(board, f'IO{config["pins"]["irq"]}'),
        rst=getattr(board, f'IO{config["pins"]["rst"]}'),
        gpio=getattr(board, f'IO{config["pins"]["busy"]}')
    )
    led = digitalio.DigitalInOut(board.D37) # TODO
    led.direction = digitalio.Direction.OUTPUT
    
print("Configuring LoRa...")
lora.begin(
    freq=config["lora"]["frequency"],
    bw=config["lora"]["bandwidth"],
    sf=config["lora"]["spreading_factor"],
    cr=config["lora"]["coding_rate"],
    syncWord=config["lora"]["sync_word"],
    power=config["lora"]["power"],
    currentLimit=60.0,
    preambleLength=8,
    implicit=False,
    implicitLen=0xFF,
    crcOn=True,
    txIq=False,
    rxIq=False,
    tcxoVoltage=1.7,
    useRegulatorLDO=False,
    blocking=True
)

print("SX1262 configured successfully")

# Initialize EPD, if on Lilygo board
if board_type == "lilygo":

    spi = busio.SPI(clock=board.D14, MOSI=board.D11)  # Uses SCK and MOSI
    epd_cs = None #board.D15
    epd_dc = board.D16
    epd_reset = microcontroller.pin.GPIO47  # Set to None for FeatherWing
    epd_busy = microcontroller.pin.GPIO48  # Set to None for FeatherWing

    print("Init Epaper")

    display_bus = FourWire(spi, command=epd_dc, chip_select=epd_cs, reset=epd_reset, baudrate=1000000)

    # For issues with display not updating top/bottom rows correctly set colstart to 8, 0, or -8
    display = adafruit_ssd1680.SSD1680(
        display_bus,
        width=250,
        height=122,
        busy_pin=epd_busy,
        highlight_color=0xFF0000,
        rotation=270,
        #colstart=-8,  # Comment out for older displays
    )

    g = displayio.Group()
    r = rect.Rect(x=0, y=0, width=250, height=122, fill=0xFFFFFF)
    row1 = label.Label(terminalio.FONT, text="Lora / Wifi bridge", x=10, y=10, color=0x000000)
    row2 = label.Label(terminalio.FONT, text="status display", x=10, y=40, color=0x000000)
    row3 = label.Label(terminalio.FONT, text="in 5 minutes", x=10, y=70, color=0x000000)
    row4 = label.Label(terminalio.FONT, text="", x=10, y=100, color=0x000000)
    g.append(r)
    g.append(row1)
    g.append(row2)
    g.append(row3)
    g.append(row4)
    display.root_group = g

    display.refresh()

# WiFi connection
def connect_wifi():
    """Connect to WiFi network"""
    print(f"Connecting to WiFi: {config['wifi']['ssid']}...")
    try:
        wifi.radio.connect(config['wifi']['ssid'], config['wifi']['password'])
        print("Connected to WiFi!")
        print(f"IP address: {wifi.radio.ipv4_address}")
        return True
    except Exception as e:
        print(f"WiFi connection failed: {e}")
        return False

# HTTP request setup
pool = socketpool.SocketPool(wifi.radio)
requests = requests.Session(pool, ssl.create_default_context())
server_url = f"http://{config['server']['host']}:{config['server']['port']}{config['server']['endpoint']}"

def send_to_server(packet_data):
    """Send LoRa packet to server via HTTP POST"""
    response = None
    try:
        # Encode packet data as hexadecimal string
        packet_hex = binascii.hexlify(packet_data).decode('ascii')

        # Prepare JSON payload
        payload = {
            "packet_data": packet_hex
        }

        # Send HTTP POST request
        print(f"Sending to server: {server_url}")
        response = requests.post(server_url, json=payload, timeout=3)

        if response.status_code == 200:
            result = response.json()
            print(f"Server response: {result}")
            return True
        else:
            print(f"Server error: {response.status_code}")
            return False

    except Exception as e:
        print(f"HTTP request failed: {e}")
        return False

def send_keepalive():
    """Send keepalive packet to server"""
    response = None
    try:
        payload = {"keepalive": True}
        response = requests.post(server_url, json=payload, timeout=3)
        if response.status_code == 200:
            print("✓ Keepalive sent")
            return True
        else:
            print(f"✗ Keepalive failed: {response.status_code}")
            return False

    except Exception as e:
        print(f"✗ Keepalive error: {e}")
        return False

# Statistics
packets_received = 0
packets_sent = 0
start_time = time.monotonic()

def print_stats():
    """Print statistics"""
    uptime = time.monotonic() - start_time
    print(f"- Stats (uptime: {uptime:.0f}s) -")
    print(f"Packets received: {packets_received}")
    print(f"Packets forwarded: {packets_sent}")
    if packets_received > 0:
        print(f"Forward success rate: {(packets_sent/packets_received)*100:.1f}%")
    if board_type == "lilygo":
        row2.text = f"Uptime: {uptime:.0f}s"
        row3.text = f"Packets received: {packets_received}"
        row4.text = f"Packets forwarded: {packets_sent}"
        display.refresh()

# Connect to WiFi on startup
wifi_connected = connect_wifi()

print("=== WiFi Bridge Ready ===")
print(f"Board type: {board_type}")
print(f"LoRa: {config['lora']['frequency']:.1f}MHz, SF{config['lora']['spreading_factor']}, BW{config['lora']['bandwidth']}")
if wifi_connected:
    print(f"Server: {server_url}")
else:
    print("WiFi not connected - packets will be dropped")

# Main receive loop
stats_timer = time.monotonic()
wifi_check_timer = time.monotonic()
keepalive_timer = 0 # immediately send the first keepalive

try:
    while True:
        try:
            # Check WiFi connection periodically
            if time.monotonic() - wifi_check_timer > 60:
                if not wifi.radio.connected:
                    print("WiFi disconnected, reconnecting...")
                    wifi_connected = connect_wifi()
                wifi_check_timer = time.monotonic()

            # Send keepalive every 60 seconds
            if time.monotonic() - keepalive_timer > 60:
                if wifi_connected and wifi.radio.connected:
                    send_keepalive()
                keepalive_timer = time.monotonic()

            # Print stats every 5 minutes
            if time.monotonic() - stats_timer > 300:
                print_stats()
                stats_timer = time.monotonic()
                gc.collect()  # Clean up memory

            # Listen for LoRa packets (with timeout to allow keepalives)
            packet, state = lora.recv(timeout_en=True, timeout_ms=15000)

            if packet and len(packet) > 0:
                led.value = False
                packets_received += 1
                packet_hex = binascii.hexlify(packet).decode()
                print(f"LoRa RX ({len(packet)} bytes): {packet_hex}")

                # Forward to server if WiFi connected
                if wifi_connected and wifi.radio.connected:
                    success = send_to_server(packet)
                    if success:
                        packets_sent += 1
                        print("✓ Packet forwarded to server")
                    else:
                        print("✗ Failed to forward packet")
                else:
                    print("✗ WiFi not connected - packet dropped")
                led.value = True

        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(1)

except KeyboardInterrupt:
    print("\nShutting down WiFi Bridge...")
    print_stats()
