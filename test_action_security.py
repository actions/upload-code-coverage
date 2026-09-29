import tempfile
import unittest
from pathlib import Path

import yaml


def action_run_commands(action_path: Path) -> list[str]:
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    commands = []

    def collect_run_commands(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "run" and isinstance(child, str):
                    commands.append(child)
                collect_run_commands(child)
        elif isinstance(value, list):
            for child in value:
                collect_run_commands(child)

    collect_run_commands(action)
    return commands


class ActionSecurityTests(unittest.TestCase):
    def test_run_command_extraction_handles_inline_and_block_values(self):
        with tempfile.TemporaryDirectory() as directory:
            action_path = Path(directory) / "action.yml"
            action_path.write_text(
                """
steps:
  - env:
      REF: ${{ github.ref }}
    run: echo "$REF"
  - run: |
      echo "${{ github.ref_name }}"
""".lstrip(),
                encoding="utf-8",
            )

            commands = action_run_commands(action_path)

        self.assertEqual(2, len(commands))
        self.assertNotIn("${{", commands[0])
        self.assertIn("${{ github.ref_name }}", commands[1])

    def test_run_commands_do_not_interpolate_expressions(self):
        action_path = Path(__file__).with_name("action.yml")
        commands = action_run_commands(action_path)

        self.assertGreater(len(commands), 0)
        for command in commands:
            with self.subTest(command=command):
                self.assertNotIn("${{", command)


if __name__ == "__main__":
    unittest.main()
