# Temperature logger

USB flash drives report no temperature, so each drive housing carries a DS18B20
digital probe, plus one probe in free air per host. An Arduino Uno (the department's
kits) reads all probes on one 1-Wire bus and prints one line per probe every 10 s:

```
28FF4A1B6C1403C1,31.25
```

## Wiring

| DS18B20 lead | Arduino |
| --- | --- |
| VDD (red) | 5 V |
| GND (black) | GND |
| DQ (yellow) | pin 2, with a 4.7 kΩ pull-up resistor to 5 V |

All probes share the same three wires (parallel). Tape each probe flat on a drive
housing with Kapton tape; keep the ambient probe away from the drives and hubs.

## Software

1. Install the **OneWire** and **DallasTemperature** libraries in the Arduino IDE and
   upload `arduino/ds18b20_logger/ds18b20_logger.ino`.
2. Open the serial monitor (9600 baud), touch one probe at a time, and note which ROM
   code warms up. Record the mapping in `configs/probes.yaml`
   (see `configs/probes.example.yaml`).
3. Start the test with the logger attached:
   `flashrel run configs/phase1.yaml --host W --temperature-port COM5 --probe-map configs/probes.yaml`
   (`pip install -e ".[sensors]"` adds pyserial; on macOS the port looks like
   `/dev/cu.usbmodem1101`).

Readings are written every 5 min to `<data_dir>/<campaign>/temperature-<host>.csv`.
