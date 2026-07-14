# bitaxe-health

Cron health check for a Bitaxe Gamma (BM1370). Polls `/api/system/info` every 5
minutes, compares against expected setpoints and thresholds, and pushes an
[ntfy](https://ntfy.sh) alarm when something drifts. Standard library Python only,
no dependencies.

## What it checks

- `frequency` and `coreVoltage` match the configured setpoints exactly.
- `hashRate_10m` stays above a percentage of the firmware's `expectedHashrate`
  (the 10-minute average is used because instantaneous `hashRate` is noisy).
- `temp`, `vrTemp`, input `voltage`, and `power` stay in range.
- `overheat_mode`, `miningPaused`, and `wifiStatus` are healthy.
- The device answers at all (a timeout or error is an "unreachable" alarm).

The Gamma factory defaults are 490 MHz / 1150 mV. Set `[expected]` in your config
to whatever setpoints your miner actually runs, and update them if you retune.

## Setup

```sh
cp config.example.toml config.toml
# edit config.toml: set the device host (your Bitaxe IP or hostname) and the
# ntfy topic (pick a private, hard-to-guess name)
```

Subscribe to the same topic in the ntfy app (phone or desktop) so alarms reach you.

## Run

```sh
python3 bitaxe_health.py --config config.toml
```

Exit codes: `0` healthy, `1` unhealthy, `2` device unreachable. A one-line status
prints to stdout; alarm detail prints to stderr (which cron mails).

## Alarm behavior

To avoid a push every 5 minutes while a fault persists, state is kept in
`~/.local/state/bitaxe-health/state.json`:

- Push once when the device goes healthy -> unhealthy.
- Push again if a new kind of fault appears, or after `realert_hours` (default 6).
- Push once when it recovers (if `notify_on_recovery`).

## Install the cron job

```sh
./install-cron.sh
```

It prints the crontab line and asks before adding it. Or add it yourself with
`crontab -e`:

```
*/5 * * * * /usr/bin/python3 /path/to/bitaxe_health.py --config /path/to/config.toml >> ~/.local/state/bitaxe-health/cron.log 2>&1
```

## Tests

```sh
python3 -m pytest
```

The check logic (`evaluate`) is a pure function tested with saved sample payloads,
no network needed.
