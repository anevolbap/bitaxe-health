"""Tests for the pure check core. No network needed."""

import copy

import bitaxe_health as bh

# Trimmed /api/system/info payload from a healthy Gamma.
HEALTHY_INFO = {
    "frequency": 525,
    "coreVoltage": 1150,
    "coreVoltageActual": 1144,
    "actualFrequency": 525,
    "voltage": 5296.875,
    "power": 19.4318237,
    "temp": 60,
    "vrTemp": 77,
    "expectedHashrate": 1071,
    "hashRate": 987.84,
    "hashRate_10m": 1076.18,
    "uptimeSeconds": 43913,
    "overheat_mode": 0,
    "miningPaused": False,
    "wifiStatus": "Connected!",
}

CONFIG = {
    "expected": {"frequency": 525, "core_voltage": 1150},
    "thresholds": {
        "hashrate_min_pct": 85,
        "temp_max": 68,
        "vr_temp_max": 90,
        "input_voltage_min": 4900,
        "input_voltage_max": 5500,
        "power_max": 30,
    },
}


def keys(failures):
    return {k for k, _ in failures}


def test_healthy_has_no_failures():
    assert bh.evaluate(HEALTHY_INFO, CONFIG) == []


def test_wrong_frequency():
    info = copy.deepcopy(HEALTHY_INFO)
    info["frequency"] = 490
    assert keys(bh.evaluate(info, CONFIG)) == {"frequency"}


def test_wrong_core_voltage():
    info = copy.deepcopy(HEALTHY_INFO)
    info["coreVoltage"] = 1200
    assert keys(bh.evaluate(info, CONFIG)) == {"core_voltage"}


def test_hot_asic_and_vr():
    info = copy.deepcopy(HEALTHY_INFO)
    info["temp"] = 75
    info["vrTemp"] = 95
    assert keys(bh.evaluate(info, CONFIG)) == {"temp", "vr_temp"}


def test_low_hashrate():
    info = copy.deepcopy(HEALTHY_INFO)
    info["hashRate_10m"] = 800  # 75% of 1071, below 85% floor
    assert keys(bh.evaluate(info, CONFIG)) == {"hashrate"}


def test_warmup_skips_hashrate_check():
    info = copy.deepcopy(HEALTHY_INFO)
    info["hashRate_10m"] = 0
    info["uptimeSeconds"] = 120  # under 600s warmup
    assert bh.evaluate(info, CONFIG) == []


def test_input_voltage_low_and_high():
    low = copy.deepcopy(HEALTHY_INFO)
    low["voltage"] = 4800
    assert keys(bh.evaluate(low, CONFIG)) == {"input_voltage"}
    high = copy.deepcopy(HEALTHY_INFO)
    high["voltage"] = 5600
    assert keys(bh.evaluate(high, CONFIG)) == {"input_voltage"}


def test_power_over_max():
    info = copy.deepcopy(HEALTHY_INFO)
    info["power"] = 35
    assert keys(bh.evaluate(info, CONFIG)) == {"power"}


def test_overheat_and_paused_and_wifi():
    info = copy.deepcopy(HEALTHY_INFO)
    info["overheat_mode"] = 1
    info["miningPaused"] = True
    info["wifiStatus"] = "Disconnected"
    assert keys(bh.evaluate(info, CONFIG)) == {"overheat", "paused", "wifi"}


def test_multiple_failures_aggregate():
    info = copy.deepcopy(HEALTHY_INFO)
    info["frequency"] = 400
    info["temp"] = 80
    assert keys(bh.evaluate(info, CONFIG)) == {"frequency", "temp"}


def test_should_alert_transitions():
    now = 1_000_000
    healthy = {"status": "healthy", "failures": [], "last_alert": 0}
    assert bh.should_alert(healthy, ["temp"], now, 6) is True

    same = {"status": "unhealthy", "failures": ["temp"], "last_alert": now}
    assert bh.should_alert(same, ["temp"], now, 6) is False

    new_fault = {"status": "unhealthy", "failures": ["temp"], "last_alert": now}
    assert bh.should_alert(new_fault, ["temp", "power"], now, 6) is True

    stale = {"status": "unhealthy", "failures": ["temp"], "last_alert": now - 7 * 3600}
    assert bh.should_alert(stale, ["temp"], now, 6) is True
