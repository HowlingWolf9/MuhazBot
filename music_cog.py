import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio

ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0'
}
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')
        self.requester = None

    @classmethod
    async def extract_info(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        if not url.startswith('http'):
            url = f"ytsearch:{url}"
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        if 'entries' in data:
            data = data['entries'][0]
        return data

    @classmethod
    async def create_source(cls, url, *, loop=None):
        data = await cls.extract_info(url, loop=loop)
        return cls(discord.FFmpegPCMAudio(data['url'], **ffmpeg_options), data=data)

class Song:
    def __init__(self, data, requester):
        self.data = data
        self.requester = requester
        self.title = data.get('title')
        self.url = data.get('webpage_url')
        self.thumbnail = data.get('thumbnail')

class MusicPlayer:
    def __init__(self, interaction: discord.Interaction, cog):
        self.bot = interaction.client
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.cog = cog
        
        self.queue = asyncio.Queue()
        self.next = asyncio.Event()
        
        self.current = None
        self.volume = 0.5

        self.player_task = self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            self.next.clear()
            
            try:
                # Wait 5 minutes for the next song before disconnecting
                song = await asyncio.wait_for(self.queue.get(), timeout=300)
            except asyncio.TimeoutError:
                return self.destroy(self.guild)

            # Re-fetch stream URL to avoid expiration issues
            try:
                source = await YTDLSource.create_source(song.url, loop=self.bot.loop)
            except Exception as e:
                # Skip to next song if extraction fails
                self.bot.loop.call_soon_threadsafe(self.next.set)
                continue

            self.current = song
            self.current.source = source
            
            source.volume = self.volume
            
            if not self.guild.voice_client:
                return self.destroy(self.guild)
                
            self.guild.voice_client.play(
                source, 
                after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set)
            )
            
            embed = discord.Embed(title="Now Playing", description=f"[{song.title}]({song.url})", color=discord.Color.green())
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.add_field(name="Requested by", value=song.requester.mention)
            
            try:
                await self.channel.send(embed=embed)
            except Exception:
                pass
            
            await self.next.wait()
            self.current = None

    def destroy(self, guild):
        return self.bot.loop.create_task(self.cog.cleanup(guild))

class SearchSelect(discord.ui.Select):
    def __init__(self, entries, cog):
        self.entries = entries
        self.cog = cog
        options = []
        for i, entry in enumerate(entries):
            title = entry.get('title', 'Unknown Title')
            if len(title) > 90:
                title = title[:90] + "..."
            duration = entry.get('duration')
            if duration:
                mins, secs = divmod(duration, 60)
                desc = f"{mins}:{secs:02d}"
            else:
                desc = "Unknown duration"
            options.append(discord.SelectOption(label=f"{i+1}. {title}", description=desc, value=str(i)))
            
        super().__init__(placeholder="Choose a song to play...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        selected_index = int(self.values[0])
        selected_entry = self.entries[selected_index]
        
        player = self.cog.get_player(interaction)
        player.channel = interaction.channel
        
        song = Song(selected_entry, interaction.user)
        await player.queue.put(song)
        
        if player.current:
            embed = discord.Embed(title="Added to Queue", description=f"[{song.title}]({song.url})", color=discord.Color.blue())
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"⏳ Loading track: **{song.title}**...")
            
        self.disabled = True
        await interaction.message.edit(view=self.view)

class SearchView(discord.ui.View):
    def __init__(self, entries, user, cog):
        super().__init__(timeout=60)
        self.user = user
        self.add_item(SearchSelect(entries, cog))
        
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def get_player(self, interaction):
        try:
            player = self.players[interaction.guild.id]
        except KeyError:
            player = MusicPlayer(interaction, self)
            self.players[interaction.guild.id] = player
        return player

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass
        try:
            player = self.players.pop(guild.id)
            player.player_task.cancel()
        except KeyError:
            pass

    async def verify_voice(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice:
            await interaction.response.send_message("🔴 You need to join a voice channel first!", ephemeral=True)
            return False
        if interaction.guild.voice_client and interaction.guild.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message("🔴 You must be in the same voice channel as the bot to use this.", ephemeral=True)
            return False
        return True

    @app_commands.command(name='play', description="Play a song or add it to the queue")
    async def play(self, interaction: discord.Interaction, url: str):
        if not await self.verify_voice(interaction): return
            
        await interaction.response.defer()

        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await interaction.user.voice.channel.connect()

        if "spotify.com" in url:
            return await interaction.followup.send("⚠️ Spotify URLs aren't supported natively. Just type the song name instead!")

        try:
            data = await YTDLSource.extract_info(url, loop=self.bot.loop)
        except Exception as e:
            return await interaction.followup.send(f"❌ An error occurred: {str(e)}")

        song = Song(data, interaction.user)
        player = self.get_player(interaction)
        player.channel = interaction.channel
        
        await player.queue.put(song)
        
        if player.current:
            embed = discord.Embed(title="Added to Queue", description=f"[{song.title}]({song.url})", color=discord.Color.blue())
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("⏳ Loading track...")

    @app_commands.command(name='search', description="Search for a song and choose from the results")
    async def search(self, interaction: discord.Interaction, query: str):
        if not await self.verify_voice(interaction): return
            
        await interaction.response.defer()

        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await interaction.user.voice.channel.connect()

        try:
            url = f"ytsearch5:{query}"
            loop = self.bot.loop or asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        except Exception as e:
            return await interaction.followup.send(f"❌ An error occurred: {str(e)}")

        if 'entries' not in data or not data['entries']:
            return await interaction.followup.send("❌ No results found.")

        entries = data['entries'][:5]
        view = SearchView(entries, interaction.user, self)
        await interaction.followup.send("Please select a song:", view=view)

    @app_commands.command(name='skip', description="Skip the currently playing song")
    async def skip(self, interaction: discord.Interaction):
        if not await self.verify_voice(interaction): return
        
        voice_client = interaction.guild.voice_client
        if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
            return await interaction.response.send_message("There is nothing playing to skip.", ephemeral=True)
            
        voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped the current song.")

    @app_commands.command(name='pause', description="Pause the music")
    async def pause(self, interaction: discord.Interaction):
        if not await self.verify_voice(interaction): return
        
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.pause()
            await interaction.response.send_message("⏸️ Paused the music.")
        else:
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)

    @app_commands.command(name='resume', description="Resume the music")
    async def resume(self, interaction: discord.Interaction):
        if not await self.verify_voice(interaction): return
        
        if interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
            interaction.guild.voice_client.resume()
            await interaction.response.send_message("▶️ Resumed the music.")
        else:
            await interaction.response.send_message("The music is not paused.", ephemeral=True)

    @app_commands.command(name='queue', description="Show the current music queue")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction)
        if player.queue.empty():
            return await interaction.response.send_message("The queue is currently empty.")
            
        upcoming = list(player.queue._queue)
        fmt = '\n'.join(f"{i+1}. **{song.title}**" for i, song in enumerate(upcoming[:10]))
        
        embed = discord.Embed(title=f"Queue for {interaction.guild.name}", description=fmt, color=discord.Color.blue())
        if len(upcoming) > 10:
            embed.set_footer(text=f"And {len(upcoming) - 10} more...")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='nowplaying', description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        try:
            player = self.players[interaction.guild.id]
        except KeyError:
            return await interaction.response.send_message("There is no music playing right now.")
            
        if not player.current:
            return await interaction.response.send_message("There is no music playing right now.")
            
        embed = discord.Embed(title="Now Playing", description=f"[{player.current.title}]({player.current.url})", color=discord.Color.green())
        if player.current.thumbnail:
            embed.set_thumbnail(url=player.current.thumbnail)
        embed.add_field(name="Requested by", value=player.current.requester.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='volume', description="Set the volume of the bot (1-100)")
    async def volume(self, interaction: discord.Interaction, vol: int):
        if not await self.verify_voice(interaction): return
        
        if not 1 <= vol <= 100:
            return await interaction.response.send_message("Please enter a value between 1 and 100.", ephemeral=True)
            
        player = self.get_player(interaction)
        if interaction.guild.voice_client and getattr(interaction.guild.voice_client, 'source', None):
            interaction.guild.voice_client.source.volume = vol / 100.0
            
        player.volume = vol / 100.0
        await interaction.response.send_message(f"🔊 Changed volume to {vol}%")

    @app_commands.command(name='leave', description="Clear the queue and leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        if not await self.verify_voice(interaction): return
        
        if interaction.guild.voice_client:
            await self.cleanup(interaction.guild)
            await interaction.response.send_message("🛑 Cleared the queue and disconnected.")
        else:
            await interaction.response.send_message("I am not connected to a voice channel.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(MusicCog(bot))
