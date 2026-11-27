# CrewCogs 🔧
A collection of custom Red-DiscordBot cogs built for CrewBot. Includes tools, games, and AI features like local chatbot integration with Ollama.

---

## 🚀 Installation

### 1. Add Repo to Red
In Discord or your Redbot console, run:

```bash
[p]repo add crewcogs https://github.com/aKidWolfe96/crewcogs
```

### 2. Install Specific Cog
Example: to install `crewbot` (Ollama chatbot + Image generation via Comfyui)

```bash
[p]cog install crewcogs crewbot
```

You can replace `crewbot` with any other cog name in this repo.

### 3. Load the Cog

```bash
[p]load crewbot
```
---

## - Link to install Red-DiscordBot -
https://github.com/Cog-Creators/Red-DiscordBot

---

## 📖 Daily Verse

An inspirational Bible verse as an elegant image card every day, with seven card styles that rotate daily, weekday themes (Motivation Monday, Faith Friday…) and holiday verses. Look up any verse, compare translations side by side, read whole chapters, and pick verses by theme.

**Translations:** New King James Version, New International Version and New American Standard Bible (1995) through [API.Bible](https://api.bible/) (your plan must include them), plus the public-domain King James Version, which is built in and always works.

```bash
[p]cog install crewcogs dailyverse
[p]load dailyverse
[p]set api apibible api_key,YOUR_KEY      # run in a DM with the bot
[p]verseset apistatus                     # check which translations your key can load
[p]verseset channel #daily-verse
[p]verseset time 8:00am
[p]verseset timezone America/New_York
```

Try `[p]verse John 3:16`, `[p]verse compare John 3:16`, `[p]verse read Psalm 23`, `[p]verse random hope`, `[p]verse pick` and `[p]verseset preview`.

---

## 🔊 Chatterbox TTS

Reads messages out loud in voice chat using your own [Chatterbox TTS server](https://github.com/devnen/Chatterbox-TTS-Server). If Red's Audio cog is playing music, the music steps aside for the message and then comes back where it left off, with the same song, position, queue and volume.

**1. Run the TTS server in Docker** (on the machine with the GPU). The scripts are in [`chatterbox_tts/docker`](chatterbox_tts/docker).

**Windows** (needs [Docker Desktop](https://www.docker.com/products/docker-desktop/) running and [Git](https://git-scm.com/download/win)). In PowerShell or Command Prompt, from that folder:

```bash
.\chatterbox-docker.cmd up                # auto-picks NVIDIA or CPU; add -Gpu cu128 for RTX 50-series
```

**Linux / WSL / macOS:**

```bash
./chatterbox-docker.sh up                 # auto-picks NVIDIA or CPU; --gpu cu128 for RTX 50-series
```

The first start downloads the model, so give it a few minutes. When it's ready, the script prints the exact `[p]ttsset url` command to run. Other commands: `status`, `logs`, `restart`, `down` and `update`. Use `help` (Windows) or `--help` to see every option (port, install folder, GPU type).

**2. Install the cog** (the bot host needs `ffmpeg`):

```bash
[p]cog install crewcogs chatterbox_tts
[p]load chatterbox_tts
[p]ttsset url http://192.168.1.227:8000  # the address the script printed
[p]ttsset status
```

Try `[p]tts hello everyone`, `[p]tts #Some-Voice-Channel surprise!`, `[p]ttsvoice list` and `[p]ttsvoice Olivia`. Changing the voice needs Manage Server. To add voices, put `.wav` files in the server's `voices` folder. The bot owner can set the longest message with `[p]ttsset maxlength 400`.
