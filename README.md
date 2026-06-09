# Network Switch Configurator

Rich-formatted SSH automation for bulk switch configuration with timed rollback protection for Aruba AOS-CX and Cisco IOS XE.

## What It Does

- Configures multiple switches concurrently over SSH.
- Uses a professional terminal UI with credential inputs, progress, live status, and a results table.
- Validates `switches.csv` and `commands.txt` before opening device sessions.
- Supports per-switch variables from CSV columns.
- Supports conditional command templates with `#IF`, `#ELSE`, `#ENDIF`, `#FOR`, and `#ENDFOR`.
- Handles selected interactive command prompts from `prompthandling.txt`.
- Writes concise run logs and complete device output to `logs/switch_configurator.log`.

## Platform Behavior

### Aruba AOS-CX

The configurator uses:

```text
checkpoint auto <minutes>
...
checkpoint auto confirm
```

HPE documents `checkpoint auto <TIME-LAPSE-INTERVAL>` as a 1-60 minute temporary checkpoint. If `checkpoint auto confirm` is not entered within the interval, the runtime configuration is restored from the temporary checkpoint.

Source: HPE Aruba AOS-CX command reference for `checkpoint auto`:
https://support.hpe.com/hpesc/public/docDisplay?docId=sd00007458en_us&docLocale=en_US&page=GUID-A18F399E-3CDE-4727-9801-5C62AB5FF791.html

### Cisco IOS XE

The configurator verifies archive support, configures it when missing, then uses:

```text
configure terminal revert timer <minutes>
...
configure confirm
write memory
```

Cisco documents `configure terminal revert timer minutes` as a timed rollback option with a 1-120 minute range, and documents that configuration archive functionality must be enabled before timed rollback can be used. This tool limits the UI rollback timer to 1-60 minutes so a single run remains valid for both Aruba and Cisco inventories.

Sources:

- Cisco command reference for `configure terminal revert`: https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fundamentals/command/Cisco_IOS_Configuration_Fundamentals_Command_Reference/C_commands.html
- Cisco configuration rollback guide: https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/config-mgmt/configuration/15-e/config-mgmt-15-e-book/cm-config-rollback.html

## Installation

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

## Required Files

### `switches.csv`

Copy the example inventory and edit it:

```bash
cp switches.csv.example switches.csv
```

Required columns:

- `switchname`
- `ip address`
- `vendor`

Supported vendor values:

- Aruba AOS-CX: `aruba`, `arubacx`, `aruba_cx`, `aruba_aoscx`
- Explicit Aruba Netmiko aliases: `aruba_os`, `aruba_osswitch`, `aruba_procurve`
- Cisco IOS XE: `cisco`, `ios`, `ios_xe`, `cisco_xe`, `cisco_ios_xe`

Additional columns are available as variables in `commands.txt`.

Example:

```csv
switchname,ip address,vendor,location,mgmt_vlan,stack
CORE-01,10.0.0.1,cisco,Main Data Center,10,2
ACCESS-01,10.0.1.1,aruba,Building A,20,0
```

### `commands.txt`

One command per line. Blank lines and normal comments are ignored.

Variables use CSV column names:

```text
snmp-server location {location}
vlan {mgmt_vlan}
name Management
```

Conditional blocks:

```text
#IF {vendor} == cisco
logging buffered 32768
#ELSE
logging buffered 16384
#ENDIF
```

Loops:

```text
#IF {stack} > 0
#FOR member IN {stack}
interface 1/{member}/48
description Stack member {member} uplink
exit
#ENDFOR
#ENDIF
```

Supported comparison operators:

- `==`
- `!=`
- `>`
- `<`
- `>=`
- `<=`

Supported logical operators:

- `AND`
- `OR`

Delays:

```text
interface 1/1/1
apply fault-monitor profile stormcontrol50
#SLEEP 5
no shutdown
```

`#SLEEP <seconds>` pauses before the next command runs — useful when a device needs a
moment to settle after enabling a feature or applying a profile. Any pending commands are
sent to the device before the pause. Durations may be whole or decimal seconds
(`#SLEEP 5`, `#SLEEP 2.5`), from `0` up to a maximum of 300 seconds; an invalid or
out-of-range value fails before any SSH connection is opened. A `#SLEEP` inside a `#FOR`
loop runs on every iteration. Note that the pause counts against the rollback/checkpoint
window, so keep total sleep time well under your configured rollback timer.

Invalid template syntax fails before SSH connections are opened.

### `prompthandling.txt`

Format:

```text
command_keyword|response
```

Special responses:

- `RETURN` or `ENTER`: sends a carriage return
- `YES`: sends `yes`
- `NO`: sends `no`
- any other value: sends that text

Example:

```text
reload|YES
copy running-config|RETURN
delete|YES
```

## Usage

Run the Rich interface:

```bash
venv/bin/python switch_configurator.py
```

Workflow:

1. Enter SSH username and password.
2. Enter rollback timer in minutes.
3. Select `Run configuration`.
4. Monitor progress, live status, and final per-switch results.

The UI validates local files before starting SSH sessions. If validation fails, no device is touched.

## Logging

Runtime logs are written to:

```text
logs/switch_configurator.log
```

The log file includes:

- file loading and validation status
- switch connection lifecycle
- parsed command counts
- raw device output for archive, checkpoint, confirm, save, and configuration commands
- exceptions and failure details

The `logs/` directory is ignored by git.

## Safety Notes

- Review `commands.txt` before every run.
- Test on non-production switches first.
- Use a rollback timer long enough for the command set and device response times.
- Make sure Cisco devices have sufficient flash storage for archive files.
- Make sure the SSH account has privileges for configuration, archive, checkpoint, confirm, and save commands.

## Development

Run tests:

```bash
venv/bin/python -m unittest discover -s tests -v
```

Compile check:

```bash
venv/bin/python -m compileall -q switch_configurator.py tests/test_switch_configurator.py
```

The test suite covers command template parsing, loader validation, prompt handling, vendor rollback flows, runner result handling, logging setup, and Rich table rendering.
