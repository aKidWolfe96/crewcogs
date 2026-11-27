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
DEFAULT_VOICE = "Ryan.wav"
DEFAULT_MAX_CHARS = 400

# Used only when the Chatterbox server can't be asked for its voice list.
FALLBACK_VOICES = [
    "Abigail.wav", "Adrian.wav", "Alexander.wav", "Alice.wav",
    "Austin.wav", "Axel.wav", "Connor.wav", "Cora.wav",
    "Elena.wav", "Eli.wav", "Emily.wav", "Everett.wav",
    "Gabriel.wav", "Gianna.wav", "Henry.wav", "Ian.wav",
    "Jade.wav", "Jeremiah.wav", "Jordan.wav", "Julian.wav",
    "Layla.wav", "Leonardo.wav", "Michael.wav", "Miles.wav",
    "Olivia.wav", "Ryan.wav", "Taylor.wav", "Thomas.wav",
]

MAX_QUEUE = 10            # pending messages per server
VOICE_CACHE_SECONDS = 300
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
        self.config.register_global(
            url=DEFAULT_URL, max_chars=DEFAULT_MAX_CHARS, migrated_v1=False
        )
        self.config.register_guild(voice_id=DEFAULT_VOICE)

        self._session: Optional[aiohttp.ClientSession] = None
        self._queues: Dict[int, asyncio.Queue] = {}
        self._workers: Dict[int, asyncio.Task] = {}
        self._voice_cache: Optional[List[str]] = None
        self._voice_cache_at = 0.0

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        await self._migrate_old_config()

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

    async def _migrate_old_config(self):
        """Earlier versions stored each server's voice under the cog name "NoneType"."""
        if await self.config.migrated_v1():
            return
        try:
            old = Config.get_conf(None, identifier=5544332211, cog_name="NoneType")
            old.register_guild(voice_id=None)
            for guild_id, data in (await old.all_guilds()).items():
                if data.get("voice_id"):
                    await self.config.guild_from_id(guild_id).voice_id.set(data["voice_id"])
        except Exception:
            log.exception("Could not migrate old TTS voice settings")
        await self.config.migrated_v1.set(True)

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

    @commands.command()
    @commands.guild_only()
    async def ttsvoice(self, ctx: commands.Context, *, voice: str = None):
        """Show the TTS voices, or set this server's voice (needs Manage Server)."""
        voices, live = await self._get_voices()
        current = await self.config.guild(ctx.guild).voice_id()

        if voice is None or voice.lower() == "list":
            names = ", ".join(self._display(v) for v in voices)
            note = "" if live else "\n*(Couldn't reach the Chatterbox server; showing the default list.)*"
            return await ctx.send(
                f"**Current voice:** {self._display(current)}\n"
                f"**Available voices:**\n{names}{note}\n\n"
                f"Use `{ctx.clean_prefix}ttsvoice <name>` to set one."
            )

        if not await self._can_manage(ctx):
            return await ctx.send("You need the Manage Server permission to change the TTS voice.")

        wanted = _strip_suffix(voice.lower(), ".wav")
        match = next((v for v in voices if self._display(v).lower() == wanted), None)
        if not match:
            return await ctx.send(
                f"Voice `{voice}` not found. Use `{ctx.clean_prefix}ttsvoice list` to see all options."
            )

        await self.config.guild(ctx.guild).voice_id.set(match)
        await ctx.send(f"TTS voice set to **{self._display(match)}**.")

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
        self._voice_cache = None
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
            f"**This server's voice:** {self._display(await self.config.guild(ctx.guild).voice_id()) if ctx.guild else '-'}"
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
            "voice_mode": "predefined",
            "predefined_voice_id": await self.config.guild(guild).voice_id(),
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

    async def _get_voices(self):
        """Return (voices, came_from_server)."""
        now = time.monotonic()
        if self._voice_cache and now - self._voice_cache_at < VOICE_CACHE_SECONDS:
            return self._voice_cache, True
        url = await self.config.url()
        try:
            async with self._session.get(
                f"{url}/get_predefined_voices", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            voices = sorted({v["filename"] for v in data if v.get("filename")}, key=str.lower)
            if voices:
                self._voice_cache, self._voice_cache_at = voices, now
                return voices, True
        except Exception as exc:
            log.debug("Couldn't fetch Chatterbox voices: %s", exc)
        return FALLBACK_VOICES, False

    async def _check_server(self, url: str):
        try:
            async with self._session.get(
                f"{url}/get_predefined_voices", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                data = await resp.json()
                return True, f"{len(data)} voices available"
        except Exception as exc:
            return False, str(exc) or type(exc).__name__

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
    def _display(voice: str) -> str:
        return _strip_suffix(voice, ".wav")

    async def _can_manage(self, ctx) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True
        if ctx.author.guild_permissions.manage_guild:
            return True
        return await self.bot.is_admin(ctx.author)

    @staticmethod
    async def _safe_send(channel, content: str):
        try:
            await channel.send(content)
        except discord.HTTPException:
            pass
