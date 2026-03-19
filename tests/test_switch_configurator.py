import unittest
from unittest.mock import MagicMock

from switch_configurator import SwitchConfigurator


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
                ),
                unittest.mock.call(
                    ["logging buffered 16384"],
                    enter_config_mode=False,
                    exit_config_mode=False,
                    cmd_verify=False,
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
        )

    def test_empty_command_list_is_rejected(self):
        connection = MagicMock()

        with self.assertRaisesRegex(ValueError, "No configuration commands"):
            self.configurator.execute_configuration_commands(
                connection,
                [],
                already_in_config_mode=False,
            )


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


if __name__ == "__main__":
    unittest.main()
