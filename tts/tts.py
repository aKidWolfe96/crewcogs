import discord
import aiohttp
import asyncio
import tempfile
import os
from redbot.core import commands
from redbot.core.bot import Red


class ChatterboxTTS(commands.Cog):
    """TTS via Chatterbox — plays in your voice channel, pauses Red Audio."""

    CHATTERBOX_URL = "http://192.168.1.227:8000/tts"

    def __init__(self, bot: Red):
        self.bot = bot
        self._tts_queue: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}

    @commands.command(name="tts")
    @commands.guild_only()
    async def tts(self, ctx: commands.Context, *, text: str):
        """Speak text in your current voice channel via Chatterbox TTS."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel first.")

        channel = ctx.author.voice.channel
        guild_id = ctx.guild.id

        if guild_id not in self._tts_queue:
            self._tts_queue[guild_id] = asyncio.Queue()

        await self._tts_queue[guild_id].put((ctx, channel, text))

        if guild_id not in self._workers or self._workers[guild_id].done():
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id))

    async def _worker(self, guild_id: int):
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

    async def _pause_audio(self, ctx: commands.Context) -> bool:
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

    async def _resume_audio(self, ctx: commands.Context, channel: discord.VoiceChannel):
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

    async def _speak(self, ctx: commands.Context, channel: discord.VoiceChannel, text: str):
        guild = ctx.guild

        audio_was_paused = await self._pause_audio(ctx)

        vc = guild.voice_client
        if vc:
            await vc.disconnect(force=True)
            await asyncio.sleep(0.3)

        vc = await channel.connect()
        audio_path = await self._fetch_tts(text)

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

    async def _fetch_tts(self, text: str) -> str:
        payload = {"text": text}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.CHATTERBOX_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
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
