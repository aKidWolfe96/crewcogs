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
