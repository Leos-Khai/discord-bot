from __future__ import annotations

from collections.abc import Callable, Mapping

from discord.ext import commands


class HelpfulHelpCommand(commands.DefaultHelpCommand):
    """Renders command help with its complete invocation path."""

    def __init__(self):
        super().__init__(
            command_attrs={"help": "Show help for a command or command group."},
            show_parameter_descriptions=True,
            width=100,
        )

    def get_command_signature(self, command: commands.Command, /) -> str:
        signature = f"{self.context.clean_prefix}{command.qualified_name} {command.signature}"
        return signature.rstrip()

    def add_command_formatting(self, command: commands.Command, /) -> None:
        if command.description:
            self.paginator.add_line(command.description, empty=True)

        self.paginator.add_line(self.get_command_signature(command), empty=True)

        if command.help:
            help_text = "\n".join(
                line
                for line in command.help.splitlines()
                if not line.strip().casefold().startswith("usage:")
            )
            if help_text:
                self.paginator.add_line(help_text, empty=True)

        self.add_command_arguments(command)


def describe_parameters(
    **descriptions: str,
) -> Callable[[commands.Command], commands.Command]:
    """Attach prefix-help descriptions without changing a command callback."""

    def decorate(command: commands.Command) -> commands.Command:
        unknown = descriptions.keys() - command.params.keys()
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown command parameter(s): {names}")
        for name, description in descriptions.items():
            command.params[name] = command.params[name].replace(description=description)
        command.callback.__command_parameter_descriptions__ = descriptions
        return command

    return decorate


def apply_parameter_descriptions(cog: commands.Cog) -> None:
    """Restore descriptions after discord.py copies a Cog's commands."""

    for command in cog.walk_commands():
        descriptions: Mapping[str, str] = getattr(
            command.callback, "__command_parameter_descriptions__", {}
        )
        for name, description in descriptions.items():
            command.params[name] = command.params[name].replace(description=description)
