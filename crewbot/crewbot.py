import requests
from redbot.core import commands
import discord

class CrewBot(commands.Cog):
    """Chat with CrewBot powered by your local Ollama model."""

    def __init__(self, bot):
        self.bot = bot
        self.api_url = "http://localhost:11434/api/chat"
        self.model = "self.model = "llama2-uncensored"  # Customize this model name

    @commands.command(name="crewbot")
    async def crewbot_chat(self, ctx, *, message: str):
        """Talk to CrewBot (Ollama-based AI)"""
        await ctx.typing()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": message}
            ]
        }

        try:
            response = requests.post(self.api_url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            if "message" in data and "content" in data["message"]:
                reply = data["message"]["content"]
            else:
                reply = "CrewBot had no response."

        except Exception as e:
            reply = f"⚠️ CrewBot Error: {e}"

        await ctx.send(reply[:2000])
