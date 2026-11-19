{
  "type": "QPSK",
  "qpsk": {
    "carrierFrequencyRecovery": {
      "centerFrequency": {
        "value": ${center_frequency_mhz},
        "units": "MHz"
      },
      "range": {
        "value": 250,
        "units": "kHz"
      }
    },
    "symbolTimingRecovery": {
      "symbolRate": {
        "value": ${symbol_rate_msps},
        "units": "Msps"
      },
      "range": {
        "value": 0.75,
        "units": "ksps"
      },
      "matchedFilter": {
        "type": "ROOT_RAISED_COSINE",
        "rolloffFactor": 0.5
      }
    }
  }
}
