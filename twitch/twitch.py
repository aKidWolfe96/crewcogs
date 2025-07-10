import discord
import aiohttp
import asyncio
import datetime
import logging

from redbot.core import commands
from discord.ext.tasks import loop

log = logging.getLogger("red.twitch")


class TwitchWebhook(commands.Cog):
    """Notify via webhook when Twitch streamers go live and update their roles."""

    def __init__(self, bot):
        self.bot = bot
        self.client_id = "CHANGEME"
        self.client_secret = "CHANGEME"
        self.webhook_url = "CHANGEME"
        self.streamers = ["TWITCHUSER1", "TWITCHUSER2"]

        # Map Twitch login → Discord user ID
        self.discord_map = {
            "TWITCHUSER1": DISCORDID,
            "TWITCHUSER2": DISCORDID
        }

        self.streamer_role_id = 1390723266909569054  # Role to assign/remove
        self.guild_id = 1170173161795506296          # Guild where it happens

        self.checked = {}
        self.access_token = None
        self.token_expiry = None
        self.poll_streams.start()

    def cog_unload(self):
        self.poll_streams.cancel()

    async def get_access_token(self):
        async with aiohttp.ClientSession() as session:
            url = "https://id.twitch.tv/oauth2/token"
            params = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials"
            }
            async with session.post(url, params=params) as resp:
                data = await resp.json()
                self.access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self.token_expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in - 60)
                if not self.access_token:
                    log.error(f"Twitch token error: {data}")

    @loop(seconds=60)
    async def poll_streams(self):
        if not self.access_token or datetime.datetime.utcnow() >= self.token_expiry:
            await self.get_access_token()
            if not self.access_token:
                return

        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self.access_token}"
        }

        async with aiohttp.ClientSession() as session:
            for streamer in self.streamers:
                try:
                    user_info, stream = await self.get_streamer_info(session, streamer, headers)
                    if stream and not self.checked.get(streamer, False):
                        log.info(f"{streamer} just went live.")
                        await self.send_webhook(user_info, stream)
                        await self.update_streamer_role(streamer, is_live=True)
                        self.checked[streamer] = True
                    elif not stream and self.checked.get(streamer, False):
                        log.info(f"{streamer} went offline.")
                        await self.update_streamer_role(streamer, is_live=False)
                        self.checked[streamer] = False
                except Exception as e:
                    log.error(f"Error checking {streamer}: {e}")
                    continue

    async def get_streamer_info(self, session, streamer, headers):
        user_url = f"https://api.twitch.tv/helix/users?login={streamer}"
        async with session.get(user_url, headers=headers) as user_resp:
            user_data = await user_resp.json()
            user_info = user_data.get("data", [])[0]

        stream_url = f"https://api.twitch.tv/helix/streams?user_login={streamer}"
        async with session.get(stream_url, headers=headers) as stream_resp:
            stream_data = await stream_resp.json()
            stream_info = stream_data.get("data", [])
            is_live = len(stream_info) > 0

        return user_info, stream_info[0] if is_live else None

    async def send_webhook(self, user, stream):
        display_name = user["display_name"]
        profile_img = user["profile_image_url"]
        banner_img = user.get("offline_image_url", "")
        bio = user.get("description", "No bio provided.")

        thumbnail = stream["thumbnail_url"].format(width=1280, height=720)
        timestamp = datetime.datetime.utcnow().isoformat()

        payload = {
            "username": display_name,
            "avatar_url": profile_img,
            "embeds": [
                {
                    "title": f"🔴 {display_name} is LIVE!",
                    "url": f"https://twitch.tv/{user['login']}",
                    "description": f"**{stream['title']}**\n\n{bio}",
                    "color": 0x9146FF,
                    "timestamp": timestamp,
                    "thumbnail": {
                        "url": profile_img
                    },
                    "image": {
                        "url": thumbnail + f"?rand={datetime.datetime.utcnow().timestamp()}"
                    },
                    "author": {
                        "name": f"{display_name}",
                        "url": f"https://twitch.tv/{user['login']}",
                        "icon_url": profile_img
                    },
                    "fields": [
                        {
                            "name": "🎮 Game",
                            "value": stream.get("game_name", "Unknown"),
                            "inline": True
                        },
                        {
                            "name": "👥 Viewers",
                            "value": str(stream.get("viewer_count", "0")),
                            "inline": True
                        },
                        {
                            "name": "🔗 Link",
                            "value": f"[Watch Stream](https://twitch.tv/{user['login']})",
                            "inline": False
                        }
                    ],
                    "footer": {
                        "text": f"Streaming since {stream['started_at']}",
                        "icon_url": "https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"
                    }
                }
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    log.error(f"Webhook failed: {resp.status} | {await resp.text()}")

    async def update_streamer_role(self, streamer: str, is_live: bool):
        discord_id = self.discord_map.get(streamer)
        if not discord_id:
            return

        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return

        member = guild.get_member(discord_id)
        if not member:
            return

        role = guild.get_role(self.streamer_role_id)
        if not role:
            return

        try:
            if is_live and role not in member.roles:
                await member.add_roles(role, reason="Twitch stream started")
                log.info(f"Added live role to {member.display_name}")
            elif not is_live and role in member.roles:
                await member.remove_roles(role, reason="Twitch stream ended")
                log.info(f"Removed live role from {member.display_name}")
        except Exception as e:
            log.error(f"Role update failed for {member.display_name}: {e}")
