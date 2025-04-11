import discord
from discord.ext import commands
from cogs.general.admin import is_admin


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @is_admin()
    async def ping(self, ctx):
        await ctx.send(f"Pong! {round(self.bot.latency * 1000)}ms")

    @commands.command()
    async def calculate(self, ctx, num1: float, operator: str, num2: float):
        try:
            if operator == "+":
                result = num1 + num2
            elif operator == "-":
                result = num1 - num2
            elif operator == "*":
                result = num1 * num2
            elif operator == "/":
                if num2 == 0:
                    await ctx.send("Error: Division by zero is not allowed.")
                    return
                result = num1 / num2
            else:
                await ctx.send("Invalid operator. Use +, -, *, or /.")
                return
            await ctx.send(f"The result of `{num1} {operator} {num2}` is `{result}`.")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")
            print(f"Error in calculate command: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send("You do not have permission to use this command.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(General(bot))
