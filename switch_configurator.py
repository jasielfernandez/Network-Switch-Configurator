#!/usr/bin/env python3
"""
Network Switch Configurator
Automates SSH configuration with automatic rollback support for Aruba CX and Cisco IOS XE
"""

import csv
import getpass
import sys
import time
from typing import List, Dict, Tuple
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException


class SwitchConfigurator:
    """Handles SSH connections and configuration for network switches"""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.commands = []
        self.prompt_handlers = {}

    def load_switches(self, csv_file: str) -> List[Dict]:
        """Load switch information from CSV file"""
        switches = []
        required_columns = {'switchname', 'ip address', 'vendor'}
        try:
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                # Validate required columns exist
                if reader.fieldnames:
                    missing = required_columns - set(reader.fieldnames)
                    if missing:
                        print(f"[ERROR] Missing required columns in {csv_file}: {', '.join(missing)}")
                        sys.exit(1)

                for row in reader:
                    # Validate required fields have values
                    for col in required_columns:
                        if not row.get(col, '').strip():
                            print(f"[ERROR] Empty value for required column '{col}' in row: {row}")
                            sys.exit(1)
                    switches.append(row)
            print(f"[INFO] Loaded {len(switches)} switches from {csv_file}")
            return switches
        except FileNotFoundError:
            print(f"[ERROR] File not found: {csv_file}")
            sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Failed to load switches: {e}")
            sys.exit(1)

    def load_commands(self, commands_file: str) -> List[str]:
        """Load commands from text file"""
        try:
            with open(commands_file, 'r') as f:
                self.commands = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            print(f"[INFO] Loaded {len(self.commands)} commands from {commands_file}")
            return self.commands
        except FileNotFoundError:
            print(f"[ERROR] File not found: {commands_file}")
            sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Failed to load commands: {e}")
            sys.exit(1)

    def load_prompt_handlers(self, prompt_file: str) -> Dict[str, str]:
        """Load prompt handling responses from file"""
        try:
            with open(prompt_file, 'r') as f:
                for line in f:
                    if line.strip() and not line.startswith('#'):
                        if '|' in line:
                            prompt, response = line.strip().split('|', 1)
                            response = response.strip()
                            # Handle special keywords
                            if response.upper() == 'RETURN' or response.upper() == 'ENTER':
                                response = '\n'
                            elif response.upper() == 'YES':
                                response = 'yes\n'
                            elif response.upper() == 'NO':
                                response = 'no\n'
                            self.prompt_handlers[prompt.strip()] = response
            print(f"[INFO] Loaded {len(self.prompt_handlers)} prompt handlers from {prompt_file}")
            return self.prompt_handlers
        except FileNotFoundError:
            print(f"[WARN] Prompt handling file not found: {prompt_file}, continuing without prompt handlers")
            return {}
        except Exception as e:
            print(f"[WARN] Failed to load prompt handlers: {e}, continuing without them")
            return {}

    def get_device_type(self, os_type: str) -> str:
        """Map OS type to netmiko device type"""
        os_mapping = {
            'aruba_cx': 'aruba_osswitch',
            'arubacx': 'aruba_osswitch',
            'aruba': 'aruba_osswitch',
            'cisco_ios_xe': 'cisco_ios',
            'cisco_xe': 'cisco_ios',
            'cisco': 'cisco_ios',
            'ios_xe': 'cisco_ios',
            'ios': 'cisco_ios'
        }
        return os_mapping.get(os_type.lower(), 'cisco_ios')

    def check_archive_configured(self, connection) -> bool:
        """Check if archive is configured on Cisco device"""
        try:
            output = connection.send_command('show archive')
            # If archive is configured, output will show path
            if 'flash:' in output.lower() or 'bootflash:' in output.lower():
                return True
            return False
        except Exception:
            return False

    def setup_archive(self, connection) -> Tuple[bool, str]:
        """Configure archive on Cisco device if not already set up"""
        output = ""
        try:
            print("[INFO] Configuring archive for rollback support...")

            # Configure archive
            archive_commands = [
                'configure terminal',
                'archive',
                'path flash:archive-config',
                'maximum 10',
                'end'
            ]

            for cmd in archive_commands:
                cmd_output = connection.send_command(cmd, expect_string=r'#')
                output += cmd_output + "\n"

            # Save the archive configuration
            save_output = connection.send_command('write memory', expect_string=r'#')
            output += save_output + "\n"

            print("[SUCCESS] Archive configured successfully")
            return True, output
        except Exception as e:
            print(f"[ERROR] Failed to configure archive: {e}")
            return False, output + f"\nERROR: {str(e)}"

    def configure_aruba_cx(self, connection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Aruba CX switch with checkpoint auto confirm"""
        output = ""
        try:
            print("[INFO] Setting up checkpoint with auto-confirm...")
            # Create checkpoint with auto-confirm
            checkpoint_output = connection.send_command('checkpoint auto confirm')
            output += checkpoint_output + "\n"
            print("[INFO] Checkpoint created with auto-confirm enabled")

            # Execute commands
            print(f"[INFO] Executing {len(commands)} commands...")
            for cmd in commands:
                print(f"  -> {cmd}")
                cmd_output = self.execute_command_with_prompts(connection, cmd)
                output += cmd_output + "\n"
                time.sleep(0.5)

            # Automatically confirm changes
            print("\n" + "="*60)
            print("CONFIGURATION APPLIED")
            print("="*60)
            print("[INFO] Auto-confirming configuration changes...")
            confirm_output = connection.send_command('checkpoint confirm')
            output += confirm_output + "\n"
            print("[SUCCESS] Configuration confirmed and saved!")
            return True, output

        except Exception as e:
            print(f"[ERROR] Failed during Aruba CX configuration: {e}")
            print("[INFO] Checkpoint will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def configure_cisco_ios_xe(self, connection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Cisco IOS XE switch with configure terminal revert"""
        output = ""
        try:
            # Check if archive is configured
            if not self.check_archive_configured(connection):
                print("[WARN] Archive not configured, setting up now...")
                success, archive_output = self.setup_archive(connection)
                output += archive_output + "\n"
                if not success:
                    print("[ERROR] Cannot proceed without archive configuration")
                    return False, output
            else:
                print("[INFO] Archive already configured")

            print("[INFO] Entering configuration mode with revert timer (2 minutes)...")
            # Enter config mode with revert timer
            revert_output = connection.send_command('configure terminal revert timer 2', expect_string=r'#')
            output += revert_output + "\n"

            # Validate that revert timer was accepted
            if 'error' in revert_output.lower() or 'invalid' in revert_output.lower():
                print("[ERROR] Failed to enter revert mode - archive may not be properly configured")
                return False, output

            print("[INFO] Configuration mode entered - will auto-revert in 2 minutes if not confirmed")

            # Execute commands
            print(f"[INFO] Executing {len(commands)} commands...")
            for cmd in commands:
                print(f"  -> {cmd}")
                cmd_output = self.execute_command_with_prompts(connection, cmd)
                output += cmd_output + "\n"
                time.sleep(0.5)

            # Exit config mode
            connection.send_command('end')

            # Automatically confirm changes
            print("\n" + "="*60)
            print("CONFIGURATION APPLIED")
            print("="*60)
            print("[INFO] Auto-confirming configuration changes...")
            confirm_output = connection.send_command('configure confirm')
            output += confirm_output + "\n"

            # Save configuration
            save_output = connection.send_command('write memory', expect_string=r'#')
            output += save_output + "\n"
            print("[SUCCESS] Configuration confirmed and saved!")
            return True, output

        except Exception as e:
            print(f"[ERROR] Failed during Cisco IOS XE configuration: {e}")
            print("[INFO] Configuration will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def substitute_variables(self, commands: List[str], switch: Dict) -> List[str]:
        """Substitute variables in commands with values from switch CSV row"""
        substituted = []
        for cmd in commands:
            try:
                # Use format_map for safe variable substitution
                # This allows {column_name} syntax in commands
                substituted_cmd = cmd.format_map(switch)
                substituted.append(substituted_cmd)
            except KeyError as e:
                # If a variable is missing, keep the command as-is and warn
                print(f"[WARN] Variable {e} not found in CSV for command: {cmd}")
                substituted.append(cmd)
        return substituted

    def execute_command_with_prompts(self, connection, command: str) -> str:
        """Execute command and handle any prompts based on prompt_handlers"""
        try:
            # Check if this command needs prompt handling
            for prompt_match, response in self.prompt_handlers.items():
                if prompt_match.lower() in command.lower():
                    # Send command and expect a prompt
                    output = connection.send_command_timing(command)
                    # If there's a prompt, send the response
                    if any(p in output.lower() for p in ['[y/n]', '(y/n)', 'confirm', '[yes/no]']):
                        output += connection.send_command_timing(response)
                    return output

            # Normal command execution
            return connection.send_command(command, expect_string=r'#')
        except Exception as e:
            return f"ERROR executing '{command}': {str(e)}"

    def configure_switch(self, switch: Dict) -> bool:
        """Configure a single switch"""
        hostname = switch.get('switchname', 'unknown')
        ip_address = switch.get('ip address', '')
        os_type = switch.get('vendor', 'cisco').lower()

        print(f"\n{'='*60}")
        print(f"Connecting to {hostname} ({ip_address}) - OS: {os_type}")
        print(f"{'='*60}")

        device_type = self.get_device_type(os_type)

        device = {
            'device_type': device_type,
            'host': ip_address,
            'username': self.username,
            'password': self.password,
            'timeout': 60
        }

        try:
            # Connect to device
            print(f"[INFO] Establishing SSH connection...")
            connection = ConnectHandler(**device)
            print(f"[SUCCESS] Connected to {hostname}")

            # Substitute variables in commands with switch-specific values
            switch_commands = self.substitute_variables(self.commands, switch)

            # Determine OS and configure accordingly
            success = False
            if 'aruba' in os_type:
                success, output = self.configure_aruba_cx(connection, switch_commands)
            else:
                success, output = self.configure_cisco_ios_xe(connection, switch_commands)

            # Disconnect
            connection.disconnect()
            print(f"[INFO] Disconnected from {hostname}")

            return success

        except NetmikoTimeoutException:
            print(f"[ERROR] Connection timeout to {hostname} ({ip_address})")
            return False
        except NetmikoAuthenticationException:
            print(f"[ERROR] Authentication failed to {hostname} ({ip_address})")
            return False
        except Exception as e:
            print(f"[ERROR] Failed to configure {hostname}: {e}")
            return False


def main():
    """Main execution function"""
    print("="*60)
    print("Network Switch Configurator")
    print("Automated SSH Configuration with Rollback Support")
    print("="*60)

    # Get credentials
    print("\nPlease enter SSH credentials:")
    username = input("Username: ").strip()
    if not username:
        print("[ERROR] Username cannot be empty")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("[ERROR] Password cannot be empty")
        sys.exit(1)

    # Initialize configurator
    configurator = SwitchConfigurator(username, password)

    # Load files
    print("\nLoading configuration files...")
    switches = configurator.load_switches('switches.csv')
    commands = configurator.load_commands('commands.txt')
    configurator.load_prompt_handlers('prompthandling.txt')

    if not switches:
        print("[ERROR] No switches to configure")
        sys.exit(1)

    if not commands:
        print("[ERROR] No commands to execute")
        sys.exit(1)

    # Display summary
    print(f"\n{'='*60}")
    print(f"CONFIGURATION SUMMARY")
    print(f"{'='*60}")
    print(f"Switches to configure: {len(switches)}")
    print(f"Commands to execute: {len(commands)}")
    print(f"Prompt handlers: {len(configurator.prompt_handlers)}")
    print(f"{'='*60}")

    # Confirm before proceeding
    proceed = input("\nProceed with configuration? (yes/no): ").strip().lower()
    if proceed != 'yes' and proceed != 'y':
        print("[INFO] Configuration cancelled by user")
        sys.exit(0)

    # Configure each switch
    results = []
    for switch in switches:
        success = configurator.configure_switch(switch)
        results.append({
            'hostname': switch.get('switchname', 'unknown'),
            'success': success
        })

    # Display final results
    print(f"\n{'='*60}")
    print("FINAL RESULTS")
    print(f"{'='*60}")
    for result in results:
        status = "[SUCCESS]" if result['success'] else "[FAILED]"
        print(f"{status} {result['hostname']}")
    print(f"{'='*60}")

    success_count = sum(1 for r in results if r['success'])
    print(f"\nCompleted: {success_count}/{len(results)} switches configured successfully")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Configuration cancelled by user (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")
        sys.exit(1)
