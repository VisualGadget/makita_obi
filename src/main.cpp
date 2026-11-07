// Makita LXT battery interface for the Wemos D1 mini (ESP8266).
// Based on ArduinoOBI from Open Battery Information, Copyright (c) 2024 Martin Jansson, MIT licence.
// The serial frame format stays compatible with the OBI web tool.
//
// Host frame:  01 <data length> <reply length> <command> <data...>
// Reply frame: <command> <reply length> <reply...>
//
// Commands:
//   01  firmware version
//   02  session: data[0] = 1 holds Enable high across frames, 0 releases it
//   03  raw transaction: reset, write data, read reply
//   04  presence check: reply[0] = 1 if the battery answered the bus reset
//   31  F0513 model read
//   32  F0513 version read
//   33  read ROM id, write data, read the rest: reply length includes the 8 ROM bytes
//   CC  skip ROM, write data, read reply
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include "OneWire2.h"

static constexpr uint32_t ENABLE_SETTLE_MS = 400;
static constexpr uint32_t BYTE_GAP_US = 90;
static constexpr uint32_t RESET_GAP_US = 400;
static constexpr uint32_t FRAME_TIMEOUT_MS = 1000;
static constexpr uint32_t SESSION_IDLE_MS = 30000;
static constexpr uint8_t ROM_LENGTH = 8;

static OneWire makita(OBI_OW_PIN);
static bool enableHigh = false;
static bool sessionActive = false;
static uint32_t lastFrameMs = 0;

static void setEnable(bool on) {
    if (on == enableHigh) {
        return;
    }

    digitalWrite(OBI_EN_PIN, on ? HIGH : LOW);
    enableHigh = on;
    if (on) {
        delay(ENABLE_SETTLE_MS);
    }
}

static void writeBytes(const uint8_t *data, uint8_t length) {
    for (uint16_t i = 0; i < length; i++) {
        delayMicroseconds(BYTE_GAP_US);
        makita.write(data[i], 0);
    }
}

static void readBytes(uint8_t *reply, uint8_t length) {
    for (uint16_t i = 0; i < length; i++) {
        delayMicroseconds(BYTE_GAP_US);
        reply[i] = makita.read();
    }
}

static void busReset() {
    makita.reset();
    delayMicroseconds(RESET_GAP_US);
}

static void transactionRom(const uint8_t *data, uint8_t length, uint8_t *reply, uint8_t replyLength) {
    uint8_t rom[ROM_LENGTH];
    busReset();
    makita.write(0x33, 0);
    readBytes(rom, ROM_LENGTH);
    memcpy(reply, rom, replyLength < ROM_LENGTH ? replyLength : ROM_LENGTH);
    writeBytes(data, length);
    if (replyLength > ROM_LENGTH) {
        readBytes(reply + ROM_LENGTH, replyLength - ROM_LENGTH);
    }
}

static void transactionSkip(const uint8_t *data, uint8_t length, uint8_t *reply, uint8_t replyLength) {
    busReset();
    makita.write(0xCC, 0);
    writeBytes(data, length);
    readBytes(reply, replyLength);
}

static void transactionRaw(const uint8_t *data, uint8_t length, uint8_t *reply, uint8_t replyLength) {
    busReset();
    writeBytes(data, length);
    readBytes(reply, replyLength);
}

static void f0513Read(uint8_t command, uint8_t *reply) {
    busReset();
    makita.write(0xCC, 0);
    delayMicroseconds(BYTE_GAP_US);
    makita.write(0x99, 0);
    delay(ENABLE_SETTLE_MS);
    busReset();
    makita.write(command, 0);
    readBytes(reply, 2);
}

static bool readFrameBody(uint8_t *data, uint8_t length) {
    for (uint16_t i = 0; i < length; i++) {
        const uint32_t started = millis();
        while (Serial.available() < 1) {
            if (millis() - started > FRAME_TIMEOUT_MS) {
                return false;
            }

            yield();
        }

        data[i] = Serial.read();
    }

    return true;
}

static void handleFrame() {
    if (Serial.available() < 4) {
        return;
    }

    if (Serial.read() != 0x01) {
        return;
    }

    uint8_t data[255];
    uint8_t reply[257] = {0};
    const uint8_t length = Serial.read();
    uint8_t replyLength = Serial.read();
    const uint8_t command = Serial.read();
    if (!readFrameBody(data, length)) {
        return;
    }

    lastFrameMs = millis();
    uint8_t *body = &reply[2];
    if (command == 0x02) {
        sessionActive = length > 0 && data[0] != 0;
        setEnable(sessionActive);
        body[0] = sessionActive;
    } else if (command == 0x01) {
        body[0] = FIRMWARE_VERSION_MAJOR;
        body[1] = FIRMWARE_VERSION_MINOR;
        body[2] = FIRMWARE_VERSION_PATCH;
    } else {
        setEnable(true);
        switch (command) {
            case 0x03:
                transactionRaw(data, length, body, replyLength);
                break;
            case 0x04:
                body[0] = makita.reset();
                break;
            case 0x31:
            case 0x32:
                f0513Read(command, body);
                break;
            case 0x33:
                transactionRom(data, length, body, replyLength);
                break;
            case 0xCC:
                transactionSkip(data, length, body, replyLength);
                break;
            default:
                replyLength = 0;
                break;
        }
    }

    reply[0] = command;
    reply[1] = replyLength;
    Serial.write(reply, replyLength + 2);
    if (!sessionActive) {
        setEnable(false);
    }
}

void setup() {
    digitalWrite(OBI_EN_PIN, LOW);
    pinMode(OBI_EN_PIN, OUTPUT);
    WiFi.mode(WIFI_OFF);
    Serial.begin(9600);
}

void loop() {
    handleFrame();
    if (sessionActive && millis() - lastFrameMs > SESSION_IDLE_MS) {
        sessionActive = false;
        setEnable(false);
    }
}
