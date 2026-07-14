#!/usr/bin/env python3
"""Bitaxe Gamma health check.

Polls a Bitaxe /api/system/info endpoint, compares telemetry against configured
expected values and thresholds, and pushes an ntfy alarm when something is wrong.
Standard library only. Meant to run from cron every few minutes.
"""

import argparse
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request

# Exit codes
OK = 0
UNHEALTHY = 1
UNREACHABLE = 2


def load_config(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def fetch_info(host, timeout):
    """GET /api/system/info. Returns parsed dict.

    Raises urllib.error.URLError / OSError on network failure, or ValueError on
    a non-200 status or bad JSON.
    """
    url = f"http://{host}/api/system/info"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} from {url}")
        return json.loads(resp.read().decode("utf-8"))


def evaluate(info, config):
    """Pure check core. Returns a list of (key, message) failures.

    `info` is the parsed /api/system/info dict, `config` the loaded config dict.
    No network, no side effects, so this is what the tests exercise.
    """
    exp = config["expected"]
    th = config["thresholds"]
    failures = []

    def fail(key, msg):
        failures.append((key, msg))

    freq = info.get("frequency")
    if freq is not None and freq != exp["frequency"]:
        fail("frequency", f"frequency {freq} != expected {exp['frequency']} MHz")

    cv = info.get("coreVoltage")
    if cv is not None and cv != exp["core_voltage"]:
        fail("core_voltage", f"coreVoltage {cv} != expected {exp['core_voltage']} mV")

    # Hash rate drift, using the stable 10m average. Skip during warmup: the 10m
    # average needs ~10 min of data, and expectedHashrate must be known.
    expected_hr = info.get("expectedHashrate") or 0
    uptime = info.get("uptimeSeconds", 0)
    hr10 = info.get("hashRate_10m")
    if expected_hr > 0 and uptime >= 600 and hr10 is not None:
        floor = expected_hr * th["hashrate_min_pct"] / 100.0
        if hr10 < floor:
            pct = 100.0 * hr10 / expected_hr
            fail(
                "hashrate",
                f"hashRate_10m {hr10:.0f} is {pct:.0f}% of expected {expected_hr} "
                f"(floor {th['hashrate_min_pct']}%)",
            )

    temp = info.get("temp")
    if temp is not None and temp > th["temp_max"]:
        fail("temp", f"temp {temp}C > max {th['temp_max']}C")

    vr = info.get("vrTemp")
    if vr is not None and vr > th["vr_temp_max"]:
        fail("vr_temp", f"vrTemp {vr}C > max {th['vr_temp_max']}C")

    volt = info.get("voltage")
    if volt is not None:
        if volt < th["input_voltage_min"]:
            fail("input_voltage", f"input {volt:.0f}mV < min {th['input_voltage_min']}mV")
        elif volt > th["input_voltage_max"]:
            fail("input_voltage", f"input {volt:.0f}mV > max {th['input_voltage_max']}mV")

    power = info.get("power")
    if power is not None and power > th["power_max"]:
        fail("power", f"power {power:.1f}W > max {th['power_max']}W")

    if info.get("overheat_mode", 0) != 0:
        fail("overheat", "overheat mode active")

    if info.get("miningPaused"):
        fail("paused", "mining paused")

    wifi = info.get("wifiStatus")
    if wifi is not None and wifi != "Connected!":
        fail("wifi", f"wifi status: {wifi}")

    return failures


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"status": "healthy", "failures": [], "last_alert": 0}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


def should_alert(prev, keys, now, realert_hours):
    """Decide whether to push, given previous state and current failing keys.

    Alert on healthy->unhealthy, when a new failure key appears, or when
    realert_hours have elapsed while still unhealthy.
    """
    if prev["status"] == "healthy":
        return True
    prev_keys = set(prev.get("failures", []))
    if set(keys) - prev_keys:
        return True
    return (now - prev.get("last_alert", 0)) >= realert_hours * 3600


def send_ntfy(ntfy, title, body, priority=None):
    url = f"{ntfy['server'].rstrip('/')}/{ntfy['topic']}"
    headers = {"Title": title, "Priority": priority or ntfy.get("priority", "default")}
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status // 100 != 2:
            raise ValueError(f"ntfy HTTP {resp.status}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bitaxe health check")
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    dev = config["device"]
    ntfy = config["ntfy"]
    alerting = config["alerting"]
    state_path = os.path.expanduser(alerting["state_file"])
    now = int(time.time())
    prev = load_state(state_path)

    # Fetch. A failure here is its own alarm.
    try:
        info = fetch_info(dev["host"], dev.get("timeout_seconds", 5))
    except (urllib.error.URLError, OSError, ValueError) as e:
        msg = f"Bitaxe unreachable at {dev['host']}: {e}"
        print(msg, file=sys.stderr)
        keys = ["unreachable"]
        if should_alert(prev, keys, now, alerting["realert_hours"]):
            try:
                send_ntfy(ntfy, "Bitaxe DOWN", msg, priority="urgent")
                now_alert = now
            except (urllib.error.URLError, OSError, ValueError) as ne:
                print(f"ntfy send failed: {ne}", file=sys.stderr)
                now_alert = prev.get("last_alert", 0)
        else:
            now_alert = prev.get("last_alert", 0)
        save_state(state_path, {"status": "unhealthy", "failures": keys, "last_alert": now_alert})
        return UNREACHABLE

    failures = evaluate(info, config)

    if not failures:
        # Healthy now. Notify recovery once if we were unhealthy.
        if prev["status"] == "unhealthy" and alerting.get("notify_on_recovery"):
            try:
                send_ntfy(ntfy, "Bitaxe recovered", "All checks back to normal.", priority="default")
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"ntfy send failed: {e}", file=sys.stderr)
        save_state(state_path, {"status": "healthy", "failures": [], "last_alert": 0})
        print(f"OK: healthy. hashRate_10m={info.get('hashRate_10m')} temp={info.get('temp')} "
              f"freq={info.get('frequency')} coreV={info.get('coreVoltage')}")
        return OK

    keys = [k for k, _ in failures]
    body = "\n".join(m for _, m in failures)
    print(f"UNHEALTHY: {body.replace(chr(10), '; ')}", file=sys.stderr)

    if should_alert(prev, keys, now, alerting["realert_hours"]):
        try:
            send_ntfy(ntfy, "Bitaxe alarm", body)
            last_alert = now
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"ntfy send failed: {e}", file=sys.stderr)
            last_alert = prev.get("last_alert", 0)
    else:
        last_alert = prev.get("last_alert", 0)

    save_state(state_path, {"status": "unhealthy", "failures": keys, "last_alert": last_alert})
    return UNHEALTHY


if __name__ == "__main__":
    sys.exit(main())
