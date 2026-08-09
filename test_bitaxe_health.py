"""Tests for the pure check core. No network needed."""

import copy
import json
import os

import bitaxe_health as bh

ALERTING = {"realert_hours": 6, "notify_on_recovery": True, "fail_streak": 2}

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


HEALTHY_STATE = {"status": "healthy", "failures": [], "last_alert": 0, "streak": 0}


def test_debounce_suppresses_first_bad_check():
    # First failure with fail_streak=2: streak 1, no alarm, status stays healthy.
    state, action = bh.next_state(HEALTHY_STATE, False, ["temp"], 1000, ALERTING)
    assert action == "none"
    assert state["streak"] == 1
    assert state["status"] == "healthy"


def test_debounce_alarms_on_second_consecutive_bad_check():
    after_one = {"status": "healthy", "failures": ["temp"], "last_alert": 0, "streak": 1}
    state, action = bh.next_state(after_one, False, ["temp"], 1000, ALERTING)
    assert action == "alarm"
    assert state["streak"] == 2
    assert state["status"] == "unhealthy"
    assert state["last_alert"] == 1000


def test_single_blip_then_recover_does_not_alarm_or_recover():
    # streak 1 (no alarm), then healthy again -> no recover push since never alarmed.
    after_blip = {"status": "healthy", "failures": ["temp"], "last_alert": 0, "streak": 1}
    state, action = bh.next_state(after_blip, True, [], 1000, ALERTING)
    assert action == "none"
    assert state == HEALTHY_STATE


def test_recover_after_real_alarm():
    unhealthy = {"status": "unhealthy", "failures": ["temp"], "last_alert": 900, "streak": 3}
    state, action = bh.next_state(unhealthy, True, [], 1000, ALERTING)
    assert action == "recover"
    assert state == HEALTHY_STATE


def test_realert_throttled_while_unhealthy():
    unhealthy = {"status": "unhealthy", "failures": ["temp"], "last_alert": 1000, "streak": 3}
    state, action = bh.next_state(unhealthy, False, ["temp"], 1000 + 3600, ALERTING)
    assert action == "none"  # within realert_hours, same fault
    assert state["status"] == "unhealthy"


def test_fail_streak_one_alarms_immediately():
    alerting = {"realert_hours": 6, "notify_on_recovery": True, "fail_streak": 1}
    _, action = bh.next_state(HEALTHY_STATE, False, ["temp"], 1000, alerting)
    assert action == "alarm"


def test_save_state_dirless_path_does_not_crash(tmp_path):
    # Regression: os.path.dirname("state.json") == "" once crashed makedirs.
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        bh.save_state("state.json", HEALTHY_STATE)
        with open("state.json") as f:
            assert json.load(f) == HEALTHY_STATE
    finally:
        os.chdir(cwd)


def test_info_url_defaults_to_http_and_respects_scheme():
    assert bh.info_url("192.168.0.139") == "http://192.168.0.139/api/system/info"
    assert bh.info_url("bitaxe.lan") == "http://bitaxe.lan/api/system/info"
    assert bh.info_url("https://bitaxe.lan") == "https://bitaxe.lan/api/system/info"


def _run_config(state_file):
    return {
        "device": {"host": "x"},
        "ntfy": {"server": "https://ntfy.sh", "topic": "t"},
        "alerting": {"state_file": str(state_file), "realert_hours": 6,
                     "notify_on_recovery": True, "fail_streak": 2},
        "expected": CONFIG["expected"],
        "thresholds": CONFIG["thresholds"],
    }


def test_recovery_push_retried_when_send_fails(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(
        {"status": "unhealthy", "failures": ["temp"], "last_alert": 900, "streak": 3}))
    config = _run_config(state_file)
    monkeypatch.setattr(bh, "fetch_info", lambda host, timeout: dict(HEALTHY_INFO))

    def boom(*a, **k):
        raise OSError("no net")

    monkeypatch.setattr(bh, "send_ntfy", boom)
    assert bh.run_checks(config) == bh.OK
    # Recovery send failed, so state must stay unhealthy to retry next run.
    assert json.loads(state_file.read_text())["status"] == "unhealthy"

    # Next run with a working ntfy: recovery sends and state clears to healthy.
    monkeypatch.setattr(bh, "send_ntfy", lambda *a, **k: None)
    assert bh.run_checks(config) == bh.OK
    assert json.loads(state_file.read_text())["status"] == "healthy"
