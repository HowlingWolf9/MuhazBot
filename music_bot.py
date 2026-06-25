import discord
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        await self.load_extension('music_cog')

bot = MusicBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    synced = await bot.tree.sync()
    print(f'Synced {len(synced)} commands.')

@bot.command(name='sync')
@commands.is_owner()
async def sync(ctx):
    """Sync the slash commands globally."""
    synced = await bot.tree.sync()
    await ctx.send(f"Synced {len(synced)} commands.")

if __name__ == '__main__':
    bot.run(os.getenv('DISCORD_TOKEN'))
