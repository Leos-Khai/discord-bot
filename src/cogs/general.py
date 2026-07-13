import discord
from discord.ext import commands
from command_help import apply_parameter_descriptions, describe_parameters
from cogs.admin import is_admin


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        apply_parameter_descriptions(self)

    @commands.command(help="Shows the bot's latency in milliseconds.\nUsage: !ping")
    @is_admin()
    async def ping(self, ctx):
        await ctx.send(f"Pong! {round(self.bot.latency * 1000)}ms")

    @describe_parameters(
        num1="First number in the calculation.",
        operator="Arithmetic operator: +, -, *, or /.",
        num2="Second number in the calculation.",
    )
    @commands.command(
        help="Performs basic arithmetic calculations.\nUsage: !calculate number operator number\nOperators: + - * /\nExample: !calculate 10 * 5"
    )
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


async def setup(bot):
    await bot.add_cog(General(bot))
