# Network Switch Configurator

Automated SSH-based network switch configuration tool with built-in rollback support for Aruba CX OS and Cisco IOS XE platforms.

## Features

- **Automatic Rollback Protection**: Uses checkpoint auto (Aruba CX) or configure terminal revert (Cisco IOS XE) to automatically revert changes if not confirmed within 2 minutes
- **Multi-Vendor Support**: Works with both Aruba CX and Cisco IOS XE switches
- **Bulk Configuration**: Configure multiple switches from a CSV file
- **Prompt Handling**: Automatically handle interactive prompts during configuration
- **Session Logging**: Saves detailed logs for each switch configuration session
- **Secure Credential Input**: Password is masked during input

## Installation

1. Clone this repository or download the files

2. Install required Python packages:
```bash
pip install -r requirements.txt
```

Or install manually:
```bash
pip install netmiko paramiko
```

## File Structure

```
Network-Switch-Configurator/
├── switch_configurator.py    # Main program
├── switch.csv                 # List of switches to configure
├── commands.txt               # Commands to execute on switches
├── prompthandling.txt         # Prompt response configuration
└── requirements.txt           # Python dependencies
```

## Configuration Files

### 1. switch.csv

CSV file containing switch information with the following columns:

- `switchname`: Switch hostname (for identification and logging)
- `ip address`: IP address for SSH connection
- `vendor`: Operating system vendor (aruba for Aruba CX, cisco for Cisco IOS XE)
- `switchesinstack`: Number indicating switch position in stack

Example:
```csv
switchname,ip address,vendor,switchesinstack
BNAGOSWGSGDN-L2-1,10.127.19.68,aruba,1
BNAGOSWTS-G-DeltaMezz,10.127.19.198,cisco,1
```

### 2. commands.txt

Text file with commands to execute, one per line. Lines starting with `#` are treated as comments.

Example:
```
# VLAN Configuration
vlan 100
name VLAN_100_Data
exit

# Interface Configuration
interface GigabitEthernet1/0/1
description Access Port
switchport mode access
exit
```

### 3. prompthandling.txt

Configuration for handling interactive prompts. Format: `command_keyword | response`

Special responses:
- `RETURN` or `ENTER`: Sends carriage return
- `YES`: Sends 'yes' followed by enter
- `NO`: Sends 'no' followed by enter
- Any other text: Sends that text followed by enter

Example:
```
reload|YES
copy running-config|RETURN
overwrite|YES
```

## Usage

### Basic Usage

1. Edit `switch.csv` with your switch information
2. Edit `commands.txt` with the commands you want to execute
3. (Optional) Edit `prompthandling.txt` for prompt handling
4. Run the program:

```bash
python3 switch_configurator.py
```

5. Enter your SSH credentials when prompted
6. Review the configuration summary and confirm to proceed
7. After commands are executed, you have 2 minutes to confirm or the changes will auto-revert

### Execution Flow

#### For Aruba CX Switches:
1. Program executes `checkpoint auto 2` to create a checkpoint with 2-minute auto-revert
2. Commands are executed
3. User is prompted to confirm changes
4. If confirmed: `checkpoint confirm` saves the configuration
5. If not confirmed: Configuration auto-reverts after 2 minutes

#### For Cisco IOS XE Switches:
1. Program executes `configure terminal revert timer 2` to enter config mode with revert timer
2. Commands are executed
3. User is prompted to confirm changes
4. If confirmed: `configure confirm` and `write memory` save the configuration
5. If not confirmed: Configuration auto-reverts after 2 minutes

### Output Files

The program generates the following files:

- `session_<hostname>.log`: Detailed SSH session log for each switch
- `output_<hostname>_<timestamp>.log`: Command output for each switch configuration

## Example Session

```
============================================================
Network Switch Configurator
Automated SSH Configuration with Rollback Support
============================================================

Please enter SSH credentials:
Username: admin
Password: ********

Loading configuration files...
[INFO] Loaded 2 switches from switch.csv
[INFO] Loaded 5 commands from commands.txt
[INFO] Loaded 3 prompt handlers from prompthandling.txt

============================================================
CONFIGURATION SUMMARY
============================================================
Switches to configure: 2
Commands to execute: 5
Prompt handlers: 3
============================================================

Proceed with configuration? (yes/no): yes

============================================================
Connecting to aruba-switch-01 (192.168.1.10) - OS: aruba_cx
============================================================
[INFO] Establishing SSH connection...
[SUCCESS] Connected to aruba-switch-01
[INFO] Setting up checkpoint with 2-minute auto-revert...
[INFO] Checkpoint created - configuration will auto-revert in 2 minutes if not confirmed
[INFO] Executing 5 commands...
  -> vlan 100
  -> name VLAN_100_Data
  -> exit

============================================================
CONFIGURATION APPLIED
============================================================
The configuration will auto-revert in 2 minutes.
Do you want to CONFIRM the changes? (yes/no): yes
[SUCCESS] Configuration confirmed and saved!
[INFO] Full output saved to output_aruba-switch-01_20231214_153045.log
[INFO] Disconnected from aruba-switch-01

============================================================
FINAL RESULTS
============================================================
[SUCCESS] aruba-switch-01
============================================================

Completed: 1/1 switches configured successfully
```

## Safety Features

1. **Automatic Rollback**: All configurations automatically revert if not confirmed within 2 minutes
2. **Session Logging**: Complete SSH session logs for troubleshooting
3. **Confirmation Prompt**: User must explicitly confirm changes
4. **Error Handling**: Robust error handling for connection and authentication failures
5. **Pre-execution Summary**: Review what will be configured before execution

## Supported Platforms

- **Aruba CX OS**: Uses `checkpoint auto` for automatic rollback
- **Cisco IOS XE**: Uses `configure terminal revert timer` for automatic rollback

## Troubleshooting

### Connection Timeout
- Verify IP addresses in `switch.csv`
- Ensure switches are reachable (try `ping <ip_address>`)
- Check firewall rules for SSH (port 22)

### Authentication Failed
- Verify username and password
- Check if account has appropriate privileges
- Ensure SSH is enabled on the switch

### Commands Not Executing
- Check command syntax in `commands.txt`
- Review session logs in `session_<hostname>.log`
- Verify OS type is correctly specified in `switch.csv`

### Configuration Reverted
- This is expected if you don't confirm within 2 minutes
- Re-run the program and confirm changes when prompted

## Security Recommendations

1. Use read-only credentials for testing
2. Keep `switch.csv` secure (contains IP addresses)
3. Review all commands in `commands.txt` before execution
4. Test on non-production switches first
5. Always verify configuration changes before confirming

## License

This tool is provided as-is for network automation purposes.

## Contributing

Contributions are welcome! Please ensure all changes are tested on both Aruba CX and Cisco IOS XE platforms.
