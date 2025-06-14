from flask import Flask, jsonify
import smbus2
import time
import threading
import requests
import json
import os
import math
import hmac
import hashlib
import base64

# Sensor Configuration
BME280_ADDRESS = 0x77
BME280_REGISTER_CHIPID = 0xD0
BME280_REGISTER_CTRL_HUM = 0xF2
BME280_REGISTER_CTRL_MEAS = 0xF4
BME280_REGISTER_CONFIG = 0xF5
BME280_REGISTER_TEMP = 0xFA
BME280_REGISTER_PRESS = 0xF7
BME280_REGISTER_HUM = 0xFD

# Calibration registers
BME280_REGISTER_DIG_T1 = 0x88
BME280_REGISTER_DIG_H1 = 0xA1
BME280_REGISTER_DIG_H2 = 0xE1

# Tuya IoT Configuration for India Region
TUYA_CLIENT_ID = os.getenv("TUYA_CLIENT_ID", "your_client_id")
TUYA_CLIENT_SECRET = os.getenv("TUYA_CLIENT_SECRET", "your_client_secret")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID", "your_device_id")
TUYA_ENDPOINT = "https://openapi.tuyain.com"  # India region endpoint
TUYA_ACCESS_TOKEN = None
TUYA_TOKEN_EXPIRY = 0

# Data Point IDs (must match Tuya configuration)
TUYA_TEMP_DP_ID = "101"     # Temperature
TUYA_HUMID_DP_ID = "102"    # Humidity
TUYA_HEAT_DP_ID = "103"     # Heat Index

# I2C bus
bus = smbus2.SMBus(1)

# Flask app
app = Flask(__name__)

# Global variables
sensor_data = {
    "temperature": None,
    "humidity": None,
    "heat_index": None
}
last_tuya_update = 0
tuya_update_interval = 300  # 5 minutes

def generate_signature(client_id, client_secret, timestamp, access_token=None, body=None):
    """
    Generate HMAC-SHA256 signature for Tuya API (India region)
    Format: client_id + access_token + timestamp for signing
    """
    # Create string to sign
    string_to_sign = client_id
    if access_token:
        string_to_sign += access_token
    string_to_sign += str(timestamp)
    
    # Create HMAC-SHA256 signature
    sign = hmac.new(
        client_secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256
    ).hexdigest().upper()  # Tuya India requires uppercase hex digest
    
    return sign

def read_unsigned_short(bus, address, register, little_endian=True):
    """Safely read an unsigned 16-bit value"""
    try:
        data = bus.read_i2c_block_data(address, register, 2)
        if little_endian:
            return data[0] + (data[1] << 8)
        else:
            return (data[0] << 8) | data[1]
    except Exception as e:
        print(f"Error reading unsigned short: {str(e)}")
        return 0

def read_signed_short(bus, address, register, little_endian=True):
    """Safely read a signed 16-bit value"""
    try:
        data = bus.read_i2c_block_data(address, register, 2)
        if little_endian:
            val = data[0] + (data[1] << 8)
        else:
            val = (data[0] << 8) | data[1]
        return val if val < 32768 else val - 65536
    except Exception as e:
        print(f"Error reading signed short: {str(e)}")
        return 0

def bme280_init(bus, address):
    """Initialize BME280 sensor"""
    try:
        chip_id = bus.read_byte_data(address, BME280_REGISTER_CHIPID)
        if chip_id != 0x60:
            print(f"Invalid chip ID 0x{chip_id:02x}, expected 0x60")
            return False
        
        # Reset the device
        bus.write_byte_data(address, 0xE0, 0xB6)
        time.sleep(0.5)
        
        # Configure humidity: oversampling x1
        bus.write_byte_data(address, BME280_REGISTER_CTRL_HUM, 0x01)
        time.sleep(0.1)
        
        # Configure control measurement
        bus.write_byte_data(address, BME280_REGISTER_CTRL_MEAS, 0x23)  # 00100011
        
        # Configure standby time and filter
        bus.write_byte_data(address, BME280_REGISTER_CONFIG, 0x00)
        time.sleep(0.5)
        return True
    except Exception as e:
        print(f"Error initializing BME280: {str(e)}")
        return False

def read_calibration_data(bus, address):
    """Read calibration data"""
    try:
        # Temperature calibration
        dig_T1 = read_unsigned_short(bus, address, BME280_REGISTER_DIG_T1)
        dig_T2 = read_signed_short(bus, address, BME280_REGISTER_DIG_T1 + 2)
        dig_T3 = read_signed_short(bus, address, BME280_REGISTER_DIG_T1 + 4)
        
        # Humidity calibration
        dig_H1 = bus.read_byte_data(address, BME280_REGISTER_DIG_H1)
        dig_H2 = read_signed_short(bus, address, BME280_REGISTER_DIG_H2)
        dig_H3 = bus.read_byte_data(address, BME280_REGISTER_DIG_H2 + 2)
        
        # Read H4 and H5
        e4 = bus.read_byte_data(address, BME280_REGISTER_DIG_H2 + 3)
        e5 = bus.read_byte_data(address, BME280_REGISTER_DIG_H2 + 4)
        e6 = bus.read_byte_data(address, BME280_REGISTER_DIG_H2 + 5)
        
        dig_H4 = (e4 << 4) | (e5 & 0x0F)
        dig_H5 = (e6 << 4) | (e5 >> 4)
        
        dig_H6 = bus.read_byte_data(address, BME280_REGISTER_DIG_H2 + 6)
        if dig_H6 > 127:
            dig_H6 -= 256
        
        print("Calibration Data:")
        print(f"  T: T1={dig_T1}, T2={dig_T2}, T3={dig_T3}")
        print(f"  H: H1={dig_H1}, H2={dig_H2}, H3={dig_H3}, H4={dig_H4}, H5={dig_H5}, H6={dig_H6}")
        
        return {
            "T": (dig_T1, dig_T2, dig_T3),
            "H": (dig_H1, dig_H2, dig_H3, dig_H4, dig_H5, dig_H6)
        }
    except Exception as e:
        print(f"Error reading calibration data: {str(e)}")
        return {
            "T": (27504, 26435, -1000),  # Default calibration values
            "H": (75, 360, 0, 300, 50, 30)
        }

def read_raw_data(bus, address):
    """Read raw sensor data"""
    try:
        data = bus.read_i2c_block_data(address, BME280_REGISTER_PRESS, 8)
        press_raw = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
        temp_raw = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
        hum_raw = (data[6] << 8) | data[7]
        return temp_raw, press_raw, hum_raw
    except Exception as e:
        print(f"Error reading raw data: {str(e)}")
        return 0, 0, 0

def compensate_temperature(raw_temp, calib_T):
    """Temperature compensation"""
    try:
        dig_T1, dig_T2, dig_T3 = calib_T
        var1 = ((raw_temp / 16384.0) - (dig_T1 / 1024.0)) * dig_T2
        var2 = (((raw_temp / 131072.0) - (dig_T1 / 8192.0)) ** 2) * dig_T3
        t_fine = int(var1 + var2)
        temperature = t_fine / 5120.0
        return temperature, t_fine
    except Exception as e:
        print(f"Error compensating temperature: {str(e)}")
        return 25.0, 0

def compensate_humidity(raw_hum, calib_H, t_fine):
    """Humidity compensation"""
    try:
        dig_H1, dig_H2, dig_H3, dig_H4, dig_H5, dig_H6 = calib_H
        var_h = t_fine - 76800.0
        
        if var_h != 0:
            var_h = (raw_hum - (dig_H4 * 64.0 + dig_H5 / 16384.0 * var_h)) * (
                dig_H2 / 65536.0 * (1.0 + dig_H6 / 67108864.0 * var_h * (
                    1.0 + dig_H3 / 67108864.0 * var_h)))
            var_h = var_h * (1.0 - dig_H1 * var_h / 524288.0)
            humidity = max(0.0, min(100.0, var_h))
        else:
            humidity = 0.0
        
        return humidity
    except Exception as e:
        print(f"Error compensating humidity: {str(e)}")
        return 50.0

def calculate_heat_index(temperature, humidity):
    """Calculate heat index"""
    try:
        if temperature < 26.0:
            return temperature
        
        # NOAA heat index formula
        T = temperature
        R = humidity
        hi = (0.363445176 + 0.988622465*T + 0.008184780*R + 
              0.000144105*T*R - 0.000054777*T**2 - 0.00121227*R**2 + 
              0.000038646*T**2*R + 0.000029039*T*R**2 - 0.00000187*T**2*R**2)
        return hi
    except Exception as e:
        print(f"Error calculating heat index: {str(e)}")
        return temperature

def get_tuya_token():
    """Get Tuya API access token (India region)"""
    global TUYA_ACCESS_TOKEN, TUYA_TOKEN_EXPIRY
    try:
        t_ms = int(time.time() * 1000)
        url = f"{TUYA_ENDPOINT}/v1.0/token?grant_type=1"
        
        # Generate signature for token request (no access token)
        signature = generate_signature(
            client_id=TUYA_CLIENT_ID,
            client_secret=TUYA_CLIENT_SECRET,
            timestamp=t_ms
        )
        
        headers = {
            "client_id": TUYA_CLIENT_ID,
            "sign": signature,
            "sign_method": "HMAC-SHA256",
            "t": str(t_ms)
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("success", False):
                TUYA_ACCESS_TOKEN = data["result"]["access_token"]
                TUYA_TOKEN_EXPIRY = time.time() + data["result"]["expire_time"] - 60
                print("Tuya token obtained successfully")
                return True
        
        print(f"Tuya token error: {response.text}")
        return False
    except Exception as e:
        print(f"Error getting Tuya token: {str(e)}")
        return False

def send_to_tuya(temperature, humidity, heat_index):
    """Send data to Tuya Cloud (India region)"""
    global TUYA_ACCESS_TOKEN, TUYA_TOKEN_EXPIRY
    
    try:
        # Refresh token if needed
        if not TUYA_ACCESS_TOKEN or time.time() > TUYA_TOKEN_EXPIRY:
            if not get_tuya_token():
                return False
        
        # Get current timestamp in milliseconds
        t_ms = int(time.time() * 1000)
        
        # Prepare values (multiply by 10 to preserve decimal)
        commands = [
            {"code": TUYA_TEMP_DP_ID, "value": int(temperature * 10)},
            {"code": TUYA_HUMID_DP_ID, "value": int(humidity * 10)},
            {"code": TUYA_HEAT_DP_ID, "value": int(heat_index * 10)}
        ]
        
        # Send commands
        url = f"{TUYA_ENDPOINT}/v1.0/devices/{TUYA_DEVICE_ID}/commands"
        
        # Generate signature for device command
        signature = generate_signature(
            client_id=TUYA_CLIENT_ID,
            client_secret=TUYA_CLIENT_SECRET,
            timestamp=t_ms,
            access_token=TUYA_ACCESS_TOKEN
        )
        
        headers = {
            "client_id": TUYA_CLIENT_ID, 
            "access_token": TUYA_ACCESS_TOKEN,
            "sign_method": "HMAC-SHA256",
            "t": str(t_ms),
            "sign": signature
        }
        payload = {"commands": commands}
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                print(f"Data sent to Tuya: Temp={temperature:.1f}°C, Hum={humidity:.1f}%, HI={heat_index:.1f}°C")
                return True
        
        print(f"Tuya send error: {response.text}")
        return False
    except Exception as e:
        print(f"Error sending to Tuya: {str(e)}")
        return False

def update_sensor_data():
    global sensor_data, last_tuya_update
    
    try:
        # Initialize and calibrate sensor
        if not bme280_init(bus, BME280_ADDRESS):
            print("BME280 initialization failed")
            sensor_data = {k: None for k in sensor_data}
            return
            
        calib_data = read_calibration_data(bus, BME280_ADDRESS)
        
        while True:
            # Read raw data
            temp_raw, _, hum_raw = read_raw_data(bus, BME280_ADDRESS)
            
            # Skip invalid readings
            if temp_raw in [0x80000, 0xFFFFF] or hum_raw in [0x8000, 0xFFFF]:
                print("Error: Invalid sensor reading")
                time.sleep(1)
                continue
                
            # Compensate temperature
            temperature, t_fine = compensate_temperature(temp_raw, calib_data["T"])
            
            # Compensate humidity
            humidity = compensate_humidity(hum_raw, calib_data["H"], t_fine)
            
            # Calculate heat index
            heat_index = calculate_heat_index(temperature, humidity)
            
            # Update global data
            sensor_data = {
                "temperature": temperature,
                "humidity": humidity,
                "heat_index": heat_index
            }
            
            print(f"Temp: {temperature:.2f}°C, Hum: {humidity:.2f}%, HI: {heat_index:.2f}°C")
            
            # Send to Tuya periodically
            current_time = time.time()
            if current_time - last_tuya_update >= tuya_update_interval:
                if send_to_tuya(temperature, humidity, heat_index):
                    last_tuya_update = current_time
                else:
                    print("Will retry Tuya update later")
            
            time.sleep(2)
    except Exception as e:
        print(f"Error in sensor thread: {str(e)}")
        sensor_data = {k: None for k in sensor_data}

@app.route("/", methods=["GET"])
def get_sensor_data():
    if sensor_data["temperature"] is not None:
        return jsonify({
            "status": "success",
            "data": {
                "temperature": f"{sensor_data['temperature']:.2f} °C",
                "humidity": f"{sensor_data['humidity']:.2f} %",
                "heat_index": f"{sensor_data['heat_index']:.2f} °C"
            },
            "tuya_device": TUYA_DEVICE_ID,
            "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_tuya_update))
        })
    else:
        return jsonify({"status": "error", "message": "Sensor data not available"}), 500

def main():
    threading.Thread(target=update_sensor_data, daemon=True).start()
    app.run(host="0.0.0.0", port=5004, debug=False)

if __name__ == "__main__":
    main()