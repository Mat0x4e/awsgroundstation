# Design — IoT Sensor Dashboard

## Overview

This document describes the technical architecture, component design, data flows, and deployment model for the IoT sensor dashboard system.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  RASPBERRY PI 4 (192.168.1.44)                                      │
│                                                                     │
│  ┌──────────────┐    Zigbee     ┌─────────────────────────────┐    │
│  │ Aqara        │ ────────────► │ Sonoff ZBDongle-P           │    │
│  │ WSDCGQ11LM   │  868 MHz      │ TI CC2652P / ZStack3x0      │    │
│  │ (Séjour)     │               │ /dev/ttyUSB0  (CP210x)      │    │
│  └──────────────┘               └──────────────┬──────────────┘    │
│                                                │ serial             │
│                                 ┌──────────────▼──────────────┐    │
│                                 │ Zigbee2MQTT 2.12.0           │    │
│                                 │ /opt/zigbee2mqtt             │    │
│                                 │ data: /var/www/html/plugins/ │    │
│                                 │       zigbee2mqtt/data/z2m/  │    │
│                                 └──────────────┬──────────────┘    │
│                                                │ MQTT pub/sub       │
│                                 ┌──────────────▼──────────────┐    │
│                                 │ Mosquitto broker             │    │
│                                 │ 192.168.1.44:1883            │    │
│                                 └──────────────┬──────────────┘    │
│                                                │                    │
│                                 ┌──────────────▼──────────────┐    │
│                                 │ Jeedom 4.0                   │    │
│                                 │ Plugin ZigbeeLinker (z2m)    │    │
│                                 │ Plugin Script                │    │
│                                 │ Scénario DataForwarding      │    │
│                                 └──────────────┬──────────────┘    │
│                                                │ exec               │
│                                 ┌──────────────▼──────────────┐    │
│                                 │ dataforwarding.PY            │    │
│                                 │ awsiotsdk / mTLS             │    │
│                                 └──────────────┬──────────────┘    │
└────────────────────────────────────────────────┼────────────────────┘
                                                 │ MQTT over TLS
                                                 │ port 8883
┌────────────────────────────────────────────────▼────────────────────┐
│  AWS (eu-west-1)                                                     │
│                                                                      │
│  ┌─────────────────────┐                                            │
│  │ IoT Core             │  topic: device/1111/data                  │
│  │ Endpoint: au3t0j...  │                                           │
│  └──────────┬──────────┘                                            │
│             │ IoT Rule: jourdan_iot_input                            │
│             │ SELECT temperature, humidity, pressure                 │
│             │ FROM 'device/+/data'                                   │
│             │                                                        │
│  ┌──────────▼──────────┐                                            │
│  │ DynamoDB             │  Table: jourdan_iot_input                  │
│  │                      │  PK: sample_time (N) + device_id (N)      │
│  │                      │  GSI: device_index                        │
│  │                      │       device_id (H) + sample_time (R)     │
│  └──────────┬──────────┘                                            │
│             │ Query GSI                                              │
│  ┌──────────▼──────────┐                                            │
│  │ Lambda               │  iot-dashboard-api                        │
│  │ Python 3.14          │  128 MB / 10s                             │
│  │ IAM: lambda-role     │                                           │
│  └──────────┬──────────┘                                            │
│             │ AWS_PROXY integration                                  │
│  ┌──────────▼──────────┐                                            │
│  │ API Gateway REST     │  GET /prod/data                           │
│  │ ID: 8nmnzlkwz7       │  ?limit=N&device_id=N                     │
│  └──────────┬──────────┘                                            │
│             │ fetch()                                                │
│  ┌──────────▼──────────┐                                            │
│  │ S3 Static Website    │  iot-dashboard-966292187375               │
│  │ index.html           │  Chart.js 4.4 / date-fns adapter          │
│  └─────────────────────┘                                            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Component Design

### Zigbee Layer

**Coordinator** : Sonoff ZBDongle-P, TI CC2652P, firmware ZStack3x0 (revision 20210708). USB bridge CP210x — the "Silicon Labs" label in the USB device descriptor refers to this bridge chip, **not** the Zigbee radio. The adapter type must be configured as `zstack` in Zigbee2MQTT.

**Sensor** : Aqara WSDCGQ11LM (Zigbee spec revision pre-21). Reports on value change (threshold ~0.5°C for temperature) or approximately every 50 minutes. Battery: CR2032. Direct range to coordinator recommended — does not re-attach reliably through third-party routers.

**Zigbee2MQTT configuration** (abbreviated):
```yaml
serial:
  adapter: zstack
  port: /dev/serial/by-id/usb-Silicon_Labs_Sonoff_Zigbee_3.0_USB_Dongle_Plus_0001-if00-port0
advanced:
  channel: 20
  network_key: [61, 148, 184, 187, 177, 104, 195, 106, 67, 41, 37, 62, 96, 150, 210, 136]
  pan_id: 31144
device_options: {}          # MUST be object, not []
devices:
  "0x00158d008c4b2402":
    friendly_name: AqaraTS-Séjour-202606
```

**Known issue** : Jeedom regenerates `configuration.yaml` on daemon restart and may reset `device_options` to `[]`, causing Z2M to refuse startup with `device_options must be object`. The correct value (`{}`) must be preserved. ModemManager must be disabled (`systemctl disable ModemManager`) to prevent serial port interference on startup.

---

### Forwarding Layer

**Trigger** : Jeedom scenario `DataForwarding` (ID 2), mode Provoked, event `#[Séjour][AqaraTS-Séjour-202606][Température]#`.

**Script invocation** :
```
/var/www/html/plugins/script/data/dataforwarding.PY AqTS #13# #15# #14#
```
Arguments map to: `sensor=AqTS`, `temperature=#13#`, `pressure=#15#`, `humidity=#14#`.

**MQTT payload published to IoT Core**:
```json
{"temperature": "32.36", "humidity": "45.14", "pressure": "1005.4"}
```
Values are currently typed as strings. The Lambda converts them to float at read time.

**TLS certificates** : stored in `/home/admin/scripts/certificates/`, readable by `www-data` (directory permissions 755, files 644).

---

### DynamoDB Schema

```
Table: jourdan_iot_input
├── sample_time  (N)  HASH   ← millisecond Unix timestamp
├── device_id    (N)  RANGE  ← 1111 for this device
└── device_data  (M)         ← Map: {temperature: {S}, humidity: {S}, pressure: {S}}

GSI: device_index
├── device_id    (N)  HASH
├── sample_time  (N)  RANGE
└── Projection: ALL
```

**Provisioned capacity** : 1 RCU / 1 WCU on both table and GSI. A full-table Scan with filter consumes ~76 RCUs and triggers throttling — always use the GSI for read queries.

**Data filter** : items with `sample_time < 1735689600000` are test data from 2023 and are excluded at query time by the Lambda.

---

### Lambda Design

**Handler** : `lambda_function.lambda_handler`

**Logic flow**:
1. Parse `queryStringParameters` → `limit` (default 200), `device_id` (default 1111)
2. Query GSI `device_index` with `device_id = N AND sample_time >= 1735689600000`
3. `ScanIndexForward=False` → most recent first, then `reversed()` to restore chronological order
4. For each item: convert `sample_time` (ms) → ISO 8601, cast `device_data` fields to float
5. Return JSON with CORS headers

**Timestamp conversion**:
```python
ts_seconds = sample_time / 1000 if sample_time > 1e11 else sample_time
dt = datetime.fromtimestamp(ts_seconds, tz=timezone.utc).isoformat()
```

**IAM permissions** (inline policy `dynamodb-read-iot`):
```json
{
  "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"],
  "Resource": [
    "arn:aws:dynamodb:eu-west-1:966292187375:table/jourdan_iot_input",
    "arn:aws:dynamodb:eu-west-1:966292187375:table/jourdan_iot_input/index/*"
  ]
}
```

---

### API Gateway

**Type** : REST API (not HTTP API — chosen for compatibility with existing `AWS_PROXY` integration pattern)

**Resource** : `/data`

**Methods**:
- `GET` → Lambda proxy integration
- `OPTIONS` → MOCK integration for CORS preflight

**CORS response headers**:
```
Access-Control-Allow-Origin:  *
Access-Control-Allow-Methods: GET, OPTIONS
Access-Control-Allow-Headers: Content-Type, X-Amz-Date, Authorization, X-Api-Key
```

**Stage** : `prod`

---

### Frontend

**Technology** : Vanilla HTML/JS, no build step.

**Dependencies** (CDN):
- `chart.js@4.4.0`
- `chartjs-adapter-date-fns@3.0.0`

**Chart configuration** : time-series line charts, x-axis type `time`, date-fns adapter for tick formatting (`HH:mm` for hours, `dd/MM` for days).

**Auto-refresh** : `setInterval(loadData, 5 * 60 * 1000)` — every 5 minutes.

**API call** :
```javascript
fetch(`${API_URL}?limit=${limit}&device_id=1111`)
```

---

## File Structure

```
/home/admin/
├── scripts/
│   └── certificates/
│       ├── raspi4-1-certificate.pem.crt
│       ├── raspi4-1-private.pem.key
│       ├── raspi4-1-public.pem.key
│       └── root.pem
└── iot-dashboard/
    ├── deploy.sh
    ├── README.md
    ├── lambda/
    │   └── lambda_function.py
    └── frontend/
        └── index.html

/var/www/html/plugins/script/data/
└── dataforwarding.PY

/var/www/html/plugins/zigbee2mqtt/data/zigbee2mqtt/
├── configuration.yaml
├── coordinator_backup.json
├── database.db
└── log/
    └── <date>/
        └── log.log

/opt/zigbee2mqtt/           ← Z2M application code
    └── index.js
```

---

## Deployment

All AWS resources are deployed via AWS CLI. See `deploy.sh` for the complete sequence. Manual step required post-deploy: add Lambda invoke permission for API Gateway.

```bash
aws lambda add-permission \
  --function-name iot-dashboard-api \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:eu-west-1:966292187375:8nmnzlkwz7/*/GET/data" \
  --region eu-west-1
```

---

## Known Limitations & Future Work

| Item | Current state | Improvement |
|------|--------------|-------------|
| DynamoDB value types | Strings (`"S"`) | Store as Numbers (`"N"`) for native aggregation |
| CORS | `Allow-Origin: *` | Restrict to S3 bucket URL |
| API auth | None | API Key or Cognito |
| S3 | HTTP only | CloudFront + HTTPS |
| Retour d'état volets | N/A | Jeedom scenario polling `zigbee2mqtt/<device>/get` every 5 min |
| Multi-device | Hardcoded `device_id=1111` | Dynamic device selector in frontend |
| Historical data | 2023 data excluded by filter | Clean delete of pre-2026 items from DynamoDB |
