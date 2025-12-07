# Optris CT Infrared Thermometer readout with MQTT publisher and UI

This code decodes a continuous Optris CT sensor data stream and publishes all values to **MQTT**, and optionally displays a **local curses UI** in your terminal.

The script is hardened for unattended operation, so for continous logging operations. 

The sensor must already be configured to run in **burst mode** with the **burst string**:

```text
Burst-String = [1,4,2,3,5,6]
```
---

**1. Protocol Overview**
1.1 Physical Interface

The script expects the Optris CT to be connected via serial interface:
(e.g. /dev/ttyUSB0, /dev/tty.usbserial-5)
or native RS-232 / RS-485 bridged into a serial device

**Default parameters** are:

Baud rate: **9600**
**8** data bits, **n**o parity, **1** stop bit (***8N1***)

These values match the typical **Optris CT factory serial configuration**.

<br>

**1.2 Burst Mode Data Format**

Once burst mode is enabled in the sensor (outside this script), the Optris CT continuously transmits frames of the form:
```
AA AA  v1_hi v1_lo  v2_hi v2_lo  v3_hi v3_lo  v4_hi v4_lo  v5_hi v5_lo  v6_hi v6_lo  AA AA  v1_hi v1_lo ...
```
Where:
AA AA = sync word (frame start)
v1 … v6 = six 16-bit values (high byte, low byte)
Each 16-bit value is interpreted as:
 Temperature values (Target, Actual, Head, Box)

With the configured burst string [1,4,2,3,5,6], the payload layout of one frame is:

| Position | Meaning             | Type        |
| -------- | ------------------- | ----------- |
| v1       | process_temperature | temperature |
| v2       | actual_temperature  | temperature |
| v3       | head_temperature    | temperature |
| v4       | box_temperature     | temperature |
| v5       | emissivity          | fraction    |
| v6       | transmission        | fraction    |

---

2. MQTT Topics and Payloads

For a configured base topic, e.g.: ```/optris/ct01```
each decoded frame produces payloads with scalar values.

The individual topics are:

```/optris/ct01/process_temperature
/optris/ct01/actual_temperature
/optris/ct01/head_temperature
/optris/ct01/box_temperature
/optris/ct01/emissivity
/optris/ct01/transmission
```


And one JSON aggregate topic:
/optris/ct01

with payload, e.g.:

```
{
  "process_temperature": 22.8,
  "actual_temperature": 22.8,
  "head_temperature": 22.2,
  "box_temperature": 29.3,
  "emissivity": 1.0,
  "transmission": 1.0
}
```

3. Requirements and Installation
3.1 System Requirements

```Python ≥ 3.8```

```pyserial```

```paho-mqtt```

And optional curses support.


**4. Installation**

```pip install pyserial paho-mqtt```


```
python3 optris-ct.py /dev/tty.usbserial-XXX \
  --baudrate 9600 \
  --broker mqtt.yourserver.net \
  --port 1883 \
  --username YOUR_MQTT_USER \
  --password YOUR_MQTT_PASS \
  --topic /optris/ct01
  ```
  
  For a UI add   ```--UI```