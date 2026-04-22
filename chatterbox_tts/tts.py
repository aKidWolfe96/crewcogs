import discord
import aiohttp
import asyncio
import tempfile
import os
from redbot.core import commands, Config

CONFIG = Config.get_conf(None, identifier=5544332211)
CONFIG.register_guild(voice_id="Ryan.wav")

VOICES = [
    "Abigail.wav", "Adrian.wav", "Alexander.wav", "Alice.wav",
    "Austin.wav", "Axel.wav", "Connor.wav", "Cora.wav",
    "Elena.wav", "Eli.wav", "Emily.wav", "Everett.wav",
    "Gabriel.wav", "Gianna.wav", "Henry.wav", "Ian.wav",
    "Jade.wav", "Jeremiah.wav", "Jordan.wav", "Julian.wav",
    "Layla.wav", "Leonardo.wav", "Michael.wav", "Miles.wav",
    "Olivia.wav", "Ryan.wav", "Taylor.wav", "Thomas.wav"
]


class ChatterboxTTS(commands.Cog):
    """TTS via Chatterbox — speaks in your voice channel, pauses Red Audio."""

    CHATTERBOX_URL = "http://192.168.1.227:8000/tts"

    def __init__(self, bot):
        self.bot = bot
        self._tts_queue = {}
        self._workers = {}

    @commands.command()
    @commands.guild_only()
    async def tts(self, ctx, channel: discord.VoiceChannel = None, *, text: str):
        """Speak text in a voice channel via Chatterbox TTS.

        Usage:
          [p]tts <text>                  — speaks in your current voice channel
          [p]tts #channel <text>         — speaks in the specified voice channel
        """
        # If no channel was specified, use the author's current voice channel
        if channel is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                return await ctx.send("You need to be in a voice channel, or specify one like `!tts #channel your message`.")
            channel = ctx.author.voice.channel

        guild_id = ctx.guild.id

        if guild_id not in self._tts_queue:
            self._tts_queue[guild_id] = asyncio.Queue()

        await self._tts_queue[guild_id].put((ctx, channel, text))

        if guild_id not in self._workers or self._workers[guild_id].done():
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id))

    @commands.command()
    @commands.guild_only()
    async def ttsvoice(self, ctx, *, voice: str = None):
        """Set or check the TTS voice. Use [p]ttsvoice list to see all options."""
        if voice is None or voice.lower() == "list":
            names = ", ".join(v.replace(".wav", "") for v in VOICES)
            return await ctx.send(f"**Available voices:**\n{names}\n\nUse `[p]ttsvoice <name>` to set one.")

        filename = voice if voice.endswith(".wav") else voice + ".wav"
        match = next((v for v in VOICES if v.lower() == filename.lower()), None)
        if not match:
            return await ctx.send(f"Voice `{voice}` not found. Use `[p]ttsvoice list` to see all options.")

        await CONFIG.guild(ctx.guild).voice_id.set(match)
        await ctx.send(f"TTS voice set to **{match.replace('.wav', '')}**.")

    async def _worker(self, guild_id):
        queue = self._tts_queue[guild_id]
        while not queue.empty():
            ctx, channel, text = await queue.get()
            try:
                await self._speak(ctx, channel, text)
            except Exception as exc:
                await ctx.send(f"TTS error: {exc}")
            queue.task_done()

    def _get_audio_cog(self):
        return self.bot.cogs.get("Audio")

    async def _pause_audio(self, ctx):
        audio = self._get_audio_cog()
        if not audio:
            return False
        try:
            player = audio.lavalink.player_manager.get(ctx.guild.id)
        except Exception:
            return False
        if player and player.is_playing and not player.paused:
            await player.set_pause(True)
            return True
        return False

    async def _resume_audio(self, ctx, channel):
        audio = self._get_audio_cog()
        if not audio:
            return
        try:
            player = audio.lavalink.player_manager.get(ctx.guild.id)
            if player:
                await player.connect(channel_id=str(channel.id))
                await asyncio.sleep(0.5)
                await player.set_pause(False)
                return
        except Exception:
            pass
        try:
            resume_cmd = self.bot.get_command("resume")
            if resume_cmd:
                await ctx.invoke(resume_cmd)
        except Exception:
            await ctx.send("Music was paused for TTS — run `[p]resume` to continue.")

    async def _speak(self, ctx, channel, text):
        guild = ctx.guild

        audio_was_paused = await self._pause_audio(ctx)

        vc = guild.voice_client
        if vc:
            await vc.disconnect(force=True)
            await asyncio.sleep(0.3)

        vc = await channel.connect()
        audio_path = await self._fetch_tts(ctx, text)

        done_event = asyncio.Event()

        def after_play(error):
            if error:
                self.bot.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(ctx.send(f"Playback error: {error}"))
                )
            done_event.set()

        vc.play(discord.FFmpegPCMAudio(audio_path), after=after_play)
        await done_event.wait()

        try:
            os.unlink(audio_path)
        except OSError:
            pass

        await vc.disconnect(force=True)
        await asyncio.sleep(0.75)

        if audio_was_paused:
            await self._resume_audio(ctx, channel)

    async def _fetch_tts(self, ctx, text):
        voice_id = await CONFIG.guild(ctx.guild).voice_id()
        payload = {
            "text": text,
            "voice_mode": "predefined",
            "predefined_voice_id": voice_id
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.CHATTERBOX_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Chatterbox returned HTTP {resp.status}: {body[:200]}")
                content_type = resp.headers.get("Content-Type", "")
                suffix = ".mp3" if "mpeg" in content_type else ".wav"
                audio_bytes = await resp.read()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(audio_bytes)
        tmp.close()
        return tmp.name

    def cog_unload(self):
        for task in self._workers.values():
            task.cancel()


def setup(bot):
    bot.add_cog(ChatterboxTTS(bot))
