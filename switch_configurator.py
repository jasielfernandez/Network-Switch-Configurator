#!/usr/bin/env python3
"""
Network Switch Configurator
Automates SSH configuration with automatic rollback support for Aruba CX and Cisco IOS XE
"""

import csv
import getpass
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple
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
        try:
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
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
                            # Handle special keywords
                            if response.upper() == 'RETURN' or response.upper() == 'ENTER':
                                response = '\n'
                            elif response.upper() == 'YES':
                                response = 'yes\n'
                            elif response.upper() == 'NO':
                                response = 'no\n'
                            self.prompt_handlers[prompt.strip()] = response.strip()
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

    def configure_aruba_cx(self, connection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Aruba CX switch with checkpoint auto rollback"""
        output = ""
        try:
            print("[INFO] Setting up checkpoint with 2-minute auto-revert...")
            # Create checkpoint with auto-revert
            checkpoint_output = connection.send_command('checkpoint auto 2')
            output += checkpoint_output + "\n"
            print("[INFO] Checkpoint created - configuration will auto-revert in 2 minutes if not confirmed")

            # Execute commands
            print(f"[INFO] Executing {len(commands)} commands...")
            for cmd in commands:
                print(f"  -> {cmd}")
                cmd_output = self.execute_command_with_prompts(connection, cmd)
                output += cmd_output + "\n"
                time.sleep(0.5)

            # Ask user to confirm changes
            print("\n" + "="*60)
            print("CONFIGURATION APPLIED")
            print("="*60)
            print("The configuration will auto-revert in 2 minutes.")
            confirm = input("Do you want to CONFIRM the changes? (yes/no): ").strip().lower()

            if confirm == 'yes' or confirm == 'y':
                confirm_output = connection.send_command('checkpoint confirm')
                output += confirm_output + "\n"
                print("[SUCCESS] Configuration confirmed and saved!")
                return True, output
            else:
                print("[INFO] Configuration will auto-revert in ~2 minutes...")
                return False, output

        except Exception as e:
            print(f"[ERROR] Failed during Aruba CX configuration: {e}")
            print("[INFO] Checkpoint will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def configure_cisco_ios_xe(self, connection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Cisco IOS XE switch with configure terminal revert"""
        output = ""
        try:
            print("[INFO] Entering configuration mode with revert timer (2 minutes)...")
            # Enter config mode with revert timer
            revert_output = connection.send_command('configure terminal revert timer 2', expect_string=r'#')
            output += revert_output + "\n"
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

            # Ask user to confirm changes
            print("\n" + "="*60)
            print("CONFIGURATION APPLIED")
            print("="*60)
            print("The configuration will auto-revert in 2 minutes.")
            confirm = input("Do you want to CONFIRM the changes? (yes/no): ").strip().lower()

            if confirm == 'yes' or confirm == 'y':
                confirm_output = connection.send_command('configure confirm')
                output += confirm_output + "\n"
                # Save configuration
                save_output = connection.send_command('write memory', expect_string=r'#')
                output += save_output + "\n"
                print("[SUCCESS] Configuration confirmed and saved!")
                return True, output
            else:
                print("[INFO] Configuration will auto-revert in ~2 minutes...")
                return False, output

        except Exception as e:
            print(f"[ERROR] Failed during Cisco IOS XE configuration: {e}")
            print("[INFO] Configuration will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

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
            'timeout': 60,
            'session_log': f'session_{hostname}.log'
        }

        try:
            # Connect to device
            print(f"[INFO] Establishing SSH connection...")
            connection = ConnectHandler(**device)
            print(f"[SUCCESS] Connected to {hostname}")

            # Determine OS and configure accordingly
            success = False
            if 'aruba' in os_type:
                success, output = self.configure_aruba_cx(connection, self.commands)
            else:
                success, output = self.configure_cisco_ios_xe(connection, self.commands)

            # Save output to file
            output_file = f'output_{hostname}_{time.strftime("%Y%m%d_%H%M%S")}.log'
            with open(output_file, 'w') as f:
                f.write(output)
            print(f"[INFO] Full output saved to {output_file}")

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
    password = getpass.getpass("Password: ")

    # Initialize configurator
    configurator = SwitchConfigurator(username, password)

    # Load files
    print("\nLoading configuration files...")
    switches = configurator.load_switches('switch.csv')
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
