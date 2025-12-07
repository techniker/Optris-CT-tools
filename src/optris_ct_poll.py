#
# Optris CT Infrared temperature sensor readout in polling mode
# Publish values to MQTT and display them locally
#
# <tec att sixtopia.net> Bjoern Heller
#

#!/usr/bin/env python3

import serial
import time
import argparse
import paho.mqtt.client as mqtt

# Command codes for the values
COMMANDS = {
    "process_temperature": bytes([0x01]),
    "head_temperature": bytes([0x02]),
    "box_temperature": bytes([0x03]),
    "actual_temperature": bytes([0x81]),
    "emissivity": bytes([0x04]),
    "transmission": bytes([0x05]),
}

def read_sensor_data(port, command):
    """Send a command to the sensor and read the response."""
    port.write(command)
    response = port.read(2)  # Read 2 bytes for most responses
    if len(response) == 2:
        return response
    return None

def parse_temperature(response):
    """Parse a 2-byte temperature response."""
    byte1, byte2 = response
    return (byte1 * 256 + byte2 - 1000) / 10

def parse_fractional(response):
    """Parse a 2-byte fractional value."""
    byte1, byte2 = response
    return (byte1 * 256 + byte2) / 1000

def read_and_publish(port, mqtt_client, topic):
    try:
        print(f"Reading data from {port.port} and publishing to MQTT...")
        while True:
            data = {}

            # Read the temperature data fields
            for key, command in COMMANDS.items():
                response = read_sensor_data(port, command)
                if response:
                    if key.endswith("temperature"):
                        data[key] = parse_temperature(response)
                    else:
                        data[key] = parse_fractional(response)

            # Publish data to MQTT
            for key, value in data.items():
                print(f"{key}: {value}")
                mqtt_client.publish(f"{topic}/{key}", value)

            #time.sleep(0.1)  # 100 ms delay
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        print(f"Error: {e}")

def connect_mqtt(broker, port, username, password):
    try:
        client = mqtt.Client(client_id="optris-ct01")
        client.username_pw_set(username, password)
        client.connect(broker, port)
        print(f"Connected to MQTT broker at {broker}:{port}")
        return client
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        exit(1)

if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Read data from Optris CT sensor and send to MQTT topics.")
    parser.add_argument("tty", type=str, help="TTY device (e.g., /dev/ttyUSB0 or COM1)")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate (default: 9600)")
    parser.add_argument("--broker", type=str, default="mqtt.sixtopia.net", help="MQTT broker address (default: mqtt.yourserver.net)")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    parser.add_argument("--username", type=str, required=True, help="MQTT username")
    parser.add_argument("--password", type=str, required=True, help="MQTT password")
    parser.add_argument("--topic", type=str, default="/optris/ct01", help="Base MQTT topic (default: /optris/ct01)")

    args = parser.parse_args()

    # Connect to MQTT
    mqtt_client = connect_mqtt(args.broker, args.port, args.username, args.password)

    # Open serial port
    try:
        with serial.Serial(args.tty, args.baudrate, timeout=1) as ser:
            # Start reading and publishing data
            read_and_publish(ser, mqtt_client, args.topic)
    except serial.SerialException as e:
        print(f"Serial error: {e}")
