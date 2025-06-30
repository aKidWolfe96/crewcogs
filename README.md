# CrewCogs 🔧
A collection of custom Red-DiscordBot cogs built for CrewBot. Includes tools, games, and AI-powered features like local chatbot integration with Ollama.

---

## 🚀 Installation

### 1. Add Repo to Red
In Discord or your Redbot console, run:

```bash
[p]repo add crewcogs https://github.com/aKidWolfe96/crewcogs
```

### 2. Install Specific Cog
Example: to install `crewbot` (Ollama chatbot)

```bash
[p]cog install crewcogs crewbot
```

You can replace `crewbot` with any other cog name in this repo.

### 3. Load the Cog
```bash
[p]load crewbot
```

---

## 🧠 Cog: CrewBot (Chat with Ollama)

Talk to a locally hosted LLM like `llama2-uncensored` using:

```bash
[prefix]crewbot <your message>
```

**Requirements:**
- Ollama running locally (`ollama run llama2-uncensored`)
- `requests` library installed in your RedBot venv:
  ```bash
  pip install requests
  ```

---

## 📁 Folder Structure

```
CREW/
└── cogs/
    └── CogManager/
        └── cogs/
            ├── crewbot/
            ├── ...
```

---

## 🛠 Updating

When the repo is updated, run:

```bash
[p]cog update crewcogs
```

Then reload the cog:

```bash
[p]reload crewbot
```

---

## ✍️ Created by
aKidWolfe
