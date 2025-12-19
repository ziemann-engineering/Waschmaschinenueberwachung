/*
 * Washing Machine Monitoring System - Aggregator
 * 
 * Hardware: Seeed XIAO ESP32S3 + Seeed WIO-SX1262 LoRa Module
 * 
 * Function:
 *   - Continuously scan for BLE advertisements from sensor nodes
 *   - Parse manufacturer data for vibration readings
 *   - Forward data via LoRa to central server
 */

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <SPI.h>
#include <LoRa.h>
#include "config.h"

// ============================================================================
// Data Structures
// ============================================================================

struct SensorData {
    uint8_t machineId;
    uint16_t rmsX100;           // RMS acceleration × 100
    uint16_t freqX10;           // Dominant frequency × 10
    uint8_t batteryPercent;
    uint8_t flags;
    uint32_t lastSeenMs;
    bool valid;
};

// Store data from up to MAX_MACHINES_PER_PACKET sensors
SensorData sensorCache[MAX_MACHINES_PER_PACKET];
int sensorCount = 0;

// ============================================================================
// BLE Scan Callback
// ============================================================================

class WashingMachineScanCallbacks : public NimBLEScanCallbacks {
    
    void onResult(const NimBLEAdvertisedDevice* advertisedDevice) override {
        // Check for manufacturer data
        if (!advertisedDevice->haveManufacturerData()) {
            return;
        }
        
        std::string mfgData = advertisedDevice->getManufacturerData();
        
        // Minimum length check: company(2) + version(1) + id(1) + rms(2) + freq(2) + batt(1) + flags(1) = 10
        if (mfgData.length() < 10) {
            return;
        }
        
        // Parse manufacturer data
        const uint8_t* data = (const uint8_t*)mfgData.data();
        
        // Check company ID (little-endian)
        uint16_t companyId = data[0] | (data[1] << 8);
        if (companyId != WASHING_MACHINE_COMPANY_ID) {
            return;
        }
        
        // Check protocol version
        uint8_t protocolVersion = data[2];
        if (protocolVersion != PROTOCOL_VERSION) {
            #if DEBUG_SERIAL
            Serial.printf("Unknown protocol version: %d\n", protocolVersion);
            #endif
            return;
        }
        
        // Parse sensor data
        uint8_t machineId = data[3];
        uint16_t rmsX100 = data[4] | (data[5] << 8);
        uint16_t freqX10 = data[6] | (data[7] << 8);
        uint8_t batteryPercent = data[8];
        uint8_t flags = data[9];
        
        #if DEBUG_SERIAL
        Serial.printf("Received from Machine %d: RMS=%.2f m/s², Freq=%.1f Hz, Batt=%d%%\n",
                      machineId,
                      rmsX100 / 100.0,
                      freqX10 / 10.0,
                      batteryPercent);
        #endif
        
        // Update sensor cache
        updateSensorCache(machineId, rmsX100, freqX10, batteryPercent, flags);
        
        // Forward immediately if configured
        #if FORWARD_INTERVAL_MS == 0
        sendLoRaPacket(machineId, rmsX100, freqX10, batteryPercent);
        #endif
    }
    
    void updateSensorCache(uint8_t machineId, uint16_t rmsX100, uint16_t freqX10, 
                          uint8_t batteryPercent, uint8_t flags) {
        // Find existing entry or empty slot
        int slot = -1;
        for (int i = 0; i < MAX_MACHINES_PER_PACKET; i++) {
            if (sensorCache[i].valid && sensorCache[i].machineId == machineId) {
                slot = i;
                break;
            }
            if (!sensorCache[i].valid && slot == -1) {
                slot = i;
            }
        }
        
        if (slot == -1) {
            #if DEBUG_SERIAL
            Serial.println("Warning: Sensor cache full!");
            #endif
            return;
        }
        
        sensorCache[slot].machineId = machineId;
        sensorCache[slot].rmsX100 = rmsX100;
        sensorCache[slot].freqX10 = freqX10;
        sensorCache[slot].batteryPercent = batteryPercent;
        sensorCache[slot].flags = flags;
        sensorCache[slot].lastSeenMs = millis();
        sensorCache[slot].valid = true;
        
        if (slot >= sensorCount) {
            sensorCount = slot + 1;
        }
    }
};

// ============================================================================
// LoRa Functions
// ============================================================================

bool initLoRa() {
    // Configure SPI pins
    SPI.begin(LORA_SCK_PIN, LORA_MISO_PIN, LORA_MOSI_PIN, LORA_CS_PIN);
    
    // Set LoRa pins
    LoRa.setPins(LORA_CS_PIN, LORA_RST_PIN, LORA_DIO1_PIN);
    
    // Initialize LoRa
    if (!LoRa.begin(LORA_FREQUENCY * 1E6)) {
        Serial.println("LoRa init failed!");
        return false;
    }
    
    // Configure LoRa parameters
    LoRa.setSpreadingFactor(LORA_SPREADING_FACTOR);
    LoRa.setSignalBandwidth(LORA_BANDWIDTH);
    LoRa.setCodingRate4(LORA_CODING_RATE);
    LoRa.setTxPower(LORA_TX_POWER);
    LoRa.setPreambleLength(LORA_PREAMBLE_LENGTH);
    LoRa.setSyncWord(LORA_SYNC_WORD);
    
    Serial.println("LoRa initialized successfully");
    Serial.printf("  Frequency: %.1f MHz\n", LORA_FREQUENCY);
    Serial.printf("  SF: %d, BW: %d kHz\n", LORA_SPREADING_FACTOR, LORA_BANDWIDTH / 1000);
    
    return true;
}

void sendLoRaPacket(uint8_t machineId, uint16_t rmsX100, uint16_t freqX10, uint8_t batteryPercent) {
    /*
     * Packet format:
     * Byte 0: Aggregator ID
     * Byte 1: Machine count (1 for single machine)
     * Bytes 2+: Machine data (6 bytes each)
     *   - Byte 0: Machine ID
     *   - Bytes 1-2: RMS × 100 (little-endian)
     *   - Bytes 3-4: Freq × 10 (little-endian)
     *   - Byte 5: Battery %
     */
    
    uint8_t packet[8];
    packet[0] = AGGREGATOR_ID;
    packet[1] = 1;  // Single machine
    packet[2] = machineId;
    packet[3] = rmsX100 & 0xFF;
    packet[4] = (rmsX100 >> 8) & 0xFF;
    packet[5] = freqX10 & 0xFF;
    packet[6] = (freqX10 >> 8) & 0xFF;
    packet[7] = batteryPercent;
    
    #if DEBUG_SERIAL
    Serial.printf("Sending LoRa packet for machine %d...\n", machineId);
    #endif
    
    LoRa.beginPacket();
    LoRa.write(packet, sizeof(packet));
    LoRa.endPacket();
    
    #if DEBUG_SERIAL
    Serial.println("LoRa packet sent");
    #endif
}

void sendAggregatedLoRaPacket() {
    /*
     * Send all cached sensor data in one packet
     */
    
    // Count valid sensors
    int validCount = 0;
    for (int i = 0; i < sensorCount; i++) {
        if (sensorCache[i].valid) {
            // Check if sensor is still alive
            if (millis() - sensorCache[i].lastSeenMs < SENSOR_TIMEOUT_MS) {
                validCount++;
            } else {
                sensorCache[i].valid = false;  // Mark as offline
            }
        }
    }
    
    if (validCount == 0) {
        return;
    }
    
    // Build packet
    uint8_t packet[2 + (MAX_MACHINES_PER_PACKET * 6)];
    packet[0] = AGGREGATOR_ID;
    packet[1] = validCount;
    
    int offset = 2;
    for (int i = 0; i < sensorCount && offset < sizeof(packet) - 6; i++) {
        if (sensorCache[i].valid) {
            packet[offset++] = sensorCache[i].machineId;
            packet[offset++] = sensorCache[i].rmsX100 & 0xFF;
            packet[offset++] = (sensorCache[i].rmsX100 >> 8) & 0xFF;
            packet[offset++] = sensorCache[i].freqX10 & 0xFF;
            packet[offset++] = (sensorCache[i].freqX10 >> 8) & 0xFF;
            packet[offset++] = sensorCache[i].batteryPercent;
        }
    }
    
    #if DEBUG_SERIAL
    Serial.printf("Sending aggregated LoRa packet with %d machines\n", validCount);
    #endif
    
    LoRa.beginPacket();
    LoRa.write(packet, offset);
    LoRa.endPacket();
}

// ============================================================================
// BLE Initialization
// ============================================================================

NimBLEScan* pBLEScan = nullptr;

void initBLE() {
    NimBLEDevice::init("WM_Aggregator");
    
    pBLEScan = NimBLEDevice::getScan();
    pBLEScan->setScanCallbacks(new WashingMachineScanCallbacks(), false);
    pBLEScan->setActiveScan(false);  // Passive scan (less power, no scan response)
    pBLEScan->setInterval(BLE_SCAN_INTERVAL_MS);
    pBLEScan->setWindow(BLE_SCAN_WINDOW_MS);
    pBLEScan->setMaxResults(0);  // Don't store results, use callback only
    
    Serial.println("BLE initialized");
    Serial.printf("  Scan interval: %d ms, window: %d ms\n", 
                  BLE_SCAN_INTERVAL_MS, BLE_SCAN_WINDOW_MS);
}

// ============================================================================
// Setup & Loop
// ============================================================================

void setup() {
    Serial.begin(DEBUG_BAUD_RATE);
    delay(1000);  // Wait for serial
    
    Serial.println("\n========================================");
    Serial.println("Washing Machine Aggregator");
    Serial.printf("ID: %d, Name: %s\n", AGGREGATOR_ID, AGGREGATOR_NAME);
    Serial.println("========================================\n");
    
    // Initialize sensor cache
    memset(sensorCache, 0, sizeof(sensorCache));
    
    // Initialize LoRa
    if (!initLoRa()) {
        Serial.println("FATAL: LoRa initialization failed!");
        while (1) {
            delay(1000);
        }
    }
    
    // Initialize BLE
    initBLE();
    
    // Start BLE scanning
    Serial.println("Starting BLE scan...");
    pBLEScan->start(BLE_SCAN_DURATION_SEC, false);  // 0 = continuous
    
    Serial.println("\nAggregator ready, waiting for sensor data...\n");
}

void loop() {
    // BLE scanning runs in background via callbacks
    
    #if FORWARD_INTERVAL_MS > 0
    // Periodic aggregated forwarding
    static uint32_t lastForward = 0;
    if (millis() - lastForward >= FORWARD_INTERVAL_MS) {
        sendAggregatedLoRaPacket();
        lastForward = millis();
    }
    #endif
    
    // Small delay to prevent watchdog issues
    delay(10);
}
