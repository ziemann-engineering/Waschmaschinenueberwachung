#ifndef CONFIG_H
#define CONFIG_H

// ============================================================================
// Aggregator Configuration
// ============================================================================

// Aggregator identification
#define AGGREGATOR_ID           1
#define AGGREGATOR_NAME         "Building_A_Floor_1"

// ============================================================================
// BLE Configuration
// ============================================================================

// Company ID for washing machine sensors (0xFFFF = testing)
#define WASHING_MACHINE_COMPANY_ID  0xFFFF

// Protocol version we accept
#define PROTOCOL_VERSION        1

// BLE scan parameters
#define BLE_SCAN_INTERVAL_MS    100     // How often to scan (ms)
#define BLE_SCAN_WINDOW_MS      100     // Scan window duration (ms)
#define BLE_SCAN_DURATION_SEC   0       // 0 = continuous scanning

// ============================================================================
// LoRa Configuration (for Seeed WIO-SX1262)
// ============================================================================

// SPI pins for XIAO ESP32S3 + WIO-SX1262
// Adjust based on your wiring!
#define LORA_SCK_PIN            8       // D8 - SCK
#define LORA_MISO_PIN           9       // D9 - MISO
#define LORA_MOSI_PIN           10      // D10 - MOSI
#define LORA_CS_PIN             3       // D3 - NSS/CS
#define LORA_RST_PIN            7       // D7 - RST
#define LORA_DIO1_PIN           2       // D2 - DIO1 (interrupt)
#define LORA_BUSY_PIN           6       // D6 - BUSY

// LoRa radio parameters
#define LORA_FREQUENCY          868.0   // MHz (EU ISM band)
#define LORA_BANDWIDTH          125000  // Hz
#define LORA_SPREADING_FACTOR   10      // SF10 for good range
#define LORA_CODING_RATE        5       // 4/5
#define LORA_TX_POWER           14      // dBm (max 14 for EU)
#define LORA_PREAMBLE_LENGTH    8       // symbols
#define LORA_SYNC_WORD          0x12    // Private network

// ============================================================================
// Data Forwarding Configuration
// ============================================================================

// How often to send aggregated data (0 = immediately on receive)
#define FORWARD_INTERVAL_MS     0

// Maximum machines per LoRa packet
#define MAX_MACHINES_PER_PACKET 20

// Timeout for considering a sensor offline (ms)
#define SENSOR_TIMEOUT_MS       120000  // 2 minutes

// ============================================================================
// Debug Configuration
// ============================================================================

#define DEBUG_SERIAL            1       // Enable serial debug output
#define DEBUG_BAUD_RATE         115200

#endif // CONFIG_H
