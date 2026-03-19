#!/usr/bin/env python3
"""
Network Switch Configurator
Automates SSH configuration with automatic rollback support for Aruba CX and Cisco IOS XE
"""

import csv
import getpass
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Optional
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.style import Style
from rich import box

# Initialize thread-safe rich console
console = Console()
console_lock = threading.Lock()


def thread_safe_print(*args, **kwargs):
    """Thread-safe wrapper for console.print"""
    with console_lock:
        console.print(*args, **kwargs)


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
                        console.print(f"[bold red]✗ ERROR:[/bold red] Missing required columns in {csv_file}: {', '.join(missing)}")
                        sys.exit(1)

                for row in reader:
                    # Validate required fields have values
                    for col in required_columns:
                        if not row.get(col, '').strip():
                            console.print(f"[bold red]✗ ERROR:[/bold red] Empty value for required column '{col}' in row: {row}")
                            sys.exit(1)
                    switches.append(row)
            console.print(f"[bold green]✓ INFO:[/bold green] Loaded {len(switches)} switches from {csv_file}")
            return switches
        except FileNotFoundError:
            console.print(f"[bold red]✗ ERROR:[/bold red] File not found: {csv_file}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[bold red]✗ ERROR:[/bold red] Failed to load switches: {e}")
            sys.exit(1)

    def load_commands(self, commands_file: str) -> List[str]:
        """Load commands from text file"""
        try:
            with open(commands_file, 'r') as f:
                self.commands = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            console.print(f"[bold green]✓ INFO:[/bold green] Loaded {len(self.commands)} commands from {commands_file}")
            return self.commands
        except FileNotFoundError:
            console.print(f"[bold red]✗ ERROR:[/bold red] File not found: {commands_file}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[bold red]✗ ERROR:[/bold red] Failed to load commands: {e}")
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
            console.print(f"[bold green]✓ INFO:[/bold green] Loaded {len(self.prompt_handlers)} prompt handlers from {prompt_file}")
            return self.prompt_handlers
        except FileNotFoundError:
            console.print(f"[bold yellow]⚠ WARNING:[/bold yellow] Prompt handling file not found: {prompt_file}, continuing without prompt handlers")
            return {}
        except Exception as e:
            console.print(f"[bold yellow]⚠ WARNING:[/bold yellow] Failed to load prompt handlers: {e}, continuing without them")
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
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Configuring archive for rollback support...")

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

            thread_safe_print("[bold green]✓ SUCCESS:[/bold green] Archive configured successfully")
            return True, output
        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed to configure archive: {e}")
            return False, output + f"\nERROR: {str(e)}"

    def configure_aruba_cx(self, connection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Aruba CX switch with checkpoint auto confirm"""
        output = ""
        try:
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Setting up checkpoint with auto-confirm...")
            # Create checkpoint with auto-confirm (use timing mode for better compatibility)
            checkpoint_output = connection.send_command('checkpoint auto confirm',
                                                       read_timeout=60,
                                                       expect_string=r'.*#')
            output += checkpoint_output + "\n"
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Checkpoint created with auto-confirm enabled")

            # Execute commands
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Executing {len(commands)} commands...")
            output += self.execute_configuration_commands(connection, commands, already_in_config_mode=False)

            # Exit config mode before confirming checkpoint
            connection.exit_config_mode()

            # Automatically confirm changes
            thread_safe_print()
            thread_safe_print(Panel.fit(
                "[bold green]CONFIGURATION APPLIED[/bold green]",
                border_style="green"
            ))
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Auto-confirming configuration changes...")
            confirm_output = connection.send_command('checkpoint confirm',
                                                     read_timeout=60,
                                                     expect_string=r'.*#')
            output += confirm_output + "\n"
            thread_safe_print("[bold green]✓ SUCCESS:[/bold green] Configuration confirmed and saved!")
            return True, output

        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed during Aruba CX configuration: {e}")
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Checkpoint will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def configure_cisco_ios_xe(self, connection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Cisco IOS XE switch with configure terminal revert"""
        output = ""
        try:
            # Check if archive is configured
            if not self.check_archive_configured(connection):
                thread_safe_print("[bold yellow]⚠ WARNING:[/bold yellow] Archive not configured, setting up now...")
                success, archive_output = self.setup_archive(connection)
                output += archive_output + "\n"
                if not success:
                    thread_safe_print("[bold red]✗ ERROR:[/bold red] Cannot proceed without archive configuration")
                    return False, output
            else:
                thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Archive already configured")

            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Entering configuration mode with revert timer (2 minutes)...")
            # Enter config mode with revert timer
            revert_output = connection.send_command('configure terminal revert timer 2', expect_string=r'#')
            output += revert_output + "\n"

            # Validate that revert timer was accepted
            if 'error' in revert_output.lower() or 'invalid' in revert_output.lower():
                thread_safe_print("[bold red]✗ ERROR:[/bold red] Failed to enter revert mode - archive may not be properly configured")
                return False, output

            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Configuration mode entered - will auto-revert in 2 minutes if not confirmed")

            # Execute commands
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Executing {len(commands)} commands...")
            output += self.execute_configuration_commands(connection, commands, already_in_config_mode=True)

            # Exit config mode
            connection.exit_config_mode()

            # Automatically confirm changes
            thread_safe_print()
            thread_safe_print(Panel.fit(
                "[bold green]CONFIGURATION APPLIED[/bold green]",
                border_style="green"
            ))
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Auto-confirming configuration changes...")
            confirm_output = connection.send_command('configure confirm')
            output += confirm_output + "\n"

            # Save configuration
            save_output = connection.send_command('write memory', expect_string=r'#')
            output += save_output + "\n"
            thread_safe_print("[bold green]✓ SUCCESS:[/bold green] Configuration confirmed and saved!")
            return True, output

        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed during Cisco IOS XE configuration: {e}")
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Configuration will auto-revert if active...")
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
                thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Variable {e} not found in CSV for command: {cmd}")
                substituted.append(cmd)
        return substituted

    def get_prompt_response(self, command: str) -> Optional[str]:
        """Return the configured prompt response for a command, if one exists."""
        for prompt_match, response in self.prompt_handlers.items():
            if prompt_match.lower() in command.lower():
                return response
        return None

    def execute_command_with_prompts(self, connection, command: str, *, in_config_mode: bool = False) -> str:
        """Execute a command and optionally respond to interactive prompts."""
        try:
            response = self.get_prompt_response(command)
            if response is not None:
                output = connection.send_command_timing(command)
                if any(p in output.lower() for p in ['[y/n]', '(y/n)', 'confirm', '[yes/no]']):
                    output += connection.send_command_timing(response)
                return output

            if in_config_mode:
                return connection.send_config_set(
                    [command],
                    enter_config_mode=False,
                    exit_config_mode=False,
                    cmd_verify=False,
                )

            return connection.send_command(command, expect_string=r'#')
        except Exception as e:
            raise RuntimeError(f"ERROR executing '{command}': {str(e)}") from e

    def execute_configuration_batch(self, connection, commands: List[str], *, enter_config_mode: bool) -> str:
        """Send a batch of non-interactive configuration commands through Netmiko config mode."""
        for cmd in commands:
            thread_safe_print(f"  [dim cyan]→[/dim cyan] {cmd}")
        return connection.send_config_set(
            commands,
            enter_config_mode=enter_config_mode,
            exit_config_mode=False,
            cmd_verify=False,
            read_timeout=120,  # Increased timeout for slow devices
        ) + "\n"

    def execute_configuration_commands(self, connection, commands: List[str], *, already_in_config_mode: bool) -> str:
        """Execute configuration commands in-order, batching normal lines and isolating prompt-driven ones."""
        if not commands:
            raise ValueError("No configuration commands were provided")

        output = ""
        in_config_mode = already_in_config_mode
        pending_batch: List[str] = []

        def flush_pending_batch() -> None:
            nonlocal output, in_config_mode, pending_batch
            if not pending_batch:
                return
            output += self.execute_configuration_batch(
                connection,
                pending_batch,
                enter_config_mode=not in_config_mode,
            )
            in_config_mode = True
            pending_batch = []

        for cmd in commands:
            if self.get_prompt_response(cmd) is None:
                pending_batch.append(cmd)
                continue

            flush_pending_batch()
            if not in_config_mode:
                connection.config_mode()
                in_config_mode = True

            thread_safe_print(f"  [dim cyan]→[/dim cyan] {cmd}")
            output += self.execute_command_with_prompts(connection, cmd, in_config_mode=True) + "\n"

        flush_pending_batch()
        return output

    def configure_switch(self, switch: Dict) -> bool:
        """Configure a single switch (thread-safe)"""
        hostname = switch.get('switchname', 'unknown')
        ip_address = switch.get('ip address', '')
        os_type = switch.get('vendor', 'cisco').lower()

        thread_safe_print()
        thread_safe_print(Panel(
            f"[bold white]Connecting to[/bold white] [cyan]{hostname}[/cyan] [dim]({ip_address})[/dim]\n"
            f"[bold white]OS:[/bold white] [yellow]{os_type.upper()}[/yellow]",
            border_style="cyan",
            box=box.ROUNDED
        ))

        device_type = self.get_device_type(os_type)

        device = {
            'device_type': device_type,
            'host': ip_address,
            'username': self.username,
            'password': self.password,
            'timeout': 120,           # Increased timeout for slow devices
            'session_timeout': 120,   # Command execution timeout
            'global_delay_factor': 2, # Slow down for device compatibility
            'fast_cli': False,        # Disable fast CLI to avoid prompt detection issues
        }

        try:
            # Connect to device
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Establishing SSH connection...")
            connection = ConnectHandler(**device)
            thread_safe_print(f"[bold green]✓ SUCCESS:[/bold green] Connected to {hostname}")

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
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Disconnected from {hostname}")

            return success

        except NetmikoTimeoutException:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Connection timeout to {hostname} ({ip_address})")
            return False
        except NetmikoAuthenticationException:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Authentication failed to {hostname} ({ip_address})")
            return False
        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed to configure {hostname}: {e}")
            return False


def main():
    """Main execution function"""
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Network Switch Configurator[/bold cyan]\n"
        "[dim]Automated SSH Configuration with Rollback Support[/dim]",
        border_style="cyan",
        box=box.DOUBLE
    ))

    # Get credentials
    console.print("\n[bold]Please enter SSH credentials:[/bold]")
    username = Prompt.ask("[cyan]Username[/cyan]").strip()
    if not username:
        console.print("[bold red]✗ ERROR:[/bold red] Username cannot be empty")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        console.print("[bold red]✗ ERROR:[/bold red] Password cannot be empty")
        sys.exit(1)

    # Initialize configurator
    configurator = SwitchConfigurator(username, password)

    # Load files
    console.print("\n[bold]Loading configuration files...[/bold]")
    switches = configurator.load_switches('switches.csv')
    commands = configurator.load_commands('commands.txt')
    configurator.load_prompt_handlers('prompthandling.txt')

    if not switches:
        console.print("[bold red]✗ ERROR:[/bold red] No switches to configure")
        sys.exit(1)

    if not commands:
        console.print("[bold red]✗ ERROR:[/bold red] No commands to execute")
        sys.exit(1)

    # Display summary
    console.print()
    summary_table = Table(title="Configuration Summary", box=box.ROUNDED, border_style="cyan")
    summary_table.add_column("Parameter", style="cyan", justify="left")
    summary_table.add_column("Value", style="green", justify="right")
    summary_table.add_row("Switches to configure", str(len(switches)))
    summary_table.add_row("Commands to execute", str(len(commands)))
    summary_table.add_row("Prompt handlers", str(len(configurator.prompt_handlers)))
    console.print(summary_table)

    # Confirm before proceeding
    console.print()
    if not Confirm.ask("[bold yellow]Proceed with configuration?[/bold yellow]", default=False):
        console.print("[bold cyan]⚙ INFO:[/bold cyan] Configuration cancelled by user")
        sys.exit(0)

    # Configure switches concurrently (max 10 at a time)
    console.print()
    console.print(f"[bold cyan]⚙ INFO:[/bold cyan] Configuring switches with up to 10 concurrent threads...")
    console.print()

    results = []
    max_workers = min(10, len(switches))  # Up to 10 concurrent threads

    # Use ThreadPoolExecutor for concurrent execution
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task(
            f"[cyan]Configuring {len(switches)} switches...",
            total=len(switches)
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all switch configuration tasks
            future_to_switch = {
                executor.submit(configurator.configure_switch, switch): switch
                for switch in switches
            }

            # Process completed tasks as they finish
            for future in as_completed(future_to_switch):
                switch = future_to_switch[future]
                try:
                    success = future.result()
                    results.append({
                        'hostname': switch.get('switchname', 'unknown'),
                        'ip': switch.get('ip address', 'unknown'),
                        'vendor': switch.get('vendor', 'unknown'),
                        'success': success
                    })
                except Exception as e:
                    thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Unexpected error for {switch.get('switchname', 'unknown')}: {e}")
                    results.append({
                        'hostname': switch.get('switchname', 'unknown'),
                        'ip': switch.get('ip address', 'unknown'),
                        'vendor': switch.get('vendor', 'unknown'),
                        'success': False
                    })
                finally:
                    progress.update(task, advance=1)

    # Display final results
    console.print()
    results_table = Table(
        title="[bold]Final Results[/bold]",
        box=box.DOUBLE,
        border_style="cyan",
        show_header=True,
        header_style="bold cyan"
    )
    results_table.add_column("Status", justify="center", width=10)
    results_table.add_column("Hostname", style="white", justify="left")
    results_table.add_column("IP Address", style="dim", justify="left")
    results_table.add_column("Vendor", style="yellow", justify="left")

    for result in results:
        status_icon = "[bold green]✓ OK[/bold green]" if result['success'] else "[bold red]✗ FAIL[/bold red]"
        results_table.add_row(
            status_icon,
            result['hostname'],
            result['ip'],
            result['vendor']
        )

    console.print(results_table)

    success_count = sum(1 for r in results if r['success'])
    total_count = len(results)

    if success_count == total_count:
        status_style = "bold green"
        status_msg = "All switches configured successfully!"
    elif success_count > 0:
        status_style = "bold yellow"
        status_msg = f"Partial success: {success_count}/{total_count} switches configured"
    else:
        status_style = "bold red"
        status_msg = "All configurations failed"

    console.print()
    console.print(Panel.fit(
        f"[{status_style}]{status_msg}[/{status_style}]",
        border_style=status_style.split()[1] if status_style else "white"
    ))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold cyan]⚙ INFO:[/bold cyan] Configuration cancelled by user (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[bold red]✗ ERROR:[/bold red] Unexpected error: {e}")
        sys.exit(1)
