import discord
import aiohttp
import asyncio
import datetime
import logging

from redbot.core import commands
from discord.ext.tasks import loop

log = logging.getLogger("red.twitch")

class TwitchWebhook(commands.Cog):
    """Notify via webhook when Twitch streamers go live."""

    def __init__(self, bot):
        self.bot = bot
        self.client_id = "50mc80jxcaob6a8l79vag8moceqoxq"
        self.client_secret = "xb32ylq8yrzk1l67vg5lqmp5rq1d43"
        self.webhook_url = "https://discord.com/api/webhooks/1391915121344774144/1tgy7G4hO0u7uHYjwHc1o5d3ALzKlHdQByblLUcIM_t74MbH_7lrzColbROIuxabhjUt"
        self.streamers = ["onlyabi", "akidwolfe"]  # lowercase login names
        self.checked = {}
        self.access_token = None
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
                if not self.access_token:
                    log.error(f"Twitch token error: {data}")

    @loop(seconds=60)
    async def poll_streams(self):
        if not self.access_token:
            await self.get_access_token()
            if not self.access_token:
                return

        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self.access_token}"
        }

        async with aiohttp.ClientSession() as session:
            for streamer in self.streamers:
                url = f"https://api.twitch.tv/helix/streams?user_login={streamer}"
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json()
                    stream_data = data.get("data", [])
                    now_live = len(stream_data) > 0

                    if now_live:
                        if not self.checked.get(streamer, False):
                            await self.send_webhook(stream_data[0])
                            self.checked[streamer] = True
                    else:
                        self.checked[streamer] = False

    async def send_webhook(self, stream):
        thumbnail = stream["thumbnail_url"].format(width=640, height=360)
        payload = {
            "username": "Twitch Alert",
            "embeds": [
                {
                    "title": "Click here to watch the stream!",
                    "url": f"https://twitch.tv/{stream['user_login']}",
                    "description": stream['title'],
                    "color": 0x9146FF,
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "thumbnail": {"url": thumbnail},
                    "fields": [
                        {"name": "Streamer", "value": f"[{stream['user_name']}](https://twitch.tv/{stream['user_login']})", "inline": True},
                        {"name": "Game", "value": stream.get('game_name', 'Unknown'), "inline": True},
                        {"name": "Viewers", "value": str(stream.get('viewer_count', '0')), "inline": True}
                    ]
                }
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    log.error(f"Webhook failed: {resp.status} | {await resp.text()}")
