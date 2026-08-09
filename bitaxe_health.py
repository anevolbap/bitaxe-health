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


def info_url(host):
    """Build the info endpoint URL.

    host may include a scheme (use https:// if the miner sits behind a TLS reverse
    proxy). A bare host defaults to plain http, which is all the AxeOS firmware
    serves and is expected on a trusted LAN.
    """
    base = host if "://" in host else f"http://{host}"
    return f"{base}/api/system/info"


def fetch_info(host, timeout):
    """GET /api/system/info. Returns parsed dict.

    Raises urllib.error.URLError / OSError on network failure, or ValueError on
    a non-200 status or bad JSON.
    """
    url = info_url(host)
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
        return {"status": "healthy", "failures": [], "last_alert": 0, "streak": 0}


def save_state(path, state):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
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


def next_state(prev, healthy, keys, now, alerting):
    """Pure decision core. Returns (new_state, action).

    action is one of "none", "alarm", "recover". Debounce via fail_streak: an
    unhealthy result must repeat fail_streak times in a row before it alarms, so
    a single missed poll or transient blip does not fire.
    """
    if healthy:
        action = "recover" if (prev.get("status") == "unhealthy"
                               and alerting.get("notify_on_recovery")) else "none"
        return {"status": "healthy", "failures": [], "last_alert": 0, "streak": 0}, action

    fail_streak = alerting.get("fail_streak", 1)
    streak = prev.get("streak", 0) + 1
    state = {"failures": keys, "streak": streak}

    if streak < fail_streak:
        # Not enough consecutive failures yet: stay quiet, keep prior alarm status.
        state["status"] = prev.get("status", "healthy")
        state["last_alert"] = prev.get("last_alert", 0)
        return state, "none"

    if should_alert(prev, keys, now, alerting["realert_hours"]):
        state["status"] = "unhealthy"
        state["last_alert"] = now
        return state, "alarm"

    state["status"] = "unhealthy"
    state["last_alert"] = prev.get("last_alert", 0)
    return state, "none"


def send_ntfy(ntfy, title, body, priority=None):
    url = f"{ntfy['server'].rstrip('/')}/{ntfy['topic']}"
    headers = {"Title": title, "Priority": priority or ntfy.get("priority", "default")}
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status // 100 != 2:
            raise ValueError(f"ntfy HTTP {resp.status}")


def send_heartbeat(hb):
    """Ping a dead-man's-switch URL so a stopped or crashed monitor gets noticed.

    Called only after a check cycle completes, so if the script never runs (cron
    off, bad config, crash) the external monitor stops seeing pings and alarms.
    A failed ping is logged, not fatal.
    """
    url = hb.get("ping_url") if hb else None
    if not url:
        return
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except (urllib.error.URLError, OSError) as e:
        print(f"heartbeat ping failed: {e}", file=sys.stderr)


def _try_ntfy(ntfy, title, body, priority=None):
    """Send a push. Return True on success; log and return False on failure."""
    try:
        send_ntfy(ntfy, title, body, priority=priority)
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"ntfy send failed: {e}", file=sys.stderr)
        return False


def _push(ntfy, title, body, priority, now, prev):
    """Send an alarm push; return the new last_alert (now on success, else old)."""
    return now if _try_ntfy(ntfy, title, body, priority) else prev.get("last_alert", 0)


def run_checks(config):
    dev = config["device"]
    ntfy = config["ntfy"]
    alerting = config["alerting"]
    state_path = os.path.expanduser(alerting["state_file"])
    now = int(time.time())
    prev = load_state(state_path)

    # Fetch. A failure here is its own alarm (subject to the same debounce).
    try:
        info = fetch_info(dev["host"], dev.get("timeout_seconds", 5))
    except (urllib.error.URLError, OSError, ValueError) as e:
        body = f"Bitaxe unreachable at {dev['host']}: {e}"
        state, action = next_state(prev, False, ["unreachable"], now, alerting)
        if action == "alarm":
            state["last_alert"] = _push(ntfy, "Bitaxe DOWN", body, "urgent", now, prev)
            print(f"UNREACHABLE (alarm): {e}", file=sys.stderr)
        else:
            print(f"UNREACHABLE (streak {state['streak']}, no push): {e}", file=sys.stderr)
        save_state(state_path, state)
        return UNREACHABLE

    failures = evaluate(info, config)

    if not failures:
        state, action = next_state(prev, True, [], now, alerting)
        if action == "recover" and not _try_ntfy(
                ntfy, "Bitaxe recovered", "All checks back to normal.", "default"):
            # Recovery push failed: keep the unhealthy state so it retries next run.
            save_state(state_path, prev)
        else:
            save_state(state_path, state)
        print(f"OK: healthy. hashRate_10m={info.get('hashRate_10m')} temp={info.get('temp')} "
              f"freq={info.get('frequency')} coreV={info.get('coreVoltage')}")
        return OK

    keys = [k for k, _ in failures]
    body = "\n".join(m for _, m in failures)
    one_line = body.replace(chr(10), "; ")
    state, action = next_state(prev, False, keys, now, alerting)

    if action == "alarm":
        state["last_alert"] = _push(ntfy, "Bitaxe alarm", body, None, now, prev)
        print(f"UNHEALTHY (alarm): {one_line}", file=sys.stderr)
    elif state["status"] == "unhealthy":
        print(f"UNHEALTHY (throttled): {one_line}", file=sys.stderr)
    else:
        print(f"UNHEALTHY (streak {state['streak']}, no push): {one_line}", file=sys.stderr)

    save_state(state_path, state)
    return UNHEALTHY


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bitaxe health check")
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    code = run_checks(config)
    # Only reached if the run completed without an unhandled exception, so a
    # crashing script stops pinging and the dead-man's-switch fires.
    send_heartbeat(config.get("heartbeat", {}))
    return code


if __name__ == "__main__":
    sys.exit(main())
