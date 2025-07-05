import discord
from redbot.core import commands
import aiohttp
import asyncio
import json
import uuid
from pathlib import Path

class Imagine(commands.Cog):
    """Generate images using ComfyUI and send output when ready."""

    def __init__(self, bot):
        self.bot = bot
        self.server_address = "127.0.0.1:8000"
        self.output_folder = Path(r"C:\Users\SERVER\Documents\ComfyUI\output")
        self.workflow_path = Path(__file__).parent / "flux_schnell-api.json"

    async def generate_image(self, prompt: str):
        client_id = str(uuid.uuid4())

        # Load and inject prompt
        workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        if "prompt" not in workflow:
            raise Exception("Workflow must contain a top-level 'prompt' key.")

        for node in workflow["prompt"].values():
            if isinstance(node, dict) and "inputs" in node:
                for k, v in node["inputs"].items():
                    if isinstance(v, str) and "{prompt}" in v:
                        node["inputs"][k] = v.replace("{prompt}", prompt)

        async with aiohttp.ClientSession() as session:
            # Submit the prompt
            async with session.post(f"http://{self.server_address}/prompt", json=workflow) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Prompt submission failed: {resp.status} | {text}")
                data = await resp.json()
                prompt_id = data.get("prompt_id")
                print(f"[IMAGINE] Submitted prompt_id: {prompt_id}")

            # WebSocket: wait up to 60s for generation to finish
            ws_url = f"ws://{self.server_address}/ws?clientId={client_id}"
            try:
                async with session.ws_connect(ws_url) as ws:
                    print("[IMAGINE] Waiting for WebSocket done event...")
                    for _ in range(60):
                        msg = await ws.receive(timeout=1)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if (
                                data.get("type") == "executing"
                                and data["data"].get("prompt_id") == prompt_id
                                and data["data"].get("node") is None
                            ):
                                print("[IMAGINE] ✅ WebSocket: Generation complete.")
                                break
            except Exception as e:
                print(f"[IMAGINE] ⚠️ WebSocket timeout or error: {e}")

            # Fallback: Poll /history up to 3 minutes
            for i in range(36):
                await asyncio.sleep(5)
                print(f"[IMAGINE] Polling history ({i+1}/36)...")
                async with session.get(f"http://{self.server_address}/history/{prompt_id}") as hist_resp:
                    if hist_resp.status != 200:
                        continue
                    history = await hist_resp.json()

                outputs = history.get(prompt_id, {}).get("outputs", {})
                for node_output in outputs.values():
                    for image in node_output.get("images", []):
                        filename = image.get("filename")
                        image_path = self.output_folder / filename
                        print(f"[IMAGINE] Checking image path: {image_path}")
                        if image_path.exists():
                            print(f"[IMAGINE] ✅ Found image: {image_path}")
                            return image_path

            raise Exception("Image not found after timeout.")

    @commands.command()
    async def imagine(self, ctx, *, prompt: str):
        """Generate an image using ComfyUI and your prompt."""
        loading = await ctx.send(f"🧠 Generating image for: `{prompt}`")
        try:
            image_path = await self.generate_image(prompt)
            try:
                file = discord.File(str(image_path))
                await loading.edit(content=f"✅ Image generated for: `{prompt}`")
                await ctx.send(file=file)
            except Exception as send_error:
                print(f"[IMAGINE] ❌ Discord send error: {send_error}")
                await loading.edit(content="⚠️ Image generated but failed to send.")
        except Exception as e:
            print(f"[IMAGINE] ❌ Error: {e}")
            await loading.edit(content=f"❌ Error: {e}")
