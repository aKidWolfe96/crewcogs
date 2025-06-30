import requests
from redbot.core import commands
import discord

class CrewBot(commands.Cog):
    """Chat and Prompt AI using your local Ollama model."""

    def __init__(self, bot):
        self.bot = bot
        self.api_chat_url = "http://172.17.0.2:11434/api/chat"
        self.api_generate_url = "http://172.17.0.2:11434/api/generate"
        self.model = "llama2-uncensored"  # Update to your preferred model

    @commands.command(name="crewbot")
    async def crewbot_chat(self, ctx, *, message: str):
        """Chat with CrewBot using Ollama's chat endpoint."""
        await ctx.typing()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": message}
            ],
            "stream": False
        }

        try:
            response = requests.post(self.api_chat_url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            reply = data.get("message", {}).get("content", "No content returned.")

        except Exception as e:
            reply = f"⚠️ CrewBot Chat Error: {e}"

        await ctx.send(reply[:2000])

    @commands.command(name="crewprompt")
    async def crewbot_generate(self, ctx, *, prompt: str):
        """Send a raw prompt to CrewBot using Ollama's generate endpoint."""
        await ctx.typing()

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False
        }

        try:
            response = requests.post(self.api_generate_url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            reply = data.get("response", "No content returned.")

        except Exception as e:
            reply = f"⚠️ CrewBot Generate Error: {e}"

        await ctx.send(reply[:2000])
