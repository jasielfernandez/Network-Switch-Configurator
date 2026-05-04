# Network Switch Configurator

Automated SSH-based network switch configuration tool with built-in rollback support for Aruba CX OS and Cisco IOS XE platforms.

## Features

- **Concurrent Execution**: Configure up to 10 switches simultaneously with multi-threading for fast bulk operations
- **Automatic Rollback Protection**: Uses `checkpoint auto 2` + `checkpoint auto confirm` (Aruba CX) or `configure terminal revert timer 2` (Cisco IOS XE) to automatically revert changes if not confirmed within 2 minutes
- **Multi-Vendor Support**: Works with both Aruba CX and Cisco IOS XE switches
- **Bulk Configuration**: Configure multiple switches from a CSV file with progress tracking
- **Rich CLI Interface**: Beautiful colored output with tables, panels, and progress bars
- **Prompt Handling**: Automatically handle interactive prompts during configuration
- **Variable Substitution**: Use CSV column values in commands with `{variable}` syntax
- **Real Configuration Mode Execution**: Sends configuration commands through Netmiko's config-mode APIs so changes like `hostname test` are actually applied on-device
- **Secure Credential Input**: Password is masked during input

## Installation

1. Clone this repository or download the files

2. Install required Python packages:
```bash
pip install -r requirements.txt
```

Or install manually:
```bash
pip install netmiko paramiko rich
```

## File Structure

```
Network-Switch-Configurator/
├── switch_configurator.py    # Main program
├── switches.csv.example       # Example switch inventory (copy to switches.csv)
├── commands.txt               # Commands to execute on switches
├── prompthandling.txt         # Prompt response configuration
└── requirements.txt           # Python dependencies
```

## Configuration Files

### 1. switches.csv

**Setup:** Copy `switches.csv.example` to `switches.csv` and edit with your switch information.

```bash
cp switches.csv.example switches.csv
```

CSV file containing switch information. Required columns:

- `switchname`: Switch hostname (for identification and logging)
- `ip address`: IP address for SSH connection
- `vendor`: Operating system vendor (aruba for Aruba CX, cisco for Cisco IOS XE)

**Additional Columns (Optional):**
You can add any additional columns to store switch-specific data. These values can be used as variables in your commands using `{column_name}` syntax.

Example:
```csv
switchname,ip address,vendor,location,mgmt_vlan,site_id
SWITCH-CORE-01,192.168.1.1,cisco,Main Data Center,10,DC01
SWITCH-ACC-03,192.168.3.3,aruba,Building C Floor 1,30,BLDG-C
```

### 2. commands.txt

Text file with commands to execute, one per line. Lines starting with `#` are treated as comments.
Configuration commands are sent through the device's configuration mode, so state-changing lines such as `hostname test` are applied instead of being treated like show commands.

**Variable Substitution:**
Use `{column_name}` to insert values from the CSV file. Variables are replaced with switch-specific values from switches.csv.

Example:
```
# VLAN Configuration using variables
vlan {mgmt_vlan}
name Management_VLAN
exit

# Interface Configuration with location variable
interface GigabitEthernet1/0/1
description Access Port at {location}
switchport mode access
exit

# SNMP location from CSV
snmp-server location {location}
```

**Conditional Logic:**

Control command execution with conditional directives and loops. Directives are evaluated before variable substitution.

**IF/ELSE/ENDIF - Conditional Blocks:**
```
#IF {column} == value
  commands executed when condition is true
#ENDIF

#IF {vendor} == cisco
  archive
    path flash:backup
#ELSE
  logging buffered 16384
#ENDIF
```

**FOR/ENDFOR - Loop Blocks:**
```
#FOR variable_name IN {column_or_number}
  commands with {variable_name}
#ENDFOR

#FOR i IN {stack}
  interface 1/{i}/48
  description Uplink from stack member {i}
#ENDFOR
```

**Supported Operators:**
- Comparison: `==`, `!=`, `>`, `<`, `>=`, `<=`
- Logical: `AND`, `OR`

**Example - Stack Configuration:**

Add a `stack` column to switches.csv (0 for standalone, 3 for a 3-member stack):
```csv
switchname,ip address,vendor,stack
CORE-SW-01,10.0.1.1,cisco,0
STACK-SW-02,10.0.2.1,cisco,3
```

In commands.txt:
```
# Only configure stack ports if stack > 0
#IF {stack} > 0
  #FOR member IN {stack}
    interface 1/{member}/48
    description Uplink from stack member {member}
    switchport mode trunk
    exit
  #ENDFOR
#ENDIF
```

For STACK-SW-02 (stack=3), this generates:
```
interface 1/1/48
description Uplink from stack member 1
switchport mode trunk
exit
interface 1/2/48
description Uplink from stack member 2
switchport mode trunk
exit
interface 1/3/48
description Uplink from stack member 3
switchport mode trunk
exit
```

For CORE-SW-01 (stack=0), the entire block is skipped.

**Example - Vendor-Specific Configuration:**
```
# Configure based on vendor
#IF {vendor} == cisco
  archive path flash:backup
  logging buffered 32768
#ENDIF

#IF {vendor} == aruba
  logging buffered 16384
  snmp-server contact Network Team
#ENDIF
```

**Example - Complex Conditions:**
```
# Multiple conditions with AND
#IF {vendor} == cisco AND {location} == Main Data Center
  snmp-server location {location}
  vlan 999
  name Emergency_Management
#ENDIF

# Multiple conditions with OR
#IF {mgmt_vlan} == 10 OR {mgmt_vlan} == 20
  vlan {mgmt_vlan}
  name Management
#ENDIF
```

**Nested Blocks:**

You can nest IF blocks and FOR loops:
```
#IF {vendor} == cisco
  #IF {stack} > 0
    #FOR member IN {stack}
      interface 1/{member}/1
      description Stack member {member}
    #ENDFOR
  #ENDIF
#ENDIF
```

**Notes:**
- Directives are case-sensitive (use uppercase: `#IF`, `#FOR`, etc.)
- Regular comments use lowercase `#` without directive keywords
- Conditions are evaluated before variable substitution
- Missing CSV columns evaluate to empty string (false in comparisons)
- Numeric comparisons use float conversion when possible

### 3. prompthandling.txt

Configuration for handling interactive prompts. Format: `command_keyword | response`
Prompt-handled commands still run in-order with surrounding configuration commands; non-interactive config lines on either side remain batched for efficient delivery.

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

1. Copy the example file and edit with your switch information:
   ```bash
   cp switches.csv.example switches.csv
   ```
2. Edit `switches.csv` with your actual switch information
3. Edit `commands.txt` with the commands you want to execute
4. (Optional) Edit `prompthandling.txt` for prompt handling
5. Run the program:

```bash
python3 switch_configurator.py
```

6. Enter your SSH credentials when prompted
7. Review the configuration summary and confirm to proceed
8. Commands are executed concurrently on up to 10 switches at a time with real-time progress tracking
9. View the final results table showing success/failure status for each switch

### Concurrent Execution

The tool uses multi-threading to configure multiple switches simultaneously:

- **Up to 10 concurrent threads**: Configure up to 10 switches at the same time
- **Automatic thread management**: Thread pool size automatically adjusts based on the number of switches
- **Thread-safe output**: All console output is properly synchronized across threads
- **Real-time progress**: Live progress bar shows configuration status across all switches
- **Parallel speedup**: Configuring 50 switches takes approximately the same time as 10 (instead of 5x longer)

**Example Performance:**
- Sequential (old): 10 switches × 30 seconds = 5 minutes
- Concurrent (new): 10 switches in parallel = ~30 seconds total

### Execution Flow

#### For Aruba CX Switches:
1. **Auto checkpoint setup** (automatic):
   - Program executes `checkpoint auto 2` to create auto checkpoint with 2-minute timer
   - Creates checkpoint named `AUTO<YYYYMMDDHHMMSS>` 
   - If not confirmed within 2 minutes, configuration automatically reverts
2. Commands are executed in configuration mode with variable substitution
3. Configuration is confirmed with `checkpoint auto confirm` to save permanently
4. Changes are committed to the switch

**Important:** The checkpoint must be confirmed within 2 minutes or all changes automatically revert to previous state.

#### For Cisco IOS XE Switches:
1. **Archive check and setup** (automatic if needed):
   - Checks if archive is configured with `show archive`
   - If not configured, automatically sets up archive with:
     ```
     archive
       path flash:archive-config
       maximum 10
     ```
   - Saves archive configuration permanently
2. Program executes `configure terminal revert timer 2` to enter config mode with rollback protection
3. Commands are executed in configuration mode with variable substitution
4. Configuration is automatically confirmed with `configure confirm`
5. Changes are saved with `write memory`

### Prerequisites and Auto-Configuration

#### Cisco IOS XE Archive Feature

The script uses Cisco's configuration rollback feature which requires the archive subsystem. **The script automatically detects and configures archive if needed** - no manual setup required.

**What happens automatically:**
1. Script checks if archive is configured using `show archive`
2. If not configured, automatically sets up archive with:
   ```
   configure terminal
   archive
     path flash:archive-config
     maximum 10
   end
   write memory
   ```
3. Saves the archive configuration to startup-config

**If you want to pre-configure archive manually:**
```
configure terminal
archive
  path flash:archive-config
  maximum 10
end
write memory
```

**Note:** Archive configuration is persistent and only needs to be set up once per switch. Once configured, subsequent script runs will detect it and skip the setup step.

#### Aruba CX Checkpoint Feature

Aruba CX switches have checkpoint functionality built-in and require no additional configuration. The script uses:
- `checkpoint auto 2` - Creates auto checkpoint with 2-minute timer
- `checkpoint auto confirm` - Confirms and saves changes permanently

**How it works:**
1. Auto checkpoint creates a restore point named `AUTO<YYYYMMDDHHMMSS>`
2. You have 2 minutes to apply changes and confirm
3. If `checkpoint auto confirm` is not executed within 2 minutes, configuration automatically reverts
4. Upon confirmation, changes are saved permanently to the running configuration

**No manual setup required** - checkpoint commands work out of the box on Aruba CX OS.

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
[INFO] Loaded 2 switches from switches.csv
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
[INFO] Starting auto checkpoint mode (2-minute timer)...
[INFO] Auto checkpoint active - config will auto-revert in 2 minutes if not confirmed
[INFO] Executing 5 commands...
  -> vlan 100
  -> name VLAN_100_Data
  -> exit

============================================================
CONFIGURATION APPLIED
============================================================
[INFO] Confirming auto checkpoint to save changes...
[SUCCESS] Auto checkpoint confirmed - configuration saved permanently!
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
- Verify IP addresses in `switches.csv`
- Ensure switches are reachable (try `ping <ip_address>`)
- Check firewall rules for SSH (port 22)

### Authentication Failed
- Verify username and password
- Check if account has appropriate privileges
- Ensure SSH is enabled on the switch

### Commands Not Executing
- Check command syntax in `commands.txt`
- Verify OS type is correctly specified in `switches.csv`
- Check SSH connectivity and credentials

### Archive Configuration Issues (Cisco IOS XE)

If you see errors related to archive or revert timer on Cisco switches:

**Symptom:** `% Configuration Revert/Replace feature requires archive feature to be enabled`

**Cause:** Archive was not configured and auto-setup failed.

**Solution:**
1. The script should auto-configure this, but if it fails, verify:
   - Switch has sufficient flash space: `show flash:`
   - Switch IOS version supports rollback feature
   - User has sufficient privileges to configure archive

2. Manually configure archive if needed:
   ```
   configure terminal
   archive
     path flash:archive-config
     maximum 10
   end
   write memory
   ```

3. Verify archive is working:
   ```
   show archive
   ```
   Should display configured path and maximum files.

**Note:** Archive is automatically configured by the script on first run and persists across reboots.

### Checkpoint Issues (Aruba CX)

If you see errors related to checkpoint commands on Aruba switches:

**Symptom:** Configuration applied but not saved, or "Invalid input" errors

**Common Causes:**
1. **Checkpoint not confirmed in time** - Auto checkpoint reverted after 2-minute timer expired
2. **No active checkpoint** - Trying to confirm when no checkpoint exists
3. **Insufficient permissions** - User account doesn't have checkpoint privileges

**Solutions:**

1. **Verify checkpoint was created:**
   ```
   show checkpoint
   ```
   Should show an `AUTO<timestamp>` checkpoint in the list

2. **Check user permissions:**
   - User must have admin or operator privileges
   - Verify with: `show running-config | include user`

3. **Monitor checkpoint status in accounting logs:**
   ```
   show logging | include checkpoint
   ```
   Should show both `checkpoint auto 2` and `checkpoint auto confirm` commands

4. **Manual checkpoint workflow (if auto fails):**
   ```
   checkpoint my-checkpoint
   <make configuration changes>
   checkpoint confirm
   ```

**Note:** If configuration reverts automatically, you'll see a message like:
```
Reverting to checkpoint AUTO20260504091500
```

This means the 2-minute timer expired before `checkpoint auto confirm` was executed.

## Security Recommendations

1. Use read-only credentials for testing
2. Keep `switches.csv` secure (contains IP addresses)
3. Review all commands in `commands.txt` before execution
4. Test on non-production switches first
5. Always verify configuration changes before confirming

## License

This tool is provided as-is for network automation purposes.

## Contributing

Contributions are welcome! Please ensure all changes are tested on both Aruba CX and Cisco IOS XE platforms.
