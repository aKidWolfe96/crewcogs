import discord
from redbot.core import commands, Config
import ollama
import asyncio

class CrewBot(commands.Cog):
    """Chat and prompt AI using a local Ollama model (e.g., gemma3n:e4b) with conversation memory."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9876543210, force_registration=True)
        default_global = {
            "ollama_host": "192.168.100.254",
            "ollama_port": 11434,
            "ollama_model": "gemma3n:e4b"
        }
        default_user = {
            "chat_history": []
        }
        self.config.register_global(**default_global)
        self.config.register_user(**default_user)

    async def _get_ollama_client(self):
        """Create an Ollama client with configured settings."""
        host = await self.config.ollama_host()
        port = await self.config.ollama_port()
        base_url = f"http://{host}:{port}"
        return ollama.Client(host=base_url)

    @commands.group(name="setcrewbot")
    async def set_crewbot(self, ctx):
        """Commands to configure CrewBot's Ollama settings."""
        pass

    @set_crewbot.command(name="host")
    async def set_ollama_host(self, ctx, host: str):
        """Set the Ollama API host (default: 192.168.100.254)."""
        await self.config.ollama_host.set(host)
        await ctx.send(f"Ollama host set to: {host}")

    @set_crewbot.command(name="port")
    async def set_ollama_port(self, ctx, port: int):
        """Set the Ollama API port (default: 11434)."""
        await self.config.ollama_port.set(port)
        await ctx.send(f"Ollama port set to: {port}")

    @set_crewbot.command(name="model")
    async def set_ollama_model(self, ctx, model: str):
        """Set the Ollama model (default: gemma3n:e4b)."""
        await self.config.ollama_model.set(model)
        await ctx.send(f"Ollama model set to: {model}")

    @commands.command(name="crewbot")
    async def crewbot_chat(self, ctx, *, message: str):
        """Chat with CrewBot using Ollama's chat endpoint with conversation memory.

        Example: [p]crewbot Tell me a joke
        """
        async with ctx.typing():
            try:
                client = await self._get_ollama_client()
                model = await self.config.ollama_model()
                system_prompt = "You are CrewBot, a helpful AI in a Discord server. Provide concise, accurate, and friendly responses suitable for chat. Avoid sensitive or harmful content."

                # Get user's chat history
                chat_history = await self.config.user(ctx.author).chat_history()
                if not chat_history:
                    chat_history = []

                # Append system prompt and current message to history
                messages = [{"role": "system", "content": system_prompt}] + chat_history + [{"role": "user", "content": message}]

                # Send to Ollama
                response = await asyncio.to_thread(
                    client.chat,
                    model=model,
                    messages=messages
                )
                reply = response["message"]["content"].strip()

                # Update chat history (store up to 10 messages)
                chat_history.append({"role": "user", "content": message})
                chat_history.append({"role": "assistant", "content": reply})
                if len(chat_history) > 20:  # 10 user messages + 10 bot responses
                    chat_history = chat_history[-20:]
                await self.config.user(ctx.author).chat_history.set(chat_history)

                # Send response (truncate for Discord's 2000-char limit)
                if len(reply) > 2000:
                    reply = reply[:1997] + "..."
                await ctx.send(reply)
            except ollama.ResponseError as e:
                await ctx.send(f"⚠️ CrewBot Chat Error: {str(e)}")
            except Exception as e:
                await ctx.send(f"⚠️ Unexpected Error: {str(e)}")

    @commands.command(name="crewprompt")
    async def crewbot_generate(self, ctx, *, prompt: str):
        """Send a raw prompt to CrewBot using Ollama's generate endpoint (no memory).

        Example: [p]crewprompt Summarize quantum mechanics
        """
        async with ctx.typing():
            try:
                client = await self._get_ollama_client()
                model = await self.config.ollama_model()
                response = await asyncio.to_thread(
                    client.generate,
                    model=model,
                    prompt=prompt
                )
                reply = response["response"].strip()
                if len(reply) > 2000:
                    reply = reply[:1997] + "..."
                await ctx.send(reply)
            except ollama.ResponseError as e:
                await ctx.send(f"⚠️ CrewBot Generate Error: {str(e)}")
            except Exception as e:
                await ctx.send(f"⚠️ Unexpected Error: {str(e)}")

    @commands.command(name="crewbotclear")
    async def crewbot_clear(self, ctx):
        """Clear your CrewBot chat history to start a fresh conversation.

        Example: [p]crewbotclear
        """
        async with ctx.typing():
            await self.config.user(ctx.author).chat_history.set([])
            await ctx.send("✅ Your CrewBot chat history has been cleared.")

def setup(bot):
    bot.add_cog(CrewBot(bot))
