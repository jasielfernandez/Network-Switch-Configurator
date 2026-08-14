#!/usr/bin/env python3
"""
Network Switch Configurator
Automates SSH configuration with automatic rollback support for Aruba CX and Cisco IOS XE
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, List, Dict, Mapping, Sequence, Tuple, Optional, Union, cast
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from netmiko.base_connection import BaseConnection
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

status_lock = threading.Lock()
_status_callback: Optional[Callable[["StatusEvent"], None]] = None
console = Console()
console_lock = threading.Lock()

# File-only logging. Operator-facing status stays in the Rich UI; diagnostics
# and raw device output go to this single log file.
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "switch_configurator.log"


def setup_logging() -> logging.Logger:
    """Configure the application logger to write diagnostics to one file."""
    LOG_DIR.mkdir(exist_ok=True)

    app_logger = logging.getLogger("switch_configurator")
    app_logger.setLevel(logging.DEBUG)
    app_logger.propagate = False

    for handler in list(app_logger.handlers):
        if isinstance(handler, logging.NullHandler):
            app_logger.removeHandler(handler)

    if not any(isinstance(handler, logging.FileHandler) for handler in app_logger.handlers):
        file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
        ))
        app_logger.addHandler(file_handler)

    return app_logger


logger = logging.getLogger("switch_configurator")
logger.addHandler(logging.NullHandler())

# Maximum iterations allowed in FOR loops to prevent memory exhaustion
MAX_LOOP_ITERATIONS = 10000

# Maximum time (in seconds) to wait for a single switch configuration
THREAD_TIMEOUT = 600  # 10 minutes per switch

# SSH connection retry settings. TACACS+/RADIUS-backed devices can stall or
# reject logins for a few seconds while the AAA server responds, which shows
# up as an immediate authentication failure. Retry with a pause before giving
# up on a switch. Kept modest so repeated attempts with genuinely bad
# credentials do not trip account lockout policies.
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 10  # seconds between connection attempts

# The UI uses the cross-platform timer range. Cisco IOS XE supports up to 120
# minutes, but Aruba AOS-CX checkpoint auto is documented at 1-60 minutes.
ROLLBACK_TIMER_MIN = 1
ROLLBACK_TIMER_MAX = 60

# Maximum duration (seconds) allowed for a single #SLEEP directive. Kept well under
# THREAD_TIMEOUT (600s) and the rollback/checkpoint window so a pause cannot trigger
# an auto-revert or a per-switch timeout on its own.
MAX_SLEEP_SECONDS = 300

# Sentinel prefix emitted when a #SLEEP node is rendered into the flat command list.
# Device CLI commands never start with '#', so this cannot collide with a real command.
SLEEP_TOKEN_PREFIX = "#SLEEP "

# Common command output errors caught after Netmiko command execution
ERROR_INDICATORS = (
    "% invalid",
    "% incomplete",
    "% error",
    "% unknown command",
    "command not found",
    "syntax error",
)


class ConfigError(Exception):
    """Raised when local configuration files or CLI inputs are invalid."""


class CommandSyntaxError(ConfigError):
    """Raised when commands.txt contains invalid conditional syntax."""


class DeviceExecutionError(Exception):
    """Raised when device command output indicates a failed operation."""


@dataclass(frozen=True)
class SwitchRecord:
    """Normalized switch inventory row while preserving extra CSV fields."""

    switchname: str
    ip_address: str
    vendor: str
    fields: Mapping[str, str]

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "SwitchRecord":
        fields = {str(key): "" if value is None else str(value) for key, value in row.items()}
        return cls(
            switchname=fields.get("switchname", "unknown"),
            ip_address=fields.get("ip address", ""),
            vendor=fields.get("vendor", "cisco"),
            fields=MappingProxyType(fields),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def to_context(self) -> Dict[str, str]:
        return dict(self.fields)


@dataclass(frozen=True)
class RunConfig:
    """Runtime settings for a single CLI execution."""

    username: str
    password: str
    rollback_timer: int
    max_workers: int = 10
    switches_file: str = "switches.csv"
    commands_file: str = "commands.txt"
    prompt_file: str = "prompthandling.txt"


@dataclass(frozen=True)
class SwitchResult:
    """Final status for one switch configuration attempt."""

    hostname: str
    ip: str
    vendor: str
    success: bool
    error: str = ""


@dataclass(frozen=True)
class StatusEvent:
    """Concise operator-facing status message."""

    message: str
    level: str = "info"
    switch: str = ""


@dataclass(frozen=True)
class TextCommand:
    text: str


@dataclass(frozen=True)
class IfBlock:
    condition: str
    if_body: Tuple["CommandNode", ...]
    else_body: Tuple["CommandNode", ...] = ()


@dataclass(frozen=True)
class ForBlock:
    variable: str
    count_expr: str
    body: Tuple["CommandNode", ...]


@dataclass(frozen=True)
class SleepCommand:
    seconds: float
    raw: str  # original numeric text, preserved for a clean rendered token


CommandNode = Union[TextCommand, IfBlock, ForBlock, SleepCommand]


def set_status_callback(callback: Optional[Callable[[StatusEvent], None]]) -> None:
    """Set a process-wide status callback used by worker threads."""
    global _status_callback
    with status_lock:
        _status_callback = callback


def strip_markup(value: Any) -> str:
    """Convert legacy markup-heavy messages into plain status text."""
    return str(value).strip()


def infer_status_level(message: str) -> str:
    lowered = message.lower()
    if "error" in lowered or "fail" in lowered:
        return "error"
    if "warning" in lowered:
        return "warning"
    if "success" in lowered or " ok" in lowered:
        return "success"
    return "info"


def thread_safe_print(*args, **kwargs):
    """Compatibility status emitter used by worker code."""
    message = " ".join(strip_markup(arg) for arg in args if str(arg).strip())
    if not message:
        return
    event = StatusEvent(message=message, level=infer_status_level(message))
    logger.info("%s", message)
    with status_lock:
        callback = _status_callback
    if callback:
        callback(event)


def log_device_output(context: str, output: str) -> None:
    """Write raw device output to the log file without printing it."""
    if output:
        logger.debug("%s output:\n%s", context, output.rstrip())
    else:
        logger.debug("%s output: <empty>", context)


def find_error_line(output: str, indicators: Sequence[str] = ERROR_INDICATORS) -> Optional[str]:
    """Return the first line that matches a known device error indicator."""
    output_lower = output.lower()
    for indicator in indicators:
        if indicator in output_lower:
            for line in output.splitlines():
                if indicator in line.lower():
                    return line.strip()
            return indicator
    return None


def validate_no_command_errors(output: str, context: str) -> None:
    """Raise if device output contains a known command error."""
    error_line = find_error_line(output)
    if error_line:
        raise DeviceExecutionError(f"{context} failed: {error_line}")


def output_indicates_save_success(output: str) -> bool:
    """Return whether Cisco-style save output indicates success."""
    return "[OK]" in output or "Building configuration" in output


def switch_result_from_record(switch: Union[SwitchRecord, Mapping[str, Any]], success: bool, error: str = "") -> SwitchResult:
    """Build a SwitchResult from either a normalized record or a CSV row mapping."""
    record = switch if isinstance(switch, SwitchRecord) else SwitchRecord.from_mapping(switch)
    return SwitchResult(
        hostname=record.switchname,
        ip=record.ip_address,
        vendor=record.vendor,
        success=success,
        error=error,
    )


class SwitchConfigurator:
    """Handles SSH connections and configuration for network switches"""

    def __init__(self, username: str, password: str, rollback_timer: int = 2):
        self.username = username
        self.password = password
        self.rollback_timer = rollback_timer  # Timer in minutes for automatic rollback
        self.commands: Tuple[str, ...] = ()
        self.command_template: Tuple[CommandNode, ...] = ()
        self.prompt_handlers: Mapping[str, str] = MappingProxyType({})

    def load_switches(self, csv_file: str) -> List[SwitchRecord]:
        """Load switch information from CSV file"""
        switches: List[SwitchRecord] = []
        required_columns = {'switchname', 'ip address', 'vendor'}
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                # Validate required columns exist
                if not reader.fieldnames:
                    raise ConfigError(f"No header row found in {csv_file}")

                missing = required_columns - set(reader.fieldnames)
                if missing:
                    raise ConfigError(f"Missing required columns in {csv_file}: {', '.join(sorted(missing))}")

                for row in reader:
                    # Validate required fields have values
                    for col in required_columns:
                        if not row.get(col, '').strip():
                            raise ConfigError(f"Empty value for required column '{col}' in row: {row}")
                    switches.append(SwitchRecord.from_mapping(row))
            thread_safe_print(f"INFO: Loaded {len(switches)} switches from {csv_file}")
            return switches
        except FileNotFoundError:
            raise ConfigError(f"File not found: {csv_file}") from None
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(f"Failed to load switches: {e}") from e

    def load_commands(self, commands_file: str) -> List[str]:
        """Load commands from text file"""
        try:
            # Directive keywords that should be preserved
            directives = ['#IF ', '#ELSE', '#ENDIF', '#FOR ', '#ENDFOR', '#SLEEP ']

            commands: List[str] = []
            with open(commands_file, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    # Keep directives, skip regular comments
                    if stripped.startswith('#'):
                        if any(stripped.startswith(d) for d in directives):
                            commands.append(stripped)
                    else:
                        commands.append(stripped)

            self.command_template = self.compile_command_template(commands)
            self.commands = tuple(commands)

            thread_safe_print(f"INFO: Loaded {len(self.commands)} commands from {commands_file}")
            return list(self.commands)
        except FileNotFoundError:
            raise ConfigError(f"File not found: {commands_file}") from None
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(f"Failed to load commands: {e}") from e

    def load_prompt_handlers(self, prompt_file: str) -> Dict[str, str]:
        """Load prompt handling responses from file"""
        try:
            prompt_handlers: Dict[str, str] = {}
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
                            prompt_handlers[prompt.strip()] = response
            self.prompt_handlers = MappingProxyType(prompt_handlers)
            thread_safe_print(f"INFO: Loaded {len(self.prompt_handlers)} prompt handlers from {prompt_file}")
            return dict(self.prompt_handlers)
        except FileNotFoundError:
            thread_safe_print(f"WARNING: Prompt handling file not found: {prompt_file}; continuing without prompt handlers")
            self.prompt_handlers = MappingProxyType({})
            return {}
        except Exception as e:
            thread_safe_print(f"WARNING: Failed to load prompt handlers: {e}; continuing without them")
            self.prompt_handlers = MappingProxyType({})
            return {}

    def get_device_type(self, os_type: str) -> str:
        """Map OS type to netmiko device type"""
        os_mapping = {
            'aruba_cx': 'aruba_aoscx',
            'arubacx': 'aruba_aoscx',
            'aruba': 'aruba_aoscx',
            'aruba_aoscx': 'aruba_aoscx',
            'aruba_os': 'aruba_os',
            'aruba_osswitch': 'aruba_osswitch',
            'aruba_procurve': 'aruba_procurve',
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
            output: str = cast(str, connection.send_command('show archive', read_timeout=60))
            log_device_output("Cisco archive check", output)
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
            thread_safe_print("INFO: Configuring archive for rollback support...")

            # Configure archive in Netmiko config mode. Cisco treats the path value
            # as a URL/prefix used by the archive subsystem.
            archive_commands = [
                'archive',
                'path flash:archive-config',
                'maximum 10',
            ]

            archive_output: str = cast(str, connection.send_config_set(
                archive_commands,
                enter_config_mode=True,
                exit_config_mode=True,
                cmd_verify=False,
                read_timeout=240,
            ))
            output += archive_output + "\n"
            log_device_output("Cisco archive setup", archive_output)

            try:
                validate_no_command_errors(archive_output, "Archive setup")
            except DeviceExecutionError as e:
                thread_safe_print("ERROR: Archive setup command failed")
                return False, output + f"\nERROR: {str(e)}"

            # Save the archive configuration
            save_output: str = cast(str, connection.send_command('write memory', expect_string=r'#'))
            output += save_output + "\n"
            log_device_output("Archive setup save", save_output)

            if find_error_line(save_output) or not output_indicates_save_success(save_output):
                thread_safe_print(f"ERROR: Failed to save archive configuration")
                return False, output + "\nERROR: Failed to save configuration"

            # Verify archive was actually configured
            verify_output: str = cast(str, connection.send_command('show archive'))
            log_device_output("Archive setup verification", verify_output)
            if 'flash:' not in verify_output.lower() and 'bootflash:' not in verify_output.lower():
                thread_safe_print("ERROR: Archive setup failed validation")
                return False, output + "\nERROR: Archive not configured after setup"

            thread_safe_print("SUCCESS: Archive configured successfully")
            return True, output
        except Exception as e:
            thread_safe_print(f"ERROR: Failed to configure archive: {str(e)}")
            return False, output + f"\nERROR: {str(e)}"

    def configure_aruba_cx(self, connection: BaseConnection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Aruba CX switch with checkpoint auto rollback protection"""
        output: str = ""
        try:
            thread_safe_print(f"INFO: Starting auto checkpoint mode ({self.rollback_timer}-minute timer)...")
            # Start auto checkpoint mode with configurable timer
            # Creates checkpoint named AUTO<YYYYMMDDHHMMSS>
            try:
                checkpoint_output: str = cast(str, connection.send_command(f'checkpoint auto {self.rollback_timer}',
                                                           read_timeout=120,
                                                           expect_string=r'.*#'))
                output += checkpoint_output + "\n"
                log_device_output("Aruba checkpoint auto", checkpoint_output)
            except Exception as e:
                thread_safe_print(f"ERROR: Failed to execute checkpoint auto command: {str(e)}")
                return False, output + f"\nERROR: {str(e)}"

            # Validate auto checkpoint started successfully
            # Look for actual error patterns at line starts (more specific to avoid false positives)
            error_patterns = [
                ('% error', 'Command error'),
                ('% invalid', 'Invalid command'),
                ('% failed', 'Command failed'),
                ('error:', 'Error message'),
                ('not supported', 'Feature not supported'),
                ('permission denied', 'Permission issue'),
            ]

            # Check for actual errors (lines starting with error indicators)
            for line in checkpoint_output.split('\n'):
                line_lower = line.strip().lower()
                for pattern, description in error_patterns:
                    if line_lower.startswith(pattern) or (pattern in line_lower and ('error' in line_lower or '%' in line_lower)):
                        thread_safe_print(f"ERROR: Failed to start auto checkpoint mode: {description}")
                        thread_safe_print(f"OUTPUT: {str(checkpoint_output)}")
                        thread_safe_print(f"HINT: Check user permissions and Aruba CX OS version")
                        return False, output

            thread_safe_print("SUCCESS: Auto checkpoint validation passed")
            thread_safe_print(f"INFO: Auto checkpoint active - config will auto-revert in {self.rollback_timer} minutes if not confirmed")
            logger.debug("About to execute %s Aruba commands", len(commands))
            logger.debug("Aruba command preview: %s", commands[:3])

            # Execute commands
            thread_safe_print(f"INFO: Executing {len(commands)} commands...")
            try:
                cmd_output = self.execute_configuration_commands(connection, commands, already_in_config_mode=False)
                output += cmd_output
                logger.debug("Aruba command execution completed, output length: %s chars", len(cmd_output))
            except Exception as e:
                thread_safe_print(f"ERROR: Exception during command execution: {str(e)}")
                logger.debug("Exception type during Aruba command execution: %s", type(e).__name__)
                raise

            # Exit config mode
            connection.exit_config_mode()

            # Confirm the auto checkpoint to save changes permanently
            thread_safe_print("SUCCESS: Configuration applied")
            thread_safe_print("INFO: Confirming auto checkpoint to save changes...")

            # Use 'checkpoint auto confirm' to confirm the auto checkpoint
            try:
                confirm_output: str = cast(str, connection.send_command('checkpoint auto confirm',
                                                         read_timeout=120,
                                                         expect_string=r'.*#'))
                output += confirm_output + "\n"
                log_device_output("Aruba checkpoint auto confirm", confirm_output)
            except Exception as e:
                thread_safe_print(f"ERROR: Failed to execute checkpoint auto confirm: {str(e)}")
                thread_safe_print(f"WARNING: Configuration may auto-revert if timer expires")
                return False, output + f"\nERROR: {str(e)}"

            # Validate confirmation succeeded
            # Be more specific about actual errors vs informational messages
            has_error = False
            error_hint = "Configuration may auto-revert after timer expires"

            # Check for actual error patterns
            for line in confirm_output.split('\n'):
                line_lower = line.strip().lower()

                # Definite errors
                if line_lower.startswith('% error') or line_lower.startswith('% invalid'):
                    has_error = True
                    break
                elif 'invalid input' in line_lower and '%' in line_lower:
                    has_error = True
                    error_hint = "Checkpoint may have already been confirmed or expired"
                    break
                elif ('no checkpoint' in line_lower or 'does not exist' in line_lower) and ('error' in line_lower or '%' in line_lower):
                    has_error = True
                    error_hint = "No active checkpoint found - may have already reverted"
                    break
                elif line_lower.startswith('error:') or line_lower.startswith('failed:'):
                    has_error = True
                    break

            if has_error:
                thread_safe_print(f"ERROR: Failed to confirm auto checkpoint")
                thread_safe_print(f"OUTPUT: {str(confirm_output)}")
                thread_safe_print(f"HINT: {error_hint}")
                return False, output

            thread_safe_print("SUCCESS: Auto checkpoint confirmed - configuration saved permanently!")
            return True, output

        except Exception as e:
            thread_safe_print(f"ERROR: Failed during Aruba CX configuration: {str(e)}")
            thread_safe_print("INFO: Checkpoint will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def configure_cisco_ios_xe(self, connection: BaseConnection, commands: List[str]) -> Tuple[bool, str]:
        """Configure Cisco IOS XE switch with configure terminal revert"""
        output: str = ""
        try:
            # Check if archive is configured
            is_configured, error_msg = self.check_archive_configured(connection)
            if error_msg and "Failed to check" in error_msg:
                thread_safe_print(f"ERROR: Cannot verify archive status: {str(error_msg)}")
                return False, output + f"\nERROR: {error_msg}"
            elif not is_configured:
                thread_safe_print("WARNING: Archive not configured, setting up now...")
                success, archive_output = self.setup_archive(connection)
                output += archive_output + "\n"
                if not success:
                    thread_safe_print("ERROR: Cannot proceed without archive configuration")
                    return False, output
            else:
                thread_safe_print("INFO: Archive already configured")

            thread_safe_print(f"INFO: Entering configuration mode with revert timer ({self.rollback_timer} minutes)...")
            # Enter config mode with revert timer
            revert_output: str = cast(str, connection.send_command(f'configure terminal revert timer {self.rollback_timer}', expect_string=r'#'))
            output += revert_output + "\n"
            log_device_output("Cisco configure terminal revert timer", revert_output)

            # Validate that revert timer was accepted
            revert_error = find_error_line(revert_output, (*ERROR_INDICATORS, 'failed', 'not supported'))
            if revert_error:
                thread_safe_print("ERROR: Failed to enter revert mode - archive may not be properly configured")
                return False, output

            thread_safe_print(f"INFO: Configuration mode entered - will auto-revert in {self.rollback_timer} minutes if not confirmed")

            # Execute commands
            thread_safe_print(f"INFO: Executing {len(commands)} commands...")
            output += self.execute_configuration_commands(connection, commands, already_in_config_mode=True)

            # Exit config mode
            connection.exit_config_mode()

            # Automatically confirm changes
            thread_safe_print("SUCCESS: Configuration applied")
            thread_safe_print("INFO: Auto-confirming configuration changes...")
            confirm_output: str = cast(str, connection.send_command('configure confirm'))
            output += confirm_output + "\n"
            log_device_output("Cisco configure confirm", confirm_output)

            # Validate confirmation succeeded
            if find_error_line(confirm_output):
                thread_safe_print("ERROR: Failed to confirm configuration")
                return False, output

            # Save configuration
            save_output: str = cast(str, connection.send_command('write memory', expect_string=r'#'))
            output += save_output + "\n"
            log_device_output("Cisco write memory", save_output)

            # Validate save succeeded
            if find_error_line(save_output) or not output_indicates_save_success(save_output):
                thread_safe_print("ERROR: Failed to save configuration")
                return False, output

            thread_safe_print("SUCCESS: Configuration confirmed and saved!")
            return True, output

        except Exception as e:
            thread_safe_print(f"ERROR: Failed during Cisco IOS XE configuration: {str(e)}")
            thread_safe_print("INFO: Configuration will auto-revert if active...")
            return False, output + f"\nERROR: {str(e)}"

    def compile_command_template(self, commands: Sequence[str]) -> Tuple[CommandNode, ...]:
        """Validate and compile command directives once before any device connection."""
        nodes, index, terminator = self._parse_command_block(commands, 0, ())
        if terminator is not None:
            raise CommandSyntaxError(f"Unexpected directive '{terminator}' at line {index + 1}")
        if index != len(commands):
            raise CommandSyntaxError(f"Unexpected parser stop at line {index + 1}")
        return tuple(nodes)

    def _parse_command_block(
        self,
        commands: Sequence[str],
        start_index: int,
        end_tokens: Tuple[str, ...],
    ) -> Tuple[List[CommandNode], int, Optional[str]]:
        """Parse commands until EOF or one of end_tokens is reached."""
        nodes: List[CommandNode] = []
        i = start_index

        while i < len(commands):
            line = commands[i].strip()

            if any(line.startswith(token) for token in end_tokens):
                return nodes, i, line

            if line.startswith('#IF '):
                condition = line[4:].strip()
                if not condition:
                    raise CommandSyntaxError(f"Empty #IF condition at line {i + 1}")

                if_body, next_index, terminator = self._parse_command_block(
                    commands,
                    i + 1,
                    ('#ELSE', '#ENDIF'),
                )

                else_body: List[CommandNode] = []
                if terminator is None:
                    raise CommandSyntaxError(f"Unmatched #IF starting at line {i + 1}")
                if terminator.startswith('#ELSE'):
                    else_body, next_index, terminator = self._parse_command_block(
                        commands,
                        next_index + 1,
                        ('#ENDIF',),
                    )
                    if terminator is None:
                        raise CommandSyntaxError(f"Unmatched #ELSE for #IF starting at line {i + 1}")
                if not terminator.startswith('#ENDIF'):
                    raise CommandSyntaxError(f"Unmatched #IF starting at line {i + 1}")

                nodes.append(IfBlock(condition, tuple(if_body), tuple(else_body)))
                i = next_index + 1
                continue

            if line.startswith('#FOR '):
                for_parts = line[5:].strip().split(' IN ')
                if len(for_parts) != 2:
                    raise CommandSyntaxError(f"Invalid #FOR syntax at line {i + 1}: {line}")

                var_name = for_parts[0].strip()
                if not var_name.isidentifier():
                    raise CommandSyntaxError(f"Invalid #FOR variable name at line {i + 1}: {var_name}")

                count_expr = for_parts[1].strip()
                if not count_expr:
                    raise CommandSyntaxError(f"Missing #FOR loop count at line {i + 1}")

                body, next_index, terminator = self._parse_command_block(
                    commands,
                    i + 1,
                    ('#ENDFOR',),
                )
                if terminator is None:
                    raise CommandSyntaxError(f"Unmatched #FOR starting at line {i + 1}")

                nodes.append(ForBlock(var_name, count_expr, tuple(body)))
                i = next_index + 1
                continue

            if line == '#SLEEP' or line.startswith('#SLEEP '):
                arg = line[len('#SLEEP'):].strip()
                try:
                    seconds = float(arg)
                except ValueError:
                    raise CommandSyntaxError(f"Invalid #SLEEP duration at line {i + 1}: {arg!r}")
                if seconds < 0:
                    raise CommandSyntaxError(f"#SLEEP duration cannot be negative at line {i + 1}")
                if seconds > MAX_SLEEP_SECONDS:
                    raise CommandSyntaxError(
                        f"#SLEEP duration {seconds}s exceeds maximum {MAX_SLEEP_SECONDS}s at line {i + 1}"
                    )
                nodes.append(SleepCommand(seconds=seconds, raw=arg))
                i += 1
                continue

            if line.startswith(('#ELSE', '#ENDIF', '#ENDFOR')):
                raise CommandSyntaxError(f"Unexpected directive at line {i + 1}: {line}")

            if line.startswith('#'):
                i += 1
                continue

            if line:
                nodes.append(TextCommand(line))

            i += 1

        return nodes, i, None

    def render_command_template(self, switch: Mapping[str, Any]) -> List[str]:
        """Render the precompiled command template for a switch row."""
        if not self.command_template:
            self.command_template = self.compile_command_template(self.commands)
        return self._render_command_nodes(self.command_template, switch)

    def _render_command_nodes(self, nodes: Sequence[CommandNode], switch: Mapping[str, Any]) -> List[str]:
        """Evaluate command template nodes against a switch context."""
        result: List[str] = []

        for node in nodes:
            if isinstance(node, TextCommand):
                result.append(node.text)
            elif isinstance(node, SleepCommand):
                result.append(f"{SLEEP_TOKEN_PREFIX}{node.raw}")
            elif isinstance(node, IfBlock):
                branch = node.if_body if self._evaluate_condition(node.condition, switch) else node.else_body
                result.extend(self._render_command_nodes(branch, switch))
            elif isinstance(node, ForBlock):
                if node.variable in switch:
                    thread_safe_print(f"WARNING: FOR loop variable '{node.variable}' shadows existing CSV column")

                count = self._get_loop_count(node.count_expr, switch)
                logger.debug(
                    "FOR loop will iterate %s times (variable: %s, expression: %s)",
                    count,
                    node.variable,
                    node.count_expr,
                )

                for iteration in range(1, count + 1):
                    temp_switch = dict(switch)
                    temp_switch[node.variable] = str(iteration)
                    expanded = self._render_command_nodes(node.body, temp_switch)
                    result.extend(self.substitute_variables(expanded, temp_switch))

        return result

    def substitute_variables(self, commands: Sequence[str], switch: Mapping[str, Any]) -> List[str]:
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
                thread_safe_print(f"WARNING: Variable {str(e)} not found in CSV for command: {str(cmd)}")
                substituted.append(cmd)
            except (IndexError, ValueError) as e:
                # Literal braces (e.g. in banner text) break str.format_map;
                # send the command unchanged instead of failing the switch.
                thread_safe_print(f"WARNING: Could not substitute variables ({str(e)}) for command: {str(cmd)}")
                substituted.append(cmd)
        return substituted

    def parse_commands_with_conditionals(self, commands: Sequence[str], switch: Mapping[str, Any]) -> List[str]:
        """
        Parse commands and evaluate conditional directives.
        Returns flat list of commands to execute based on switch data.

        Supports:
        - #IF/#ELSE/#ENDIF blocks
        - #FOR/#ENDFOR loops
        - AND/OR logical operators
        """
        template = self.compile_command_template(commands)
        return self._render_command_nodes(template, switch)

    def _evaluate_condition(self, condition: str, switch: Mapping[str, Any]) -> bool:
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
            thread_safe_print(f"WARNING: Error evaluating condition '{str(condition)}': {str(e)}")
            return False

    def _evaluate_and_condition(self, condition: str, switch: Mapping[str, Any]) -> bool:
        """
        Evaluate AND conditions (higher precedence).
        Example: "{vendor} == cisco AND {stack} > 0"
        """
        if ' AND ' in condition:
            parts = condition.split(' AND ')
            return all(self._evaluate_simple_condition(p.strip(), switch) for p in parts)
        else:
            return self._evaluate_simple_condition(condition, switch)

    def _evaluate_simple_condition(self, condition: str, switch: Mapping[str, Any]) -> bool:
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
                        thread_safe_print(f"WARNING: Cannot compare non-numeric values with {op}: '{left_val}' {op} '{right_val}'")
                        return False

        return False

    def _get_loop_count(self, count_expr: str, switch: Mapping[str, Any]) -> int:
        """
        Get loop count from expression with maximum limit.
        Examples: "#FOR i IN {stack}" -> int(switch['stack'])
                  "#FOR i IN 5" -> 5
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
                    thread_safe_print(f"WARNING: Loop count {count} exceeds maximum {MAX_LOOP_ITERATIONS}, capping")
                    return MAX_LOOP_ITERATIONS
                return count
            except (ValueError, TypeError):
                thread_safe_print(f"WARNING: Invalid loop count '{value}' for {column}, using 0")
                return 0

        # Literal number
        try:
            count = max(0, int(count_expr))
            if count > MAX_LOOP_ITERATIONS:
                thread_safe_print(f"WARNING: Loop count {count} exceeds maximum {MAX_LOOP_ITERATIONS}, capping")
                return MAX_LOOP_ITERATIONS
            return count
        except (ValueError, TypeError):
            thread_safe_print(f"WARNING: Invalid loop count '{count_expr}', using 0")
            return 0

    def _connect_with_retries(self, device: Dict[str, Any], hostname: str) -> BaseConnection:
        """Open an SSH connection, retrying transient auth/timeout failures.

        TACACS+ authorization can take several seconds; while the AAA server is
        slow or briefly overloaded the device may reject the login outright. A
        short pause and retry usually succeeds, so only give up after
        CONNECT_ATTEMPTS tries.
        """
        last_error: Exception = RuntimeError("No connection attempts made")
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    thread_safe_print(
                        f"INFO: Retrying connection to {hostname} (attempt {attempt}/{CONNECT_ATTEMPTS})..."
                    )
                return cast(BaseConnection, ConnectHandler(**device))
            except (NetmikoAuthenticationException, NetmikoTimeoutException) as e:
                last_error = e
                kind = (
                    "Authentication failed"
                    if isinstance(e, NetmikoAuthenticationException)
                    else "Connection timeout"
                )
                logger.debug("Connection attempt %s/%s to %s failed: %s", attempt, CONNECT_ATTEMPTS, hostname, e)
                if attempt < CONNECT_ATTEMPTS:
                    thread_safe_print(
                        f"WARNING: {kind} for {hostname} on attempt {attempt}/{CONNECT_ATTEMPTS}; "
                        f"waiting {CONNECT_RETRY_DELAY}s before retry (AAA server may be slow)"
                    )
                    time.sleep(CONNECT_RETRY_DELAY)
        raise last_error

    def _safe_disconnect(self, connection: Optional[BaseConnection], hostname: str) -> None:
        """Safely disconnect with proper error handling and resource cleanup"""
        if connection is None:
            return

        try:
            connection.disconnect()
            thread_safe_print(f"INFO: Disconnected from {hostname}")
        except Exception as e:
            thread_safe_print(f"WARNING: Error disconnecting from {hostname}: {str(e)}")
            # Force close the socket if disconnect fails
            try:
                if hasattr(connection, 'remote_conn') and connection.remote_conn:
                    connection.remote_conn.close()
            except (OSError, AttributeError):
                # Socket might already be closed or connection object malformed
                pass

    def get_prompt_response(self, command: str) -> Optional[str]:
        """Return the configured prompt response for a command, if one exists."""
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
                log_device_output(f"Prompt-handled command '{command}'", output)
                return output

            if in_config_mode:
                output = cast(str, connection.send_config_set(
                    [command],
                    enter_config_mode=False,
                    exit_config_mode=False,
                    cmd_verify=False,
                ))
                log_device_output(f"Config command '{command}'", output)
                return output

            output = cast(str, connection.send_command(command, expect_string=r'#'))
            log_device_output(f"Command '{command}'", output)
            return output
        except Exception as e:
            raise RuntimeError(f"ERROR executing '{command}': {str(e)}") from e

    def execute_configuration_batch(self, connection: BaseConnection, commands: List[str], *, enter_config_mode: bool) -> str:
        """Send a batch of non-interactive configuration commands through Netmiko config mode."""
        for cmd in commands:
            thread_safe_print(f"COMMAND: {cmd}")

        output: str = cast(str, connection.send_config_set(
            commands,
            enter_config_mode=enter_config_mode,
            exit_config_mode=False,
            cmd_verify=False,  # Keep for performance, check output manually
            read_timeout=240,  # Extended timeout for TACACS+ authorization delays and slow devices
        ))
        log_device_output("Configuration batch", output)

        validate_no_command_errors(output, "Command execution")

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
            if cmd.startswith(SLEEP_TOKEN_PREFIX):
                seconds = float(cmd[len(SLEEP_TOKEN_PREFIX):].strip())
                flush_pending_batch()  # ensure prior commands reach the device before pausing
                if seconds >= self.rollback_timer * 60:
                    thread_safe_print(
                        f"WARNING: #SLEEP {seconds}s is >= the rollback window "
                        f"({self.rollback_timer} min); configuration may auto-revert"
                    )
                thread_safe_print(f"INFO: Sleeping {seconds}s before next command...")
                time.sleep(seconds)
                continue

            if self.get_prompt_response(cmd) is None:
                pending_batch.append(cmd)
                continue

            flush_pending_batch()
            if not in_config_mode:
                connection.config_mode()
                in_config_mode = True

            thread_safe_print(f"COMMAND: {cmd}")
            output += self.execute_command_with_prompts(connection, cmd, in_config_mode=True) + "\n"

        flush_pending_batch()
        return output

    def configure_switch(self, switch: Union[SwitchRecord, Mapping[str, Any]]) -> bool:
        """Configure a single switch (thread-safe)"""
        record = switch if isinstance(switch, SwitchRecord) else SwitchRecord.from_mapping(switch)
        switch_context = record.to_context()
        hostname = record.switchname
        ip_address = record.ip_address
        os_type = record.vendor.lower()

        thread_safe_print(f"INFO: Connecting to {hostname} ({ip_address}) - OS: {os_type.upper()}")

        device_type = self.get_device_type(os_type)

        # Build device connection parameters
        device = {
            'device_type': device_type,
            'host': ip_address,
            'username': self.username,
            'password': self.password,
            'conn_timeout': 120,
            'auth_timeout': 120,
            'banner_timeout': 120,
            'timeout': 120,
            'session_timeout': 120,
            'read_timeout_override': 240,
            'global_delay_factor': 2,
            'fast_cli': False,
        }

        # Aruba CX switches don't use 'enable' command - prevent netmiko from sending it
        if 'aruba' in os_type:
            device['secret'] = ''  # Disable enable mode for Aruba

        connection = None
        try:
            # Connect to device
            thread_safe_print(f"INFO: Establishing SSH connection...")
            connection = self._connect_with_retries(device, hostname)
            thread_safe_print(f"SUCCESS: Connected to {hostname}")

            parsed_commands = self.render_command_template(switch_context)
            logger.debug(
                "Parsed %s commands after conditional processing for %s",
                len(parsed_commands),
                hostname,
            )
            for idx, cmd in enumerate(parsed_commands, 1):
                logger.debug("Parsed command for %s [%s]: %s", hostname, idx, cmd)

            switch_commands = self.substitute_variables(parsed_commands, switch_context)

            # Check for empty command list
            if not switch_commands:
                thread_safe_print(f"WARNING: No commands to execute after conditional parsing for {hostname}")
                self._safe_disconnect(connection, hostname)
                return True  # Not an error, just no work to do

            # Determine OS and configure accordingly
            if 'aruba' in os_type:
                success, _output = self.configure_aruba_cx(connection, switch_commands)
            else:
                success, _output = self.configure_cisco_ios_xe(connection, switch_commands)

            # Disconnect
            self._safe_disconnect(connection, hostname)
            return success

        except NetmikoTimeoutException:
            thread_safe_print(
                f"ERROR: Connection timeout to {hostname} ({ip_address}) after {CONNECT_ATTEMPTS} attempts"
            )
            self._safe_disconnect(connection, hostname)
            return False
        except NetmikoAuthenticationException:
            thread_safe_print(
                f"ERROR: Authentication failed to {hostname} ({ip_address}) after {CONNECT_ATTEMPTS} attempts"
            )
            self._safe_disconnect(connection, hostname)
            return False
        except Exception as e:
            thread_safe_print(f"ERROR: Failed to configure {hostname}: {str(e)}")
            self._safe_disconnect(connection, hostname)
            return False

    def run_switches(
        self,
        switches: Sequence[Union[SwitchRecord, Mapping[str, Any]]],
        *,
        max_workers: int = 10,
        progress_callback: Optional[Callable[[], None]] = None,
    ) -> List[SwitchResult]:
        """Configure switches concurrently and always return one result per switch."""
        if not switches:
            return []

        results: List[SwitchResult] = []
        completed_futures = set()
        worker_count = min(max_workers, len(switches))

        executor = ThreadPoolExecutor(max_workers=worker_count)
        shutdown_wait = True
        try:
            future_to_switch = {
                executor.submit(self.configure_switch, switch): switch
                for switch in switches
            }

            try:
                for future in as_completed(future_to_switch, timeout=THREAD_TIMEOUT * len(future_to_switch)):
                    completed_futures.add(future)
                    switch = future_to_switch[future]
                    try:
                        success = future.result()
                        results.append(switch_result_from_record(switch, success))
                    except Exception as e:
                        record = switch if isinstance(switch, SwitchRecord) else SwitchRecord.from_mapping(switch)
                        thread_safe_print(f"ERROR: Unexpected error for {record.switchname}: {str(e)}")
                        results.append(switch_result_from_record(switch, False, str(e)))
                    finally:
                        if progress_callback:
                            progress_callback()
            except FuturesTimeoutError:
                timed_out = [
                    (future, switch)
                    for future, switch in future_to_switch.items()
                    if future not in completed_futures
                ]
                for future, switch in timed_out:
                    future.cancel()
                    record = switch if isinstance(switch, SwitchRecord) else SwitchRecord.from_mapping(switch)
                    error = "Configuration timeout before completion"
                    thread_safe_print(f"ERROR: {error} for {record.switchname}")
                    results.append(switch_result_from_record(switch, False, error))
                    if progress_callback:
                        progress_callback()
                shutdown_wait = False
        finally:
            executor.shutdown(wait=shutdown_wait, cancel_futures=True)

        return results


def status_style(level: str) -> str:
    """Map status levels to Rich styles."""
    return {
        "success": "green",
        "warning": "yellow",
        "error": "red",
        "info": "cyan",
    }.get(level, "white")


def print_status(event: StatusEvent) -> None:
    """Render a concise worker status event."""
    with console_lock:
        console.print(event.message, style=status_style(event.level), markup=False)


def prompt_run_config() -> RunConfig:
    """Collect operator input for a run."""
    console.print(Panel.fit(
        "[bold cyan]Network Switch Configurator[/bold cyan]\n"
        "[dim]SSH configuration with timed rollback protection[/dim]",
        border_style="cyan",
        box=box.ROUNDED,
    ))

    username = Prompt.ask("SSH username").strip()
    if not username:
        raise ConfigError("Username cannot be empty")

    password = Prompt.ask("SSH password", password=True)
    if not password:
        raise ConfigError("Password cannot be empty")

    rollback_value = Prompt.ask(
        f"Rollback timer in minutes ({ROLLBACK_TIMER_MIN}-{ROLLBACK_TIMER_MAX})",
        default="2",
    ).strip()

    try:
        rollback_timer = int(rollback_value)
    except ValueError:
        raise ConfigError("Rollback timer must be a whole number") from None

    if rollback_timer < ROLLBACK_TIMER_MIN or rollback_timer > ROLLBACK_TIMER_MAX:
        raise ConfigError(f"Rollback timer must be between {ROLLBACK_TIMER_MIN} and {ROLLBACK_TIMER_MAX} minutes")

    return RunConfig(username=username, password=password, rollback_timer=rollback_timer)


def build_summary_table(
    switch_count: int,
    command_count: int,
    handler_count: int,
    rollback_timer: int,
    max_workers: int,
) -> Table:
    """Build the pre-run summary table."""
    table = Table(title="Run Summary", box=box.SIMPLE_HEAVY, border_style="cyan")
    table.add_column("Item", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    table.add_row("Switches", str(switch_count))
    table.add_row("Commands", str(command_count))
    table.add_row("Prompt handlers", str(handler_count))
    table.add_row("Rollback timer", f"{rollback_timer} minutes")
    table.add_row("Concurrency", f"{max_workers} workers")
    table.add_row("Log file", str(LOG_FILE.relative_to(Path(__file__).resolve().parent)))
    return table


def build_results_table(results: Sequence[SwitchResult]) -> Table:
    """Build the final results table."""
    table = Table(title="Final Results", box=box.SIMPLE_HEAVY, border_style="cyan")
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Hostname", style="white")
    table.add_column("IP Address", style="dim")
    table.add_column("Vendor", style="yellow")
    table.add_column("Detail", style="dim")

    for result in results:
        status = "[green]OK[/green]" if result.success else "[red]FAIL[/red]"
        table.add_row(status, result.hostname, result.ip, result.vendor, result.error)

    return table


def print_final_summary(results: Sequence[SwitchResult]) -> None:
    """Render the final completion summary."""
    success_count = sum(1 for result in results if result.success)
    total = len(results)
    if success_count == total:
        style = "green"
        message = f"Complete: all {total} switches configured successfully."
    elif success_count:
        style = "yellow"
        message = f"Complete: {success_count}/{total} switches configured successfully."
    else:
        style = "red"
        message = "Complete: all switch configurations failed."

    console.print(Panel.fit(f"[bold {style}]{message}[/bold {style}]", border_style=style, box=box.ROUNDED))


def main() -> None:
    """Run the Rich CLI."""
    setup_logging()
    set_status_callback(print_status)

    try:
        run_config = prompt_run_config()
        configurator = SwitchConfigurator(run_config.username, run_config.password, run_config.rollback_timer)

        console.print()
        console.print("[bold]Loading configuration files...[/bold]")
        switches = configurator.load_switches(run_config.switches_file)
        commands = configurator.load_commands(run_config.commands_file)
        configurator.load_prompt_handlers(run_config.prompt_file)

        if not switches:
            raise ConfigError("No switches to configure")
        if not commands:
            raise ConfigError("No commands to execute")

        max_workers = min(run_config.max_workers, len(switches))
        console.print()
        console.print(build_summary_table(
            len(switches),
            len(commands),
            len(configurator.prompt_handlers),
            run_config.rollback_timer,
            max_workers,
        ))

        console.print()
        if not Confirm.ask("[bold yellow]Proceed with configuration?[/bold yellow]", default=False):
            console.print("[cyan]Configuration cancelled.[/cyan]")
            return

        console.print()
        results: List[SwitchResult] = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Configuring {len(switches)} switches", total=len(switches))

            def update_progress() -> None:
                with console_lock:
                    progress.update(task, advance=1)

            results = configurator.run_switches(
                switches,
                max_workers=max_workers,
                progress_callback=update_progress,
            )

        console.print()
        console.print(build_results_table(results))
        console.print()
        print_final_summary(results)
    finally:
        set_status_callback(None)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("Configuration cancelled by user.\n")
        sys.exit(0)
    except ConfigError as e:
        sys.stderr.write(f"ERROR: {str(e)}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"Unexpected error: {str(e)}\n")
        sys.exit(1)
