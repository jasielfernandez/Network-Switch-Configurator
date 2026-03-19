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


if __name__ == "__main__":
    unittest.main()
