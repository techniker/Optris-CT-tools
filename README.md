# Optris CT Infrared Thermometer readout with MQTT publisher and UI

This code decodes a continuous Optris CT sensor data stream and publishes all values to **MQTT**, and optionally displays a **local curses UI** in your terminal.

The script is hardened for unattended operation, so for continous logging operations. 


---

**1. Protocol Overview**
1.1 Physical Interface

The script expects the Optris CT to be connected via serial interface:
(e.g. /dev/ttyUSB0)
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

```
<base-topic>/process_temperature
<base-topic>/actual_temperature
<base-topic>/head_temperature
<base-topic>/box_temperature
<base-topic>/emissivity
<base-topic>/transmission

```


And one JSON aggregate topic:
/optris/ct01

with payload, e.g.:

```
{
  "ts": 1733945812.345,
  "values": {
    "process_temperature": 50.5,
    "actual_temperature": 50.4,
    "head_temperature": 39.3,
    "box_temperature": 21.8,
    "emissivity": 1.0,
    "transmission": 1.0
  }
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

Run:
```
python3 optris-ct.py \
  --serial /dev/ttyUSB0 \
  --mode burst \
  --set-burst \
  --publish-json \
  --mqtt-broker mqtt.yourbroker.net \
  --mqtt-username 'user' \
  --mqtt-password 'password'

```
**5. Program Configuration**

Serial configuration:

| Option        | Description                                               |
| ------------- | --------------------------------------------------------- |
| `--serial`    | Serial device path (e.g. `/dev/ttyUSB0`, `/dev/ttyACM0`)  |
| `--baud`      | Baud rate (default: `9600`)                               |
| `--multidrop` | RS-485 multidrop address `1…79` (optional, default: none) |

Protocol Operation Mode:

| Option   | Description                                            |
| -------- | ------------------------------------------------------ |
| `--mode` | Select read mode: `burst` or `poll` (default: `burst`) |

Burst Configuration:

| Option        | Description                                     |
| ------------- | ----------------------------------------------- |
| `--burst`     | Burst half-byte list (default: `1,4,2,3,5,6`)   |
| `--set-burst` | Program the burst configuration into the sensor |


```
Polling: --mode poll --poll-interval 0.2
```


| Option             | Description                               |
| ------------------ | ----------------------------------------- |
| `--mqtt-broker`    | MQTT broker hostname                      |
| `--mqtt-port`      | MQTT port (default: `1883`)               |
| `--mqtt-username`  | MQTT username                             |
| `--mqtt-password`  | MQTT password                             |
| `--mqtt-topic`     | Base MQTT topic (default: `/optris/ct01`) |
| `--mqtt-client-id` | MQTT client ID                            |
| `--retain`         | Set retain flag on published messages     |

| Option                | Description                            |
| --------------------- | -------------------------------------- |
| `--publish-json`      | Publish all values as one JSON message |
| `--publish-per-field` | Publish each value to its own topic    |
| `--publish-interval`  | Minimum publish interval in seconds    |

| Option        | Description                       |
| ------------- | --------------------------------- |
| `--ui curses` | Full-screen terminal UI (default) |
| `--ui plain`  | Line-based console output         |
| `--ui none`   | No local output                   |
