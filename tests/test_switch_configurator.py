import unittest
import asyncio
import logging
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from switch_configurator import (
    CommandSyntaxError,
    ConfigError,
    DeviceExecutionError,
    SwitchConfigurator,
    SwitchConfiguratorApp,
    SwitchRecord,
    logger,
)


class ExecuteConfigurationCommandsTests(unittest.TestCase):
    def setUp(self):
        self.configurator = SwitchConfigurator("user", "pass")

    def test_batch_execution_uses_send_config_set_and_enters_config_mode_when_needed(self):
        connection = MagicMock()
        connection.send_config_set.return_value = "config applied"

        output = self.configurator.execute_configuration_commands(
            connection,
            ["hostname test", "logging buffered 16384"],
            already_in_config_mode=False,
        )

        self.assertEqual(output, "config applied\n")
        connection.send_config_set.assert_called_once_with(
            ["hostname test", "logging buffered 16384"],
            enter_config_mode=True,
            exit_config_mode=False,
            cmd_verify=False,
            read_timeout=240,
        )
        connection.config_mode.assert_not_called()

    def test_prompt_handled_commands_execute_individually_in_existing_config_mode(self):
        connection = MagicMock()
        self.configurator.prompt_handlers = {"reload": "yes\n"}
        connection.send_command_timing.side_effect = ["Proceed? [y/n]", "yes"]
        connection.send_config_set.return_value = "hostname applied"

        output = self.configurator.execute_configuration_commands(
            connection,
            ["reload in 5", "hostname test"],
            already_in_config_mode=True,
        )

        self.assertIn("Proceed? [y/n]yes", output)
        self.assertIn("hostname applied", output)
        connection.send_command_timing.assert_any_call("reload in 5")
        connection.send_command_timing.assert_any_call("yes\n")
        connection.send_config_set.assert_called_once_with(
            ["hostname test"],
            enter_config_mode=False,
            exit_config_mode=False,
            cmd_verify=False,
            read_timeout=240,
        )
        connection.config_mode.assert_not_called()


    def test_mixed_commands_preserve_order_and_batch_around_prompted_lines(self):
        connection = MagicMock()
        self.configurator.prompt_handlers = {"reload": "yes\n"}
        connection.send_config_set.side_effect = ["hostname applied", "logging applied"]
        connection.send_command_timing.side_effect = ["Proceed? [y/n]", "yes"]

        output = self.configurator.execute_configuration_commands(
            connection,
            ["hostname test", "reload in 5", "logging buffered 16384"],
            already_in_config_mode=False,
        )

        self.assertIn("hostname applied", output)
        self.assertIn("Proceed? [y/n]yes", output)
        self.assertIn("logging applied", output)
        self.assertEqual(
            connection.send_config_set.call_args_list,
            [
                unittest.mock.call(
                    ["hostname test"],
                    enter_config_mode=True,
                    exit_config_mode=False,
                    cmd_verify=False,
                    read_timeout=240,
                ),
                unittest.mock.call(
                    ["logging buffered 16384"],
                    enter_config_mode=False,
                    exit_config_mode=False,
                    cmd_verify=False,
                    read_timeout=240,
                ),
            ],
        )
        connection.config_mode.assert_not_called()

    def test_prompt_first_enters_config_mode_before_running_remaining_commands(self):
        connection = MagicMock()
        self.configurator.prompt_handlers = {"reload": "yes\n"}
        connection.send_config_set.return_value = "hostname applied"
        connection.send_command_timing.side_effect = ["Proceed? [y/n]", "yes"]

        output = self.configurator.execute_configuration_commands(
            connection,
            ["reload in 5", "hostname test"],
            already_in_config_mode=False,
        )

        self.assertIn("Proceed? [y/n]yes", output)
        self.assertIn("hostname applied", output)
        connection.config_mode.assert_called_once_with()
        connection.send_config_set.assert_called_once_with(
            ["hostname test"],
            enter_config_mode=False,
            exit_config_mode=False,
            cmd_verify=False,
            read_timeout=240,
        )

    @patch("switch_configurator.logger.debug")
    def test_batch_execution_logs_device_output_to_file_logger(self, mock_debug):
        connection = MagicMock()
        connection.send_config_set.return_value = "config applied"

        output = self.configurator.execute_configuration_batch(
            connection,
            ["hostname test"],
            enter_config_mode=True,
        )

        self.assertEqual(output, "config applied\n")
        mock_debug.assert_any_call("%s output:\n%s", "Configuration batch", "config applied")

    def test_empty_command_list_is_rejected(self):
        connection = MagicMock()

        with self.assertRaisesRegex(ValueError, "No configuration commands"):
            self.configurator.execute_configuration_commands(
                connection,
                [],
                already_in_config_mode=False,
            )

    def test_batch_execution_raises_device_execution_error_on_error_indicator(self):
        connection = MagicMock()
        connection.send_config_set.return_value = "% Invalid input detected"

        with self.assertRaises(DeviceExecutionError):
            self.configurator.execute_configuration_batch(
                connection,
                ["bad command"],
                enter_config_mode=True,
            )

    def test_get_device_type_maps_aruba_cx_to_aoscx(self):
        self.assertEqual(self.configurator.get_device_type("aruba"), "aruba_aoscx")
        self.assertEqual(self.configurator.get_device_type("aruba_cx"), "aruba_aoscx")
        self.assertEqual(self.configurator.get_device_type("arubacx"), "aruba_aoscx")

    def test_get_device_type_preserves_explicit_aruba_os_aliases(self):
        self.assertEqual(self.configurator.get_device_type("aruba_os"), "aruba_os")
        self.assertEqual(self.configurator.get_device_type("aruba_osswitch"), "aruba_osswitch")


class ConditionalLogicTests(unittest.TestCase):
    def setUp(self):
        self.configurator = SwitchConfigurator("user", "pass")

    def test_simple_if_condition_true(self):
        """Test simple IF condition that evaluates to true"""
        commands = [
            "#IF {vendor} == cisco",
            "archive path flash:",
            "#ENDIF"
        ]
        switch = {"vendor": "cisco"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["archive path flash:"])

    def test_simple_if_condition_false(self):
        """Test simple IF condition that evaluates to false"""
        commands = [
            "#IF {vendor} == cisco",
            "archive path flash:",
            "#ENDIF"
        ]
        switch = {"vendor": "aruba"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [])

    def test_if_else_block(self):
        """Test IF/ELSE block with true condition"""
        commands = [
            "#IF {vendor} == cisco",
            "write memory",
            "#ELSE",
            "checkpoint confirm",
            "#ENDIF"
        ]
        switch = {"vendor": "cisco"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["write memory"])

    def test_if_else_block_false(self):
        """Test IF/ELSE block with false condition"""
        commands = [
            "#IF {vendor} == cisco",
            "write memory",
            "#ELSE",
            "checkpoint confirm",
            "#ENDIF"
        ]
        switch = {"vendor": "aruba"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["checkpoint confirm"])

    def test_and_operator(self):
        """Test AND operator with both conditions true"""
        commands = [
            "#IF {vendor} == cisco AND {mgmt_vlan} == 10",
            "interface vlan 10",
            "#ENDIF"
        ]
        switch = {"vendor": "cisco", "mgmt_vlan": "10"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["interface vlan 10"])

    def test_and_operator_false(self):
        """Test AND operator with one condition false"""
        commands = [
            "#IF {vendor} == cisco AND {mgmt_vlan} == 10",
            "interface vlan 10",
            "#ENDIF"
        ]
        switch = {"vendor": "cisco", "mgmt_vlan": "20"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [])

    def test_or_operator(self):
        """Test OR operator with one condition true"""
        commands = [
            "#IF {mgmt_vlan} == 10 OR {mgmt_vlan} == 20",
            "vlan config",
            "#ENDIF"
        ]
        switch = {"mgmt_vlan": "20"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["vlan config"])

    def test_greater_than_operator(self):
        """Test > operator"""
        commands = [
            "#IF {stack} > 0",
            "stack config",
            "#ENDIF"
        ]
        switch = {"stack": "3"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["stack config"])

    def test_greater_than_operator_false(self):
        """Test > operator with false condition"""
        commands = [
            "#IF {stack} > 0",
            "stack config",
            "#ENDIF"
        ]
        switch = {"stack": "0"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [])

    def test_for_loop_with_variable(self):
        """Test FOR loop with variable from CSV"""
        commands = [
            "#FOR i IN {stack}",
            "interface 1/{i}/1",
            "#ENDFOR"
        ]
        switch = {"stack": "3"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [
            "interface 1/1/1",
            "interface 1/2/1",
            "interface 1/3/1"
        ])

    def test_for_loop_with_literal(self):
        """Test FOR loop with literal number"""
        commands = [
            "#FOR member IN 2",
            "interface {member}/0/1",
            "#ENDFOR"
        ]
        switch = {}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [
            "interface 1/0/1",
            "interface 2/0/1"
        ])

    def test_for_loop_zero_iterations(self):
        """Test FOR loop with count of 0"""
        commands = [
            "#FOR i IN {stack}",
            "interface {i}/0/1",
            "#ENDFOR"
        ]
        switch = {"stack": "0"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [])

    def test_nested_if_blocks(self):
        """Test nested IF blocks"""
        commands = [
            "#IF {vendor} == cisco",
            "cisco command",
            "#IF {location} == Main",
            "location config",
            "#ENDIF",
            "#ENDIF"
        ]
        switch = {"vendor": "cisco", "location": "Main"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["cisco command", "location config"])

    def test_nested_if_blocks_inner_false(self):
        """Test nested IF blocks with inner condition false"""
        commands = [
            "#IF {vendor} == cisco",
            "cisco command",
            "#IF {location} == Main",
            "location config",
            "#ENDIF",
            "#ENDIF"
        ]
        switch = {"vendor": "cisco", "location": "Building A"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["cisco command"])

    def test_if_with_for_loop(self):
        """Test IF block containing FOR loop"""
        commands = [
            "#IF {stack} > 0",
            "#FOR member IN {stack}",
            "interface {member}/0/1",
            "#ENDFOR",
            "#ENDIF"
        ]
        switch = {"stack": "2"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [
            "interface 1/0/1",
            "interface 2/0/1"
        ])

    def test_regular_commands_mixed_with_conditionals(self):
        """Test regular commands mixed with conditional blocks"""
        commands = [
            "hostname test",
            "#IF {vendor} == cisco",
            "cisco config",
            "#ENDIF",
            "logging buffered 16384"
        ]
        switch = {"vendor": "cisco"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [
            "hostname test",
            "cisco config",
            "logging buffered 16384"
        ])

    def test_comments_are_ignored(self):
        """Test that regular comments are ignored"""
        commands = [
            "# This is a comment",
            "hostname test",
            "# Another comment",
            "logging buffered 16384"
        ]
        switch = {}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [
            "hostname test",
            "logging buffered 16384"
        ])

    def test_not_equal_operator(self):
        """Test != operator"""
        commands = [
            "#IF {vendor} != cisco",
            "aruba config",
            "#ENDIF"
        ]
        switch = {"vendor": "aruba"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["aruba config"])

    def test_missing_column_evaluates_to_empty(self):
        """Test that missing CSV columns evaluate to empty string"""
        commands = [
            "#IF {nonexistent} == value",
            "should not appear",
            "#ENDIF"
        ]
        switch = {}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [])

    def test_greater_than_or_equal_operator(self):
        """Test >= operator (verifies operator precedence fix)"""
        commands = [
            "#IF {count} >= 3",
            "config command",
            "#ENDIF"
        ]
        switch = {"count": "3"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["config command"])

    def test_less_than_or_equal_operator(self):
        """Test <= operator (verifies operator precedence fix)"""
        commands = [
            "#IF {count} <= 5",
            "config command",
            "#ENDIF"
        ]
        switch = {"count": "3"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["config command"])

    def test_commands_after_endif_are_included(self):
        """Test that commands after #ENDIF are not skipped (bug fix verification)"""
        commands = [
            "#IF {vendor} == cisco",
            "cisco command",
            "#ENDIF",
            "command after endif"
        ]
        switch = {"vendor": "cisco"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["cisco command", "command after endif"])

    def test_commands_after_endfor_are_included(self):
        """Test that commands after #ENDFOR are not skipped (bug fix verification)"""
        commands = [
            "#FOR i IN 2",
            "loop command {i}",
            "#ENDFOR",
            "command after endfor"
        ]
        switch = {}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["loop command 1", "loop command 2", "command after endfor"])

    @patch("switch_configurator.thread_safe_print")
    @patch("switch_configurator.logger.debug")
    def test_for_loop_debug_details_go_to_logger_not_console(self, mock_debug, mock_print):
        commands = [
            "#FOR i IN 2",
            "loop command {i}",
            "#ENDFOR"
        ]
        switch = {}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["loop command 1", "loop command 2"])
        mock_debug.assert_any_call(
            "FOR loop will iterate %s times (variable: %s, expression: %s)",
            2,
            "i",
            "2",
        )
        mock_print.assert_not_called()

    def test_variable_comparison_both_sides(self):
        """Test comparing two CSV columns (verifies right-side substitution fix)"""
        commands = [
            "#IF {vlan1} == {vlan2}",
            "vlans match",
            "#ENDIF"
        ]
        switch = {"vlan1": "10", "vlan2": "10"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, ["vlans match"])

    def test_variable_comparison_both_sides_not_equal(self):
        """Test comparing two CSV columns that don't match"""
        commands = [
            "#IF {vlan1} == {vlan2}",
            "should not appear",
            "#ENDIF"
        ]
        switch = {"vlan1": "10", "vlan2": "20"}

        result = self.configurator.parse_commands_with_conditionals(commands, switch)

        self.assertEqual(result, [])

    def test_unmatched_if_raises_command_syntax_error(self):
        commands = [
            "#IF {vendor} == cisco",
            "hostname test",
        ]

        with self.assertRaises(CommandSyntaxError):
            self.configurator.compile_command_template(commands)

    def test_unmatched_for_raises_command_syntax_error(self):
        commands = [
            "#FOR i IN 2",
            "interface {i}/0/1",
        ]

        with self.assertRaises(CommandSyntaxError):
            self.configurator.compile_command_template(commands)

    def test_malformed_for_raises_command_syntax_error(self):
        commands = [
            "#FOR i FROM 2",
            "interface {i}/0/1",
            "#ENDFOR",
        ]

        with self.assertRaises(CommandSyntaxError):
            self.configurator.compile_command_template(commands)

    def test_unexpected_else_raises_command_syntax_error(self):
        with self.assertRaises(CommandSyntaxError):
            self.configurator.compile_command_template(["#ELSE"])


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.configurator = SwitchConfigurator("user", "pass")

    def test_missing_required_csv_column_raises_config_error_without_sys_exit(self):
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "switches.csv"
            csv_path.write_text("switchname,ip address\nsw1,10.0.0.1\n", encoding="utf-8")

            with patch("switch_configurator.sys.exit") as mock_exit:
                with self.assertRaises(ConfigError):
                    self.configurator.load_switches(str(csv_path))

            mock_exit.assert_not_called()

    def test_empty_required_csv_value_raises_config_error_without_sys_exit(self):
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "switches.csv"
            csv_path.write_text("switchname,ip address,vendor\nsw1,,cisco\n", encoding="utf-8")

            with patch("switch_configurator.sys.exit") as mock_exit:
                with self.assertRaises(ConfigError):
                    self.configurator.load_switches(str(csv_path))

            mock_exit.assert_not_called()

    def test_valid_switch_csv_loads_switch_records(self):
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "switches.csv"
            csv_path.write_text(
                "switchname,ip address,vendor,location\nsw1,10.0.0.1,cisco,lab\n",
                encoding="utf-8",
            )

            switches = self.configurator.load_switches(str(csv_path))

        self.assertEqual(len(switches), 1)
        self.assertIsInstance(switches[0], SwitchRecord)
        self.assertEqual(switches[0].get("location"), "lab")

    def test_prompt_handlers_parse_special_responses(self):
        with TemporaryDirectory() as temp_dir:
            prompt_path = Path(temp_dir) / "prompthandling.txt"
            prompt_path.write_text(
                "reload|YES\ncopy running-config|RETURN\ndelete|NO\n",
                encoding="utf-8",
            )

            handlers = self.configurator.load_prompt_handlers(str(prompt_path))

        self.assertEqual(handlers["reload"], "yes\n")
        self.assertEqual(handlers["copy running-config"], "\n")
        self.assertEqual(handlers["delete"], "no\n")

    def test_load_commands_validates_syntax_before_device_connections(self):
        with TemporaryDirectory() as temp_dir:
            commands_path = Path(temp_dir) / "commands.txt"
            commands_path.write_text("#IF {vendor} == cisco\nhostname test\n", encoding="utf-8")

            with self.assertRaises(CommandSyntaxError):
                self.configurator.load_commands(str(commands_path))


class VendorFlowTests(unittest.TestCase):
    def setUp(self):
        self.configurator = SwitchConfigurator("user", "pass")

    def test_cisco_archive_already_configured_success_path(self):
        connection = MagicMock()
        connection.send_command.side_effect = [
            "Archive path: flash:/archive",
            "configure terminal revert timer 2",
            "confirm ok",
            "Building configuration...\n[OK]",
        ]
        connection.send_config_set.return_value = "config applied"

        success, output = self.configurator.configure_cisco_ios_xe(connection, ["hostname test"])

        self.assertTrue(success)
        self.assertIn("config applied", output)
        connection.exit_config_mode.assert_called_once()

    def test_cisco_archive_auto_setup_uses_config_mode_batch(self):
        connection = MagicMock()
        connection.send_command.side_effect = [
            "No archive configured",
            "Building configuration...\n[OK]",
            "Archive path: flash:/archive",
            "configure terminal revert timer 2",
            "confirm ok",
            "Building configuration...\n[OK]",
        ]
        connection.send_config_set.side_effect = [
            "archive configured",
            "config applied",
        ]

        success, output = self.configurator.configure_cisco_ios_xe(connection, ["hostname test"])

        self.assertTrue(success)
        self.assertIn("archive configured", output)
        self.assertEqual(
            connection.send_config_set.call_args_list[0],
            unittest.mock.call(
                ["archive", "path flash:archive-config", "maximum 10"],
                enter_config_mode=True,
                exit_config_mode=True,
                cmd_verify=False,
                read_timeout=240,
            ),
        )

    def test_cisco_save_failure_returns_false(self):
        connection = MagicMock()
        connection.send_command.side_effect = [
            "Archive path: flash:/archive",
            "configure terminal revert timer 2",
            "confirm ok",
            "% Error saving configuration",
        ]
        connection.send_config_set.return_value = "config applied"

        success, _output = self.configurator.configure_cisco_ios_xe(connection, ["hostname test"])

        self.assertFalse(success)

    def test_aruba_confirm_failure_returns_false(self):
        connection = MagicMock()
        connection.send_command.side_effect = [
            "checkpoint started",
            "% Invalid input detected",
        ]
        connection.send_config_set.return_value = "config applied"

        success, _output = self.configurator.configure_aruba_cx(connection, ["hostname test"])

        self.assertFalse(success)
        connection.exit_config_mode.assert_called_once()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.configurator = SwitchConfigurator("user", "pass")

    def test_run_switches_returns_success_and_failure_results(self):
        switches = [
            SwitchRecord.from_mapping({"switchname": "sw1", "ip address": "10.0.0.1", "vendor": "cisco"}),
            SwitchRecord.from_mapping({"switchname": "sw2", "ip address": "10.0.0.2", "vendor": "aruba"}),
        ]
        progress_calls = []

        with patch.object(self.configurator, "configure_switch", side_effect=[True, False]):
            results = self.configurator.run_switches(
                switches,
                max_workers=2,
                progress_callback=lambda: progress_calls.append("tick"),
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for result in results if result.success), 1)
        self.assertEqual(len(progress_calls), 2)

    def test_run_switches_worker_exception_becomes_failed_result(self):
        switches = [
            SwitchRecord.from_mapping({"switchname": "sw1", "ip address": "10.0.0.1", "vendor": "cisco"}),
        ]

        with patch.object(self.configurator, "configure_switch", side_effect=RuntimeError("boom")):
            results = self.configurator.run_switches(switches)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIn("boom", results[0].error)

    def test_run_switches_timeout_marks_unfinished_switches_failed(self):
        switches = [
            SwitchRecord.from_mapping({"switchname": "sw1", "ip address": "10.0.0.1", "vendor": "cisco"}),
        ]

        with patch.object(self.configurator, "configure_switch", return_value=True):
            with patch("switch_configurator.as_completed", side_effect=FuturesTimeoutError):
                results = self.configurator.run_switches(switches)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIn("timeout", results[0].error)


class LoggingTests(unittest.TestCase):
    def test_import_does_not_install_file_handler(self):
        self.assertFalse(any(isinstance(handler, logging.FileHandler) for handler in logger.handlers))


class TextualAppTests(unittest.TestCase):
    def test_app_mounts_core_widgets_headlessly(self):
        async def run_app() -> None:
            app = SwitchConfiguratorApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIsNotNone(app.query_one("#username"))
                self.assertIsNotNone(app.query_one("#results"))
                self.assertIsNotNone(app.query_one("#events"))

        asyncio.run(run_app())


class GitIgnoreTests(unittest.TestCase):
    def test_logs_directory_is_gitignored(self):
        gitignore = Path(__file__).resolve().parents[1] / ".gitignore"

        ignored_patterns = gitignore.read_text(encoding="utf-8").splitlines()

        self.assertIn("logs/", ignored_patterns)


if __name__ == "__main__":
    unittest.main()
