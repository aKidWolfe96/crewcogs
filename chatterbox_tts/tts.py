import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from redbot.core import Config, commands

try:  # red-lavalink ships with Red and is what the Audio cog uses.
    import lavalink
except ImportError:  # pragma: no cover - Red always installs it
    lavalink = None

log = logging.getLogger("red.crewcogs.chatterbox_tts")

DEFAULT_URL = "http://192.168.1.227:8000"
VOICE_FILE = "voice.wav"   # the one voice this cog speaks with
DEFAULT_MAX_CHARS = 400

MAX_QUEUE = 10            # pending messages per server
VOICE_CHECK_SECONDS = 300
TTS_TIMEOUT = 120         # seconds Chatterbox gets to generate one message
PLAYBACK_TIMEOUT = 600    # hard stop for a single clip

CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
LINK_RE = re.compile(r"https?://\S+")
CHANNEL_REF_RE = re.compile(r"<#(\d+)>|(\d{15,21})")


def _strip_suffix(text: str, suffix: str) -> str:
    return text[: -len(suffix)] if text.endswith(suffix) else text


class VoiceChannelRef(commands.Converter):
    """A voice channel given as a #mention or ID.

    Names are deliberately not accepted, so a message that starts with a word
    that happens to be a channel name ("music is great") is spoken as-is.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> discord.VoiceChannel:
        match = CHANNEL_REF_RE.fullmatch(argument)
        if not match:
            raise commands.BadArgument("Not a channel mention.")
        channel = ctx.guild.get_channel(int(match.group(1) or match.group(2)))
        if not isinstance(channel, discord.VoiceChannel):
            raise commands.BadArgument("Not a voice channel.")
        return channel


@dataclass
class TTSRequest:
    ctx: commands.Context
    channel: discord.VoiceChannel
    text: str


@dataclass
class MusicSnapshot:
    """Everything needed to put Red's Audio player back the way it was."""

    channel: discord.VoiceChannel
    current: Any
    position: int
    paused: bool
    queue: List[Any]
    repeat: bool
    shuffle: bool
    shuffle_bumped: bool
    volume: int
    metadata: Dict[Any, Any] = field(default_factory=dict)


class ChatterboxTTS(commands.Cog):
    """TTS via Chatterbox: speaks in a voice channel and hands music back to Red's Audio cog."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=5544332211, force_registration=True)
        self.config.register_global(url=DEFAULT_URL, max_chars=DEFAULT_MAX_CHARS)

        self._session: Optional[aiohttp.ClientSession] = None
        self._queues: Dict[int, asyncio.Queue] = {}
        self._workers: Dict[int, asyncio.Task] = {}
        self._voice_mode: Optional[str] = None  # "predefined" or "clone", see _voice_payload
        self._voice_name = VOICE_FILE
        self._voice_mode_at = 0.0

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        workers = [t for t in self._workers.values() if not t.done()]
        for task in workers:
            task.cancel()
        if workers:
            # Let each worker's cleanup run: leave voice and give music back.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True), timeout=15
                )
            except asyncio.TimeoutError:
                pass
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs):
        return  # No per-user data is stored.

    # ------------------------------------------------------------------ commands

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def tts(
        self,
        ctx: commands.Context,
        channel: Optional[VoiceChannelRef] = None,
        *,
        text: commands.clean_content(fix_channel_mentions=True),
    ):
        """Speak text in a voice channel via Chatterbox TTS.

        Usage:
          `[p]tts <text>` - speaks in your current voice channel
          `[p]tts #channel <text>` - speaks in that voice channel (mention or ID)
        """
        if channel is None:
            if not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
                return await ctx.send(
                    "You need to be in a voice channel, or pick one like "
                    f"`{ctx.clean_prefix}tts #channel your message`."
                )
            channel = ctx.author.voice.channel

        text = self._clean_text(text)
        if not text:
            return await ctx.send("There's nothing to say in that message.")
        max_chars = await self.config.max_chars()
        if len(text) > max_chars:
            return await ctx.send(
                f"That's {len(text)} characters; the limit is {max_chars}."
            )

        me = ctx.guild.me
        perms = channel.permissions_for(me)
        if not (perms.connect and perms.speak):
            return await ctx.send(f"I don't have permission to join and speak in {channel.mention}.")

        queue = self._queues.setdefault(ctx.guild.id, asyncio.Queue())
        if queue.qsize() >= MAX_QUEUE:
            return await ctx.send("The TTS queue is full. Wait for it to catch up.")
        queue.put_nowait(TTSRequest(ctx, channel, text))

        worker = self._workers.get(ctx.guild.id)
        if worker is None or worker.done():
            self._workers[ctx.guild.id] = asyncio.create_task(self._worker(ctx.guild))
        else:
            await ctx.tick()

    @commands.group()
    @commands.is_owner()
    async def ttsset(self, ctx: commands.Context):
        """Configure the Chatterbox TTS cog."""

    @ttsset.command(name="url")
    async def ttsset_url(self, ctx: commands.Context, url: str):
        """Set the Chatterbox server address, e.g. `http://192.168.1.227:8004`."""
        url = self._normalize_url(url)
        if not url.startswith(("http://", "https://")):
            return await ctx.send("The address must start with `http://` or `https://`.")
        await self.config.url.set(url)
        self._voice_mode = None
        ok, detail = await self._check_server(url)
        status = f"Connected, {detail}." if ok else f"Saved, but I couldn't reach it: {detail}"
        await ctx.send(f"Chatterbox URL set to `{url}`. {status}")

    @ttsset.command(name="maxlength")
    async def ttsset_maxlength(self, ctx: commands.Context, characters: int):
        """Set the longest message (in characters) that `[p]tts` will read out."""
        if not 1 <= characters <= 3000:
            return await ctx.send("Pick a number between 1 and 3000.")
        await self.config.max_chars.set(characters)
        await ctx.send(f"TTS messages are now limited to {characters} characters.")

    @ttsset.command(name="status")
    async def ttsset_status(self, ctx: commands.Context):
        """Show the current settings and check the Chatterbox server."""
        url = await self.config.url()
        ok, detail = await self._check_server(url)
        await ctx.send(
            f"**Server:** `{url}` - {'online' if ok else 'unreachable'} ({detail})\n"
            f"**Max length:** {await self.config.max_chars()} characters\n"
            f"**Voice:** `{VOICE_FILE}`"
        )

    # ------------------------------------------------------------------ worker

    async def _worker(self, guild: discord.Guild):
        queue = self._queues[guild.id]
        # A message can arrive while a batch is handing voice back to music,
        # so keep going until the queue is really empty.
        while not queue.empty():
            await self._run_batch(guild, queue)

    async def _run_batch(self, guild: discord.Guild, queue: asyncio.Queue):
        """Play queued messages for one server, then give voice back to music."""
        vc: Optional[discord.VoiceClient] = None
        music: Optional[MusicSnapshot] = None
        took_voice = False
        notify = None

        try:
            while True:
                try:
                    request = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                notify = request.ctx.channel
                try:
                    # Generate first, so music keeps playing while Chatterbox works.
                    async with request.ctx.typing():
                        audio = await self._fetch_tts(guild, request.text)
                    if not took_voice:
                        music = await self._release_music(guild)
                        took_voice = True
                    vc = await self._ensure_connected(guild, request.channel, vc)
                    await self._play(vc, audio)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("TTS failed in guild %s", guild.id, exc_info=exc)
                    await self._safe_send(request.ctx.channel, f"TTS error: {exc}")
                finally:
                    queue.task_done()
        finally:
            if vc is not None and vc.is_connected():
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    log.exception("Failed to leave voice in guild %s", guild.id)
            if music is not None:
                await asyncio.sleep(1)  # let Discord register the disconnect
                await self._restore_music(guild, music, notify)

    async def _ensure_connected(self, guild, channel, vc):
        if vc is not None and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
            return vc
        existing = guild.voice_client
        if existing is not None:
            await existing.disconnect(force=True)
            await asyncio.sleep(0.5)
        return await channel.connect(self_deaf=True, timeout=20)

    async def _play(self, vc: discord.VoiceClient, audio: bytes):
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def _finish(error):
            if not finished.done():
                finished.set_result(error)

        def after(error):  # runs on discord.py's audio thread
            loop.call_soon_threadsafe(_finish, error)

        vc.play(discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True), after=after)
        try:
            error = await asyncio.wait_for(finished, PLAYBACK_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            vc.stop()
            raise
        if error:
            raise RuntimeError(f"playback failed: {error}")

    # ------------------------------------------------------------------ music handoff

    def _music_player(self, guild):
        if lavalink is None:
            return None
        try:
            return lavalink.get_player(guild.id)
        except Exception:  # NodeNotFound / PlayerNotFound / Audio not loaded
            return None

    async def _release_music(self, guild) -> Optional[MusicSnapshot]:
        """Disconnect Red's Audio player (only one voice connection per server is
        possible) and remember its state so it can be restored afterwards."""
        player = self._music_player(guild)
        if player is None or player.channel is None:
            return None
        snapshot = MusicSnapshot(
            channel=player.channel,
            current=player.current,
            position=player.position,
            paused=player.paused,
            queue=list(player.queue),
            repeat=player.repeat,
            shuffle=player.shuffle,
            shuffle_bumped=player.shuffle_bumped,
            volume=player.volume,
            metadata=dict(getattr(player, "_metadata", {})),
        )
        try:
            if player.current is not None and not player.paused:
                await player.pause(True)
            await player.disconnect()
        except Exception:
            log.exception("Could not hand voice over from Audio in guild %s", guild.id)
        await asyncio.sleep(1)
        return snapshot

    async def _restore_music(self, guild, snap: MusicSnapshot, notify):
        if lavalink is None:
            return
        if self._music_player(guild) is not None:
            return  # someone started music again during TTS; leave it alone
        channel = guild.get_channel(snap.channel.id)
        if not isinstance(channel, discord.VoiceChannel):
            return
        try:
            player = await lavalink.connect(channel, self_deaf=await self._audio_deafen(guild))
            player._metadata.update(snap.metadata)
            player.queue = snap.queue
            player.repeat = snap.repeat
            player.shuffle = snap.shuffle
            player.shuffle_bumped = snap.shuffle_bumped
            await player.set_volume(snap.volume)
            if snap.current is not None:
                start = 0 if getattr(snap.current, "is_stream", False) else snap.position
                player.current = snap.current
                await player.resume(snap.current, replace=True, start=start, pause=snap.paused)
        except Exception:
            log.exception("Could not restore music in guild %s", guild.id)
            if snap.current is not None and notify is not None:
                await self._safe_send(
                    notify, "Couldn't bring the music back after TTS. Queue it up again with play."
                )

    async def _audio_deafen(self, guild) -> bool:
        audio = self.bot.get_cog("Audio")
        try:
            return await audio.config.guild(guild).auto_deafen()
        except Exception:
            return True

    # ------------------------------------------------------------------ Chatterbox

    async def _fetch_tts(self, guild, text: str) -> bytes:
        url = await self.config.url()
        payload = {
            "text": text,
            **await self._voice_payload(url),
            "output_format": "wav",
            "split_text": True,
        }
        try:
            async with self._session.post(
                f"{url}/tts", json=payload, timeout=aiohttp.ClientTimeout(total=TTS_TIMEOUT)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Chatterbox returned HTTP {resp.status}: {body[:200]}")
                return await resp.read()
        except asyncio.TimeoutError:
            raise RuntimeError(f"Chatterbox took longer than {TTS_TIMEOUT}s to answer.") from None
        except aiohttp.ClientConnectionError:
            raise RuntimeError(
                f"Couldn't reach Chatterbox at `{url}`. Is the server running?"
            ) from None

    async def _find_voice(self, url: str):
        """Where voice.wav lives on the server: (mode, exact filename), with mode
        "predefined" for the voices folder or "clone" for the web UI's
        voice-cloning uploads. (None, None) if it isn't there."""
        timeout = aiohttp.ClientTimeout(total=5)
        for endpoint, mode in (("get_predefined_voices", "predefined"), ("get_reference_files", "clone")):
            async with self._session.get(f"{url}/{endpoint}", timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
            names = [item.get("filename") if isinstance(item, dict) else item for item in data]
            for name in names:
                if str(name).lower() == VOICE_FILE:  # match Voice.wav too
                    return mode, name
        return None, None

    async def _voice_payload(self, url: str) -> dict:
        now = time.monotonic()
        if self._voice_mode is None or now - self._voice_mode_at > VOICE_CHECK_SECONDS:
            try:
                mode, name = await self._find_voice(url)
            except aiohttp.ClientConnectionError:
                raise RuntimeError(
                    f"Couldn't reach Chatterbox at `{url}`. Is the server running?"
                ) from None
            except Exception as exc:
                log.debug("Couldn't look up %s on the server: %s", VOICE_FILE, exc)
                mode, name = "predefined", VOICE_FILE  # older servers: try the voices folder
            if mode is None:
                raise RuntimeError(
                    f"The Chatterbox server doesn't have `{VOICE_FILE}`. "
                    "Put it in the server's `voices` folder."
                )
            self._voice_mode, self._voice_name, self._voice_mode_at = mode, name, now
        if self._voice_mode == "clone":
            return {"voice_mode": "clone", "reference_audio_filename": self._voice_name}
        return {"voice_mode": "predefined", "predefined_voice_id": self._voice_name}

    async def _check_server(self, url: str):
        try:
            mode, _ = await self._find_voice(url)
        except Exception as exc:
            return False, str(exc) or type(exc).__name__
        if mode is None:
            return True, f"but `{VOICE_FILE}` is missing: put it in the server's voices folder"
        where = "voices folder" if mode == "predefined" else "voice-cloning uploads"
        return True, f"`{VOICE_FILE}` found in its {where}"

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip().strip("<>").rstrip("/")
        return _strip_suffix(url, "/tts")

    @staticmethod
    def _clean_text(text: str) -> str:
        text = CUSTOM_EMOJI_RE.sub(r"\1", text)
        text = LINK_RE.sub("link", text)
        return " ".join(text.split())

    @staticmethod
    async def _safe_send(channel, content: str):
        try:
            await channel.send(content)
        except discord.HTTPException:
            pass
