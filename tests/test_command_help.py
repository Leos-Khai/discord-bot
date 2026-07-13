import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

from discord.ext import commands

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from command_help import (
    HelpfulHelpCommand,
    apply_parameter_descriptions,
    describe_parameters,
)


class CommandHelpTests(unittest.TestCase):
    def test_shows_the_full_command_path_and_signature(self):
        @commands.group(name="notifications")
        async def notifications(ctx):
            pass

        @notifications.group(name="youtube")
        async def youtube(ctx):
            pass

        @youtube.command(name="add")
        async def add(ctx, channel_id: str):
            pass

        help_command = HelpfulHelpCommand()
        help_command.context = SimpleNamespace(clean_prefix="!")

        self.assertEqual(
            "!notifications youtube add <channel_id>",
            help_command.get_command_signature(add),
        )

    def test_renders_canonical_help_without_a_hard_coded_usage_prefix(self):
        from cogs.notifications import Notifications

        cog = Notifications.__new__(Notifications)
        apply_parameter_descriptions(cog)
        help_command = HelpfulHelpCommand()
        help_command.context = SimpleNamespace(clean_prefix="?")
        help_command.add_command_formatting(cog.youtube_add)
        rendered = "\n".join(help_command.paginator.pages)

        self.assertIn("?notifications youtube add <channel_id> [channel]", rendered)
        self.assertIn("YouTube channel ID, channel URL, or @handle to track.", rendered)
        self.assertIn("Optional text channel for this subscription; otherwise use the default.", rendered)
        self.assertNotIn("No description given", rendered)
        self.assertNotIn("Usage:", rendered)

    def test_attaches_descriptions_to_prefix_help_parameters(self):
        @describe_parameters(channel_id="A YouTube channel ID, URL, or @handle.")
        @commands.command()
        async def add(ctx, channel_id: str):
            pass

        self.assertEqual(
            "A YouTube channel ID, URL, or @handle.",
            add.params["channel_id"].description,
        )

    def test_cog_command_copies_keep_parameter_descriptions(self):
        from cogs.notifications import Notifications

        cog = Notifications.__new__(Notifications)
        apply_parameter_descriptions(cog)

        self.assertEqual(
            "YouTube channel ID, channel URL, or @handle to track.",
            cog.youtube_add.params["channel_id"].description,
        )

    def test_all_user_facing_command_parameters_have_descriptions(self):
        from cogs.admin import Admin
        from cogs.general import General
        from cogs.music import MusicCommands
        from cogs.notifications import Notifications
        from cogs.tts import TtsCommands

        missing = []
        for cog_class in (Admin, General, MusicCommands, Notifications, TtsCommands):
            cog = cog_class.__new__(cog_class)
            apply_parameter_descriptions(cog)
            for command in cog.__cog_commands__:
                for parameter in command.clean_params.values():
                    if parameter.description is None:
                        missing.append(f"{command.qualified_name}.{parameter.name}")

        self.assertEqual([], missing)

        missing_help = []
        for cog_class in (Admin, General, MusicCommands, Notifications, TtsCommands):
            cog = cog_class.__new__(cog_class)
            apply_parameter_descriptions(cog)
            for command in cog.__cog_commands__:
                if not (command.help or command.brief or command.description):
                    missing_help.append(command.qualified_name)

        self.assertEqual([], missing_help)

    def test_hybrid_commands_share_descriptions_with_slash_commands(self):
        from cogs.music import MusicCommands
        from cogs.tts import TtsCommands

        mismatches = []
        for command in (
            MusicCommands.play,
            MusicCommands.search,
            MusicCommands.remove,
            MusicCommands.volume,
            TtsCommands.tts,
            TtsCommands.setttsvoice,
            TtsCommands.listvoice,
        ):
            for parameter in command.app_command.parameters:
                prefix_description = command.params[parameter.name].description
                if parameter.description != prefix_description:
                    mismatches.append(f"{command.qualified_name}.{parameter.name}")

        self.assertEqual([], mismatches)
