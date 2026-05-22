/*
  ePBR Arduino Uno Controller

  Serial commands:
    i    -> return device ID
    t    -> return current temperature
    j    -> return current jacket temperature    
    axx    -> accelerate temperature: output PWM to BTS7960 (a00 to turn OFF)
    d    -> turn ON the fans
    k    -> turn OFF the fans
    lxx  -> set LED intensity at D6 to xx percent (00..99)
    rxx  -> set the round LED intensity at D9 to xx percent (00..99)
    c    -> turn ON air valve relay
    v    -> turn OFF air valve relay

  Notes:
  - Supports both newline-terminated commands and short immediate commands.
  - Replies with one line per command.
  - Temperature conversion uses a configurable linear calibration model.
*/

#include <Arduino.h>
#include <ctype.h>
#include <string.h>
#include <stdlib.h>

// =========================
// User configuration
// =========================
const char DEVICE_ID[] = "EPBR01";

// Pin assignments
const uint8_t PIN_TEMP = A0;
const uint8_t PIN_JACKET_TEMP = A1;
const uint8_t PIN_L_EN = 4;
const uint8_t PIN_R_EN = 8;
const uint8_t PIN_FAN = 7;
const uint8_t PIN_LPWM = 3;
const uint8_t PIN_RPWM = 5;
const uint8_t PIN_LED_PWM   = 6;
const uint8_t PIN_RLED_PWM   = 9;
// Do not use D0/D1 on an Uno while using USB Serial; they are RX/TX.
const uint8_t PIN_AIR_RLY   = 2;


// Relay polarity
// Change these if your relay board is active LOW
const bool RELAY_ON  = HIGH;
const bool RELAY_OFF = LOW;

// Serial settings
const unsigned long SERIAL_BAUD = 115200;

// Temperature calibration
// A0 is designed to read 2.5V at 25C.
// With 5V ADC reference on Uno, that is about 512 ADC counts.
const float TEMP_REF_C           = 25.0f;
const float ADC_REF_AT_25C       = 512.0f;

// IMPORTANT:
// Set this based on your actual analog front-end calibration.
// Units: degrees C per ADC count.
// Example placeholder only:
const float TEMP_SLOPE_C_PER_COUNT = 0.10f;

// Number of ADC samples to average
const uint8_t TEMP_AVG_SAMPLES = 16;

// Command buffer
char cmdBuffer[16];
uint8_t cmdIndex = 0;

// State tracking
bool airValveOn = false;
uint8_t ledPercent = 0;
uint8_t rledPercent = 0;

// =========================
// Function declarations
// =========================
void setAirValve(bool on);
void setLedPercent(uint8_t percent);
void setRLedPercent(uint8_t percent);

uint16_t readTemperatureADC();
float readTemperatureC();
uint16_t readJacketTemperatureADC();
float readJacketTemperatureC();

void processCommand(const char *cmd);
void handleSerial();
void resetCommandBuffer();

bool isImmediateSingleCharCommand(char c);
bool isCompleteLedCommand(const char *buf, uint8_t len);
bool isCompleteRLedCommand(const char *buf, uint8_t len);
bool isCompleteAccelerateCommand(const char *buf, uint8_t len);

// =========================
// Setup
// =========================
void setup() {
  pinMode(PIN_L_EN, OUTPUT);
  pinMode(PIN_R_EN, OUTPUT);
  pinMode(PIN_FAN, OUTPUT);
  pinMode(PIN_LPWM, OUTPUT);
  pinMode(PIN_RPWM, OUTPUT);
  pinMode(PIN_LED_PWM, OUTPUT);
  pinMode(PIN_RLED_PWM, OUTPUT);
  pinMode(PIN_AIR_RLY, OUTPUT);

  // Safe startup state
  setAirValve(false);
  setLedPercent(0);
  setRLedPercent(0);
  digitalWrite(PIN_L_EN, HIGH);
  digitalWrite(PIN_R_EN, HIGH);
  digitalWrite(PIN_FAN, LOW);
  delay(100);


  Serial.begin(SERIAL_BAUD);

  resetCommandBuffer();

  // Optional startup banner for debugging
  Serial.println("READY");
}

// =========================
// Main loop
// =========================
void loop() {
  handleSerial();
}

// =========================
// Output control functions
// =========================
void setAirValve(bool on) {
  digitalWrite(PIN_AIR_RLY, on ? RELAY_ON : RELAY_OFF);
  airValveOn = on;
}

void setLedPercent(uint8_t percent) {
  if (percent > 99) percent = 99;
  ledPercent = percent;

  // Map 0..99% to 0..255 PWM
  uint8_t pwmValue = map(percent, 0, 99, 0, 255);
  analogWrite(PIN_LED_PWM, pwmValue);
}

void setRLedPercent(uint8_t percent) {
  if (percent > 99) percent = 99;
  rledPercent = percent;

  // Map 0..99% to 0..255 PWM
  uint8_t pwmValue = map(percent, 0, 99, 0, 255);
  analogWrite(PIN_RLED_PWM, pwmValue);
}

// =========================
// Temperature functions
// =========================
uint16_t readTemperatureADC() {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < TEMP_AVG_SAMPLES; i++) {
    sum += analogRead(PIN_TEMP);
  }
  return (uint16_t)(sum / TEMP_AVG_SAMPLES);
}

float readTemperatureC() {
  uint16_t adc = readTemperatureADC();

  // Linear calibration model:
  // T = Tref + (ADC - ADCref) * slope
  float tempC = TEMP_REF_C + ((float)adc - ADC_REF_AT_25C) * TEMP_SLOPE_C_PER_COUNT;
  return tempC;
}

uint16_t readJacketTemperatureADC() {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < TEMP_AVG_SAMPLES; i++) {
    sum += analogRead(PIN_JACKET_TEMP);
  }
  return (uint16_t)(sum / TEMP_AVG_SAMPLES);
}

float readJacketTemperatureC() {
  uint16_t adc = readJacketTemperatureADC();

  // Linear calibration model:
  // T = Tref + (ADC - ADCref) * slope
  float tempC = TEMP_REF_C + ((float)adc - ADC_REF_AT_25C) * TEMP_SLOPE_C_PER_COUNT;
  return tempC;
}

// =========================
// Serial command handling
// =========================
void handleSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    // Ignore leading whitespace/newlines
    if ((c == '\r' || c == '\n') && cmdIndex == 0) {
      continue;
    }

    // End-of-line command termination
    if (c == '\r' || c == '\n') {
      cmdBuffer[cmdIndex] = '\0';
      if (cmdIndex > 0) {
        processCommand(cmdBuffer);
      }
      resetCommandBuffer();
      continue;
    }

    // Add character if buffer has room
    if (cmdIndex < sizeof(cmdBuffer) - 1) {
      cmdBuffer[cmdIndex++] = c;
      cmdBuffer[cmdIndex] = '\0';
    } else {
      Serial.println("ERR:CMD_TOO_LONG");
      resetCommandBuffer();
      continue;
    }

    // Immediate handling for single-char commands: i t j c v k d
    if (cmdIndex == 1 && isImmediateSingleCharCommand(cmdBuffer[0])) {
      processCommand(cmdBuffer);
      resetCommandBuffer();
      continue;
    }

    // Immediate handling for lxx without waiting for newline
    if (isCompleteLedCommand(cmdBuffer, cmdIndex)) {
      processCommand(cmdBuffer);
      resetCommandBuffer();
      continue;
    }


    // Immediate handling for rxx without waiting for newline
    if (isCompleteRLedCommand(cmdBuffer, cmdIndex)) {
      processCommand(cmdBuffer);
      resetCommandBuffer();
      continue;
    }

    // Immediate handling for axx without waiting for newline
    if (isCompleteAccelerateCommand(cmdBuffer, cmdIndex)) {
      processCommand(cmdBuffer);
      resetCommandBuffer();
      continue;
    }  
  }
}

bool isImmediateSingleCharCommand(char c) {
  return (c == 'i' || c == 't' || c == 'j' || c == 'c' || c == 'v' || c == 'k' || c == 'd');
}

bool isCompleteLedCommand(const char *buf, uint8_t len) {
  return (len == 3 &&
          buf[0] == 'l' &&
          isdigit((unsigned char)buf[1]) &&
          isdigit((unsigned char)buf[2]));
}

bool isCompleteRLedCommand(const char *buf, uint8_t len) {
  return (len == 3 &&
          buf[0] == 'r' &&
          isdigit((unsigned char)buf[1]) &&
          isdigit((unsigned char)buf[2]));
}

bool isCompleteAccelerateCommand(const char *buf, uint8_t len) {
  return (len == 3 &&
          buf[0] == 'a' &&
          isdigit((unsigned char)buf[1]) &&
          isdigit((unsigned char)buf[2]));
}

void processCommand(const char *cmd) {
  if (strcmp(cmd, "i") == 0) {
    Serial.println(DEVICE_ID);
    return;
  }

  if (strcmp(cmd, "t") == 0) {
    float tempC = readTemperatureC();
    Serial.println(tempC, 2);
    return;
  }

  if (strcmp(cmd, "j") == 0) {
    float tempC = readJacketTemperatureC();
    Serial.println(tempC, 2);
    return;
  }

  if (strcmp(cmd, "c") == 0) {
    setAirValve(true);
    Serial.println("OK:AIR_ON");
    return;
  }

  if (strcmp(cmd, "v") == 0) {
    setAirValve(false);
    Serial.println("OK:AIR_OFF");
    return;
  }

  if (strcmp(cmd, "k") == 0) { 
    digitalWrite(PIN_FAN, LOW);
    Serial.println("OK:FAN_OFF");
    return;
  }

  if (strcmp(cmd, "d") == 0) {
    digitalWrite(PIN_FAN, HIGH);
    Serial.println("OK:FAN_ON");
    return;
  }

  // Heating command: axx
  // -- 'a' command: set LPWM value --------------------------
    // Convert the substring after 's' to an integer
  if (strlen(cmd) == 3 && cmd[0] == 'a' &&
      isdigit((unsigned char)cmd[1]) &&
      isdigit((unsigned char)cmd[2])) {

    int percent = (cmd[1] - '0') * 10 + (cmd[2] - '0');

    if (percent < 0 || percent > 99) {
      Serial.println("ERR:THERMAL_OUT_OF_RANGE");
      return;
    }
    uint8_t pwmValue = map(percent, 0, 99, 0, 255);
    //analogWrite(PIN_RPWM, 0);
    digitalWrite(PIN_FAN, LOW);
    delay(100);
    analogWrite(PIN_LPWM, int(pwmValue));
    delay(100);

    Serial.print("OK:THERMAL_ACC=");
    if (percent < 10) Serial.print('0');
    Serial.println(percent);
    return;
  }


  // // Heating command: dxx
  // // -- 'd' command: set RPWM value --------------------------
  //   // Convert the substring after 's' to an integer
  // if (strlen(cmd) == 3 && cmd[0] == 'd' &&
  //     isdigit((unsigned char)cmd[1]) &&
  //     isdigit((unsigned char)cmd[2])) {

  //   int percent = (cmd[1] - '0') * 10 + (cmd[2] - '0');

  //   if (percent < 0 || percent > 99) {
  //     Serial.println("ERR:THERMAL_OUT_OF_RANGE");
  //     return;
  //   }
  //   uint8_t pwmValue = map(percent, 0, 99, 0, 255);
  //   analogWrite(PIN_LPWM, 0);
  //   delay(100);
  //   digitalWrite(PIN_FAN, HIGH);
  //   //analogWrite(PIN_RPWM, int(pwmValue));
  //   delay(100);

  //   Serial.print("OK:THERMAL_DEACC=");
  //   if (percent < 10) Serial.print('0');
  //   Serial.println(percent);
  //   return;
  // }

  // LED command: lxx
  if (strlen(cmd) == 3 && cmd[0] == 'l' &&
      isdigit((unsigned char)cmd[1]) &&
      isdigit((unsigned char)cmd[2])) {

    int percent = (cmd[1] - '0') * 10 + (cmd[2] - '0');

    if (percent < 0 || percent > 99) {
      Serial.println("ERR:LED_RANGE");
      return;
    }

    setLedPercent((uint8_t)percent);
    Serial.print("OK:LED=");
    if (percent < 10) Serial.print('0');
    Serial.println(percent);
    return;
  }

  // RLED command: rxx
  if (strlen(cmd) == 3 && cmd[0] == 'r' &&
      isdigit((unsigned char)cmd[1]) &&
      isdigit((unsigned char)cmd[2])) {

    int percent = (cmd[1] - '0') * 10 + (cmd[2] - '0');

    if (percent < 0 || percent > 99) {
      Serial.println("ERR:RLED_RANGE");
      return;
    }

    setRLedPercent((uint8_t)percent);
    Serial.print("OK:RLED=");
    if (percent < 10) Serial.print('0');
    Serial.println(percent);
    return;
  }

  Serial.println("ERR:UNKNOWN_CMD");
}

void resetCommandBuffer() {
  cmdIndex = 0;
  cmdBuffer[0] = '\0';
}
