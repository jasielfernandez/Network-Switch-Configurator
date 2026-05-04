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
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import List, Dict, Tuple, Optional, cast
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from netmiko.base_connection import BaseConnection
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

# Maximum iterations allowed in FOR loops to prevent memory exhaustion
MAX_LOOP_ITERATIONS = 10000

# Maximum time (in seconds) to wait for a single switch configuration
THREAD_TIMEOUT = 600  # 10 minutes per switch


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
        self._commands_lock = threading.RLock()
        self._handlers_lock = threading.RLock()

    def load_switches(self, csv_file: str) -> List[Dict]:
        """Load switch information from CSV file"""
        switches = []
        required_columns = {'switchname', 'ip address', 'vendor'}
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
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
            # Directive keywords that should be preserved
            directives = ['#IF ', '#ELSE', '#ENDIF', '#FOR ', '#ENDFOR']

            with self._commands_lock:
                with open(commands_file, 'r', encoding='utf-8') as f:
                    self.commands = []
                    for line in f:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        # Keep directives, skip regular comments
                        if stripped.startswith('#'):
                            if any(stripped.startswith(d) for d in directives):
                                self.commands.append(stripped)
                        else:
                            self.commands.append(stripped)

                console.print(f"[bold green]✓ INFO:[/bold green] Loaded {len(self.commands)} commands from {commands_file}")
                return self.commands.copy()
        except FileNotFoundError:
            console.print(f"[bold red]✗ ERROR:[/bold red] File not found: {commands_file}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[bold red]✗ ERROR:[/bold red] Failed to load commands: {e}")
            sys.exit(1)

    def load_prompt_handlers(self, prompt_file: str) -> Dict[str, str]:
        """Load prompt handling responses from file"""
        try:
            with self._handlers_lock:
                with open(prompt_file, 'r', encoding='utf-8') as f:
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
                return self.prompt_handlers.copy()
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

    def check_archive_configured(self, connection: BaseConnection) -> Tuple[bool, str]:
        """
        Check if archive is configured on Cisco device.
        Returns: (is_configured, error_message)
        """
        try:
            output: str = cast(str, connection.send_command('show archive', read_timeout=30))
            # If archive is configured, output will show path
            if 'flash:' in output.lower() or 'bootflash:' in output.lower():
                return True, ""
            return False, "Archive not configured"
        except Exception as e:
            return False, f"Failed to check archive: {e}"

    def setup_archive(self, connection: BaseConnection) -> Tuple[bool, str]:
        """Configure archive on Cisco device if not already set up"""
        output: str = ""
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
                cmd_output: str = cast(str, connection.send_command(cmd, expect_string=r'#'))
                output += cmd_output + "\n"

                # Validate each command succeeded
                if 'error' in cmd_output.lower() or 'invalid' in cmd_output.lower():
                    thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Archive setup command failed: {cmd}")
                    return False, output + f"\nERROR: Command '{cmd}' failed"

            # Save the archive configuration
            save_output: str = cast(str, connection.send_command('write memory', expect_string=r'#'))
            output += save_output + "\n"

            if 'error' in save_output.lower() or not ('[OK]' in save_output or 'Building configuration' in save_output):
                thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed to save archive configuration")
                return False, output + "\nERROR: Failed to save configuration"

            # Verify archive was actually configured
            verify_output: str = cast(str, connection.send_command('show archive'))
            if 'flash:' not in verify_output.lower() and 'bootflash:' not in verify_output.lower():
                thread_safe_print("[bold red]✗ ERROR:[/bold red] Archive setup failed validation")
                return False, output + "\nERROR: Archive not configured after setup"

            thread_safe_print("[bold green]✓ SUCCESS:[/bold green] Archive configured successfully")
            return True, output
        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed to configure archive: {e}")
            return False, output + f"\nERROR: {str(e)}"

    def configure_aruba_cx(self, connection: BaseConnection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Aruba CX switch with checkpoint auto confirm"""
        output: str = ""
        try:
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Setting up checkpoint with auto-confirm...")
            # Create checkpoint with auto-confirm (use timing mode for better compatibility)
            checkpoint_output: str = cast(str, connection.send_command('checkpoint auto confirm',
                                                       read_timeout=60,
                                                       expect_string=r'.*#'))
            output += checkpoint_output + "\n"

            # Validate checkpoint was created - be more specific about errors
            error_patterns = [
                'error:',
                'error ',
                '% error',
                'invalid command',
                'failed to',
                'not supported',
                'unable to',
            ]
            checkpoint_lower = checkpoint_output.lower()
            for pattern in error_patterns:
                if pattern in checkpoint_lower:
                    thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Checkpoint creation failed (matched '{pattern}')")
                    thread_safe_print(f"[bold yellow]Output:[/bold yellow] {checkpoint_output}")
                    return False, output

            # Positive confirmation - should see 'checkpoint' in response
            if 'checkpoint' not in checkpoint_lower and 'config' not in checkpoint_lower:
                thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Checkpoint response unclear")
                thread_safe_print(f"[bold yellow]Output:[/bold yellow] {checkpoint_output}")

            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Checkpoint created with auto-confirm enabled")

            # Execute commands
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Executing {len(commands)} commands...")
            output += self.execute_configuration_commands(connection, commands, already_in_config_mode=False)

            # Exit config mode
            connection.exit_config_mode()

            # Configuration applied with auto-confirm
            thread_safe_print()
            thread_safe_print(Panel.fit(
                "[bold green]CONFIGURATION APPLIED[/bold green]",
                border_style="green"
            ))

            # With auto-confirm, the checkpoint is automatically saved after timer expires
            # No need to manually confirm - it happens automatically
            thread_safe_print("[bold green]✓ SUCCESS:[/bold green] Configuration will be auto-confirmed")
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Checkpoint auto-confirm active (changes saved automatically)")

            return True, output

        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed during Aruba CX configuration: {e}")
            thread_safe_print("[bold cyan]⚙ INFO:[/bold cyan] Checkpoint will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def configure_cisco_ios_xe(self, connection: BaseConnection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Cisco IOS XE switch with configure terminal revert"""
        output: str = ""
        try:
            # Check if archive is configured
            is_configured, error_msg = self.check_archive_configured(connection)
            if error_msg and "Failed to check" in error_msg:
                thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Cannot verify archive status: {error_msg}")
                return False, output + f"\nERROR: {error_msg}"
            elif not is_configured:
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
            revert_output: str = cast(str, connection.send_command('configure terminal revert timer 2', expect_string=r'#'))
            output += revert_output + "\n"

            # Validate that revert timer was accepted
            error_keywords = ['error', 'invalid', 'failed', 'not supported']
            revert_lower = revert_output.lower()
            for keyword in error_keywords:
                if keyword in revert_lower:
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
            confirm_output: str = cast(str, connection.send_command('configure confirm'))
            output += confirm_output + "\n"

            # Validate confirmation succeeded
            if 'error' in confirm_output.lower() or 'invalid' in confirm_output.lower():
                thread_safe_print("[bold red]✗ ERROR:[/bold red] Failed to confirm configuration")
                return False, output

            # Save configuration
            save_output: str = cast(str, connection.send_command('write memory', expect_string=r'#'))
            output += save_output + "\n"

            # Validate save succeeded
            if 'error' in save_output.lower() or not ('[OK]' in save_output or 'Building configuration' in save_output):
                thread_safe_print("[bold red]✗ ERROR:[/bold red] Failed to save configuration")
                return False, output

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

    def parse_commands_with_conditionals(self, commands: List[str], switch: Dict) -> List[str]:
        """
        Parse commands and evaluate conditional directives.
        Returns flat list of commands to execute based on switch data.

        Supports:
        - #IF/#ELSE/#ENDIF blocks
        - #FOR/#ENDFOR loops
        - AND/OR logical operators
        """
        result = []
        i = 0

        while i < len(commands):
            line = commands[i].strip()

            if line.startswith('#IF '):
                # Parse IF block
                condition = line[4:].strip()
                condition_met = self._evaluate_condition(condition, switch)
                block_end, else_index = self._find_block_end(commands, i, 'IF')

                if condition_met:
                    # Include IF block
                    if_block = commands[i+1:else_index if else_index else block_end]
                    result.extend(self.parse_commands_with_conditionals(if_block, switch))
                elif else_index:
                    # Include ELSE block
                    else_block = commands[else_index+1:block_end]
                    result.extend(self.parse_commands_with_conditionals(else_block, switch))

                # Skip to line after block end (block_end points to #ENDIF)
                i = block_end + 1
                continue

            elif line.startswith('#FOR '):
                # Parse FOR loop: #FOR variable_name IN {count}
                try:
                    for_parts = line[5:].strip().split(' IN ')
                    if len(for_parts) != 2:
                        thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Invalid FOR syntax: {line}")
                        i += 1
                        continue

                    var_name = for_parts[0].strip()

                    # Validate variable name
                    if not var_name.isidentifier():
                        thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Invalid variable name '{var_name}' in FOR loop, skipping")
                        i += 1
                        continue

                    # Warn if shadowing existing CSV column
                    if var_name in switch:
                        thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] FOR loop variable '{var_name}' shadows existing CSV column")

                    count_expr = for_parts[1].strip()
                    count = self._get_loop_count(count_expr, switch)

                    block_end = self._find_block_end(commands, i, 'FOR')[0]
                    loop_block = commands[i+1:block_end]

                    # Expand block N times with variable substitution
                    for iteration in range(1, count + 1):
                        # Create temporary switch dict with loop variable
                        temp_switch = switch.copy()
                        temp_switch[var_name] = str(iteration)

                        # Recursively parse the block with the loop variable
                        expanded = self.parse_commands_with_conditionals(loop_block, temp_switch)
                        # Substitute variables in the expanded commands with the loop variable
                        substituted = self.substitute_variables(expanded, temp_switch)
                        result.extend(substituted)

                    # Skip to line after block end (block_end points to #ENDFOR)
                    i = block_end + 1
                    continue

                except Exception as e:
                    thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Error parsing FOR loop: {e}")
                    i += 1
                    continue

            elif line.startswith('#ENDIF') or line.startswith('#ELSE') or line.startswith('#ENDFOR'):
                # Skip directive markers (handled by block parsing)
                pass

            elif line.startswith('#'):
                # Regular comment - skip
                pass

            elif line:
                # Regular command - include it
                result.append(line)

            i += 1

        return result

    def _evaluate_condition(self, condition: str, switch: Dict) -> bool:
        """
        Evaluate condition string against switch data with proper operator precedence.
        AND has higher precedence than OR.
        Example: "{vendor} == cisco AND {stack} > 0 OR {backup} == yes"
        is evaluated as: "({vendor} == cisco AND {stack} > 0) OR {backup} == yes"
        """
        try:
            # Split on OR first (lower precedence)
            if ' OR ' in condition:
                parts = condition.split(' OR ')
                return any(self._evaluate_and_condition(p.strip(), switch) for p in parts)
            else:
                return self._evaluate_and_condition(condition, switch)
        except Exception as e:
            thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Error evaluating condition '{condition}': {e}")
            return False

    def _evaluate_and_condition(self, condition: str, switch: Dict) -> bool:
        """
        Evaluate AND conditions (higher precedence).
        Example: "{vendor} == cisco AND {stack} > 0"
        """
        if ' AND ' in condition:
            parts = condition.split(' AND ')
            return all(self._evaluate_simple_condition(p.strip(), switch) for p in parts)
        else:
            return self._evaluate_simple_condition(condition, switch)

    def _evaluate_simple_condition(self, condition: str, switch: Dict) -> bool:
        """
        Evaluate simple condition: "{column} operator value"
        Supports: ==, !=, >, <, >=, <=
        """
        # CRITICAL: Check longer operators first to avoid incorrect splitting
        # e.g., ">=" must be checked before ">"
        operators = ['>=', '<=', '==', '!=', '>', '<']

        for op in operators:
            if op in condition:
                left, right = condition.split(op, 1)
                left_val = left.strip()
                right_val = right.strip().strip('"').strip("'")

                # Substitute variables in left side
                if left_val.startswith('{') and left_val.endswith('}'):
                    column = left_val[1:-1]
                    left_val = switch.get(column, '')

                # Substitute variables in right side
                if right_val.startswith('{') and right_val.endswith('}'):
                    column = right_val[1:-1]
                    right_val = switch.get(column, '')

                # Compare values
                if op == '==':
                    return str(left_val) == str(right_val)
                elif op == '!=':
                    return str(left_val) != str(right_val)
                elif op in ['>', '<', '>=', '<=']:
                    try:
                        # Try numeric comparison
                        left_num = float(left_val) if left_val else 0
                        right_num = float(right_val) if right_val else 0
                        if op == '>':
                            return left_num > right_num
                        elif op == '<':
                            return left_num < right_num
                        elif op == '>=':
                            return left_num >= right_num
                        elif op == '<=':
                            return left_num <= right_num
                    except (ValueError, TypeError):
                        # Numeric comparison failed - must be non-numeric values
                        # Don't fall back to string comparison for safety
                        thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Cannot compare non-numeric values with {op}: '{left_val}' {op} '{right_val}'")
                        return False

        return False

    def _get_loop_count(self, count_expr: str, switch: Dict) -> int:
        """
        Get loop count from expression with maximum limit.
        Examples: "#FOR i IN {stack}" → int(switch['stack'])
                  "#FOR i IN 5" → 5
        Maximum iterations capped at MAX_LOOP_ITERATIONS to prevent memory exhaustion.
        """
        count_expr = count_expr.strip()

        # Variable reference
        if count_expr.startswith('{') and count_expr.endswith('}'):
            column = count_expr[1:-1]
            value = switch.get(column, '0')
            try:
                count = max(0, int(value))
                if count > MAX_LOOP_ITERATIONS:
                    thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Loop count {count} exceeds maximum {MAX_LOOP_ITERATIONS}, capping")
                    return MAX_LOOP_ITERATIONS
                return count
            except (ValueError, TypeError):
                thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Invalid loop count '{value}' for {column}, using 0")
                return 0

        # Literal number
        try:
            count = max(0, int(count_expr))
            if count > MAX_LOOP_ITERATIONS:
                thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Loop count {count} exceeds maximum {MAX_LOOP_ITERATIONS}, capping")
                return MAX_LOOP_ITERATIONS
            return count
        except (ValueError, TypeError):
            thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Invalid loop count '{count_expr}', using 0")
            return 0

    def _find_block_end(self, commands: List[str], start_index: int, block_type: str) -> Tuple[int, Optional[int]]:
        """
        Find the matching end directive for a block.
        Returns: (end_index, else_index)
        - end_index: index of #ENDIF or #ENDFOR
        - else_index: index of #ELSE (only for IF blocks), or None

        Handles nested blocks correctly.
        """
        end_directive = f'#END{block_type}'
        else_directive = '#ELSE' if block_type == 'IF' else None

        depth = 1
        else_index = None

        for i in range(start_index + 1, len(commands)):
            line = commands[i].strip()

            if line.startswith(f'#{block_type} '):
                depth += 1
            elif line.startswith(end_directive):
                depth -= 1
                if depth == 0:
                    return (i, else_index)
            elif line.startswith('#ELSE') and depth == 1 and else_directive:
                else_index = i

        raise ValueError(f"Unmatched {block_type} block starting at line {start_index + 1}")

    def _safe_disconnect(self, connection: Optional[BaseConnection], hostname: str) -> None:
        """Safely disconnect with proper error handling and resource cleanup"""
        if connection is None:
            return

        try:
            connection.disconnect()
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Disconnected from {hostname}")
        except Exception as e:
            thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Error disconnecting from {hostname}: {e}")
            # Force close the socket if disconnect fails
            try:
                if hasattr(connection, 'remote_conn') and connection.remote_conn:
                    connection.remote_conn.close()
            except (OSError, AttributeError):
                # Socket might already be closed or connection object malformed
                pass

    def get_prompt_response(self, command: str) -> Optional[str]:
        """Return the configured prompt response for a command, if one exists."""
        with self._handlers_lock:
            for prompt_match, response in self.prompt_handlers.items():
                if prompt_match.lower() in command.lower():
                    return response
        return None

    def execute_command_with_prompts(self, connection: BaseConnection, command: str, *, in_config_mode: bool = False) -> str:
        """Execute a command and optionally respond to interactive prompts."""
        try:
            response = self.get_prompt_response(command)
            if response is not None:
                output: str = cast(str, connection.send_command_timing(command))
                if any(p in output.lower() for p in ['[y/n]', '(y/n)', 'confirm', '[yes/no]']):
                    output += cast(str, connection.send_command_timing(response))
                return output

            if in_config_mode:
                return connection.send_config_set(
                    [command],
                    enter_config_mode=False,
                    exit_config_mode=False,
                    cmd_verify=False,
                )

            return cast(str, connection.send_command(command, expect_string=r'#'))
        except Exception as e:
            raise RuntimeError(f"ERROR executing '{command}': {str(e)}") from e

    def execute_configuration_batch(self, connection: BaseConnection, commands: List[str], *, enter_config_mode: bool) -> str:
        """Send a batch of non-interactive configuration commands through Netmiko config mode."""
        for cmd in commands:
            thread_safe_print(f"  [dim cyan]→[/dim cyan] {cmd}")

        output: str = cast(str, connection.send_config_set(
            commands,
            enter_config_mode=enter_config_mode,
            exit_config_mode=False,
            cmd_verify=False,  # Keep for performance, check output manually
            read_timeout=120,  # Increased timeout for slow devices
        ))

        # Check output for common error indicators
        error_indicators = ['% invalid', '% incomplete', '% error', 'command not found', 'syntax error', '% unknown command']
        output_lower = output.lower()

        for indicator in error_indicators:
            if indicator in output_lower:
                # Find the line with error
                for line in output.split('\n'):
                    if indicator in line.lower():
                        raise RuntimeError(f"Command execution failed: {line.strip()}")

        return output + "\n"

    def execute_configuration_commands(self, connection: BaseConnection, commands: List[str], *, already_in_config_mode: bool) -> str:
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

        connection = None
        try:
            # Connect to device
            thread_safe_print(f"[bold cyan]⚙ INFO:[/bold cyan] Establishing SSH connection...")
            connection = ConnectHandler(**device)
            thread_safe_print(f"[bold green]✓ SUCCESS:[/bold green] Connected to {hostname}")

            # Parse conditionals first, then substitute variables
            # Make a copy to avoid race conditions with shared state
            with self._commands_lock:
                commands_copy = self.commands.copy()

            try:
                parsed_commands = self.parse_commands_with_conditionals(commands_copy, switch)
            except ValueError as e:
                thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Conditional parsing failed for {hostname}: {e}")
                thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] Falling back to commands without conditionals")
                # Fallback: use original commands without conditional parsing (filter out directives)
                parsed_commands = [cmd for cmd in commands_copy if not cmd.startswith('#')]

            switch_commands = self.substitute_variables(parsed_commands, switch)

            # Check for empty command list
            if not switch_commands:
                thread_safe_print(f"[bold yellow]⚠ WARNING:[/bold yellow] No commands to execute after conditional parsing for {hostname}")
                self._safe_disconnect(connection, hostname)
                return True  # Not an error, just no work to do

            # Determine OS and configure accordingly
            success = False
            if 'aruba' in os_type:
                success, output = self.configure_aruba_cx(connection, switch_commands)
            else:
                success, output = self.configure_cisco_ios_xe(connection, switch_commands)

            # Disconnect
            self._safe_disconnect(connection, hostname)
            return success

        except NetmikoTimeoutException:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Connection timeout to {hostname} ({ip_address})")
            self._safe_disconnect(connection, hostname)
            return False
        except NetmikoAuthenticationException:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Authentication failed to {hostname} ({ip_address})")
            self._safe_disconnect(connection, hostname)
            return False
        except Exception as e:
            thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Failed to configure {hostname}: {e}")
            self._safe_disconnect(connection, hostname)
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
            for future in as_completed(future_to_switch, timeout=THREAD_TIMEOUT * len(switches)):
                switch = future_to_switch[future]
                try:
                    success = future.result(timeout=THREAD_TIMEOUT)  # Per-thread timeout
                    results.append({
                        'hostname': switch.get('switchname', 'unknown'),
                        'ip': switch.get('ip address', 'unknown'),
                        'vendor': switch.get('vendor', 'unknown'),
                        'success': success
                    })
                except FuturesTimeoutError:
                    thread_safe_print(f"[bold red]✗ ERROR:[/bold red] Configuration timeout for {switch.get('switchname', 'unknown')} (>{THREAD_TIMEOUT}s)")
                    results.append({
                        'hostname': switch.get('switchname', 'unknown'),
                        'ip': switch.get('ip address', 'unknown'),
                        'vendor': switch.get('vendor', 'unknown'),
                        'success': False
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
                    with console_lock:
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
