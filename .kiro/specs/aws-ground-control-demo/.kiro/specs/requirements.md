# Requirements — IoT Sensor Dashboard

## Introduction

This document describes the requirements for a full IoT pipeline that collects environmental data (temperature, humidity, atmospheric pressure) from a Zigbee sensor installed in a residential property, forwards it to AWS, and exposes it as a live web dashboard.

The system is already partially deployed. This spec covers the complete picture — from physical sensor to browser — to enable Kiro to understand, extend, and maintain the codebase.

---

## Requirements

### 1. Data Acquisition (Raspberry Pi)

**1.1** The system MUST acquire temperature, humidity, and atmospheric pressure readings from an Aqara WSDCGQ11LM sensor over Zigbee.

**1.2** The Zigbee coordinator MUST use the ZStack3x0 adapter (`adapter: zstack`) running on a Sonoff ZBDongle-P (TI CC2652P chip, CP210x USB bridge on `/dev/serial/by-id/usb-Silicon_Labs_Sonoff_Zigbee_3.0_USB_Dongle_Plus_0001-if00-port0`).

**1.3** Zigbee2MQTT MUST be configured with `device_options: {}` (object, not array) to pass startup validation.

**1.4** ModemManager MUST be disabled on the Raspberry Pi to prevent serial port interference with the Zigbee adapter.

**1.5** The Zigbee2MQTT daemon MUST be managed by Jeedom (plugin ZigbeeLinker stable v2.12.0) and restart automatically on failure.

**1.6** The sensor MUST be registered in Jeedom under the object **Séjour**, equipment name **AqaraTS-Séjour-202606**, with commands: Température (ID 13), Humidité (ID 14), Pression (ID 15), Batterie (ID 12).

---

### 2. Data Forwarding (Raspberry Pi → AWS IoT Core)

**2.1** A Jeedom scenario named **DataForwarding** MUST trigger on every change of the Température command (ID 13).

**2.2** The scenario MUST invoke the script `/var/www/html/plugins/script/data/dataforwarding.PY` with the arguments:
```
AqTS  <Température>  <Pression>  <Humidité>
```
in that exact order (sensor name, temperature, pressure, humidity).

**2.3** The script MUST connect to AWS IoT Core via mutual TLS using:
- Certificate: `/home/admin/scripts/certificates/raspi4-1-certificate.pem.crt`
- Private key: `/home/admin/scripts/certificates/raspi4-1-private.pem.key`
- Root CA: `/home/admin/scripts/certificates/root.pem`
- Endpoint: `au3t0jmm9e6p9-ats.iot.eu-west-1.amazonaws.com`
- Client ID: `testDevice`

**2.4** The script MUST publish to MQTT topic `device/1111/data` with payload:
```json
{"temperature": "<value>", "humidity": "<value>", "pressure": "<value>"}
```

**2.5** The `awsiotsdk` Python library MUST be installed system-wide (`/usr/local/lib/python3.9/dist-packages/`) so that it is accessible to the `www-data` user (Jeedom's execution context).

**2.6** The script MUST NOT depend on the working directory (all file paths MUST be absolute).

---

### 3. Data Ingestion (AWS IoT Core → DynamoDB)

**3.1** An AWS IoT Rule named `jourdan_iot_input` MUST be active in region `eu-west-1`, matching topic `device/+/data`.

**3.2** The rule SQL statement MUST be:
```sql
SELECT temperature, humidity, pressure FROM 'device/+/data'
```

**3.3** The rule action MUST insert each message into the DynamoDB table `jourdan_iot_input` with:
- `sample_time` (Number) — millisecond Unix timestamp, as hash key
- `device_id` (Number) — extracted from topic segment `/device/<id>/data`, as range key
- `device_data` (Map) — containing temperature, humidity, pressure as String attributes

**3.4** The DynamoDB table MUST have a Global Secondary Index named `device_index` with:
- Hash key: `device_id` (Number)
- Range key: `sample_time` (Number)
- Projection: ALL

---

### 4. Data API (Lambda + API Gateway)

**4.1** An AWS Lambda function named `iot-dashboard-api` MUST be deployed in `eu-west-1` with runtime Python 3.14, 128 MB memory, 10s timeout.

**4.2** The Lambda MUST query DynamoDB using the GSI `device_index` with:
- `device_id` as hash key (default: 1111)
- `sample_time >= 1735689600000` (January 1st 2026 00:00 UTC) to exclude historical test data
- Sort order: ascending (chronological)
- Configurable limit via query parameter (default: 200)

**4.3** The Lambda MUST convert `sample_time` millisecond timestamps to ISO 8601 strings.

**4.4** The Lambda MUST return a JSON response:
```json
{
  "device_id": 1111,
  "count": <N>,
  "data": [
    {
      "timestamp": "2026-06-26T13:43:26+00:00",
      "sample_time": 1782480808569,
      "temperature": 32.36,
      "humidity": 45.14,
      "pressure": 1005.4
    }
  ]
}
```

**4.5** The Lambda response MUST include CORS headers:
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, OPTIONS
```

**4.6** An API Gateway REST API named `iot-dashboard-api` (ID: `8nmnzlkwz7`) MUST expose the Lambda at:
```
GET https://8nmnzlkwz7.execute-api.eu-west-1.amazonaws.com/prod/data
```
with optional query parameters `limit` (integer) and `device_id` (integer).

**4.7** The IAM role `iot-dashboard-lambda-role` MUST grant the Lambda:
- `dynamodb:Query`, `dynamodb:GetItem`, `dynamodb:Scan` on table and all indexes
- `logs:CreateLogGroup`, `logs:CreateLogDelivery`, `logs:PutLogEvents` (via AWSLambdaBasicExecutionRole)

---

### 5. Dashboard Frontend (S3 Static Site)

**5.1** A single-page HTML dashboard MUST be hosted on S3 bucket `iot-dashboard-966292187375` as a static website, accessible at:
```
http://iot-dashboard-966292187375.s3-website-eu-west-1.amazonaws.com
```

**5.2** The dashboard MUST display three time-series charts using Chart.js 4.4+ with the date-fns adapter:
- Temperature (°C) — orange (`#f97316`)
- Humidity (%) — cyan (`#22d3ee`)
- Atmospheric pressure (hPa) — purple (`#a78bfa`)

**5.3** The dashboard MUST display KPI cards showing for each metric: current (latest) value, minimum and maximum over the loaded period.

**5.4** The user MUST be able to select the number of data points to load: 50, 100, or 200.

**5.5** The dashboard MUST auto-refresh every 5 minutes.

**5.6** The dashboard MUST work without any build step or bundler — a single `index.html` file, CDN-hosted Chart.js.

---

### 6. Non-Functional Requirements

**6.1** The entire AWS stack MUST remain within or near AWS Free Tier limits for a single-device, low-frequency setup.

**6.2** The Raspberry Pi stack MUST survive a reboot and restart automatically (Jeedom manages Mosquitto and Zigbee2MQTT via systemd).

**6.3** All AWS resources MUST be deployed in region `eu-west-1` (Ireland), AWS account `966292187375`.

**6.4** The forwarding script MUST be executable by `www-data` without sudo.

**6.5** Sensor data values stored in DynamoDB are currently typed as String (`S`). Numeric conversion is handled by the Lambda at read time.
