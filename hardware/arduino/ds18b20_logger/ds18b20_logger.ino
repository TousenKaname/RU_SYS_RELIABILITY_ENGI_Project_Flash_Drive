// ds18b20_logger.ino -- temperature logger for the flash-drive test rig.
//
// Reads every DS18B20 probe on one 1-Wire bus and prints one line per probe:
//
//     <ROM code in hex>,<temperature in degrees C>
//
// e.g.  28FF4A1B6C1403C1,31.25
//
// The host-side reader (flashrel.sensors.SerialTemperatureSource) maps each ROM
// code to a drive ID or "ambient". Wiring: probe data line to pin 2, a 4.7 kOhm
// pull-up from pin 2 to 5 V, probe VDD to 5 V, probe GND to GND. Up to ~10 probes
// share the bus. Libraries: OneWire, DallasTemperature (Arduino Library Manager).

#include <OneWire.h>
#include <DallasTemperature.h>

const uint8_t ONE_WIRE_PIN = 2;
const unsigned long SAMPLE_PERIOD_MS = 10000;  // one reading per probe every 10 s

OneWire bus(ONE_WIRE_PIN);
DallasTemperature probes(&bus);

void printRom(const DeviceAddress rom) {
  for (uint8_t i = 0; i < 8; i++) {
    if (rom[i] < 16) Serial.print('0');
    Serial.print(rom[i], HEX);
  }
}

void setup() {
  Serial.begin(9600);
  probes.begin();
  probes.setResolution(12);  // 0.0625 degC steps, 750 ms conversion
}

void loop() {
  unsigned long started = millis();
  probes.requestTemperatures();
  uint8_t count = probes.getDeviceCount();
  for (uint8_t i = 0; i < count; i++) {
    DeviceAddress rom;
    if (!probes.getAddress(rom, i)) continue;
    float celsius = probes.getTempC(rom);
    if (celsius == DEVICE_DISCONNECTED_C) continue;
    printRom(rom);
    Serial.print(',');
    Serial.println(celsius, 2);
  }
  while (millis() - started < SAMPLE_PERIOD_MS) delay(50);
}
