from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        """Check bot latency."""
        await ctx.send(f"Pong! {round(self.bot.latency * 1000)}ms")

    @commands.command()
    async def calculate(self, ctx, num1: float, operator: str, num2: float):
        """Perform basic arithmetic calculations.

        Arguments:
            num1 (float): The first number to use in the calculation.
            operator (str): The operation to perform (+, -, *, /).
            num2 (float): The second number to use in the calculation."""
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


async def setup(bot):  # Ensure the setup is asynchronous
    await bot.add_cog(General(bot))
