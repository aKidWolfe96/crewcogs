# UFC Cog for Red-DiscordBot

A full-featured UFC cog for Red v3. Displays upcoming fight cards, recent results, fighter stats, and runs a server-wide pick 'em game with a leaderboard.

---

## Installation

1. **Copy the `ufc/` folder** into your Red cog directory:
   ```
   [your cogs path]/ufc/
   ```

2. **Install dependencies** (if not already present):
   ```
   pip install aiohttp beautifulsoup4
   ```
   Or from inside Red:
   ```
   [p]pipinstall aiohttp beautifulsoup4
   ```

3. **Load the cog:**
   ```
   [p]load ufc
   ```

---

## Commands

| Command | Description |
|---|---|
| `[p]ufc` | Show all commands |
| `[p]ufc card` | Upcoming fight card with matchups |
| `[p]ufc results` | Most recent event results |
| `[p]ufc fighter <name>` | Fighter stats and recent fight history |
| `[p]ufc pick <fighter>` | Lock in your pick for an upcoming fight |
| `[p]ufc picks` | View server picks for the next event |
| `[p]ufc standings` | Pick 'em leaderboard |

### Admin Commands
| Command | Permission | Description |
|---|---|---|
| `[p]ufc settle` | Manage Guild | Score picks against results, update standings, clear picks |
| `[p]ufc clearpicks` | Manage Guild | Manually clear picks without settling |
| `[p]ufc resetstandings` | Administrator | Wipe standings entirely |

---

## How Picks Work

1. **Before an event**, users run `[p]ufc pick <fighter name>` to lock in picks for individual fights. The bot matches the name to a fight on the upcoming card automatically.

2. **Users can change picks** at any time before the event by picking again — the new pick overwrites the old one.

3. **After an event**, an admin runs `[p]ufc settle`. The bot:
   - Fetches the most recent event results from ESPN
   - Compares picks against winners
   - Updates the server standings
   - Clears picks for the next event

4. **`[p]ufc picks`** shows a visual breakdown with a colored progress bar showing how the server is split for each fight.

---

## Data Sources

- **Fight cards & results:** ESPN's MMA API (free, no key required)
- **Fighter stats:** ESPN athlete endpoint + Sherdog scraper fallback
- **Picks & standings:** Stored locally via Red's Config system (no external database)

---

## File Structure

```
ufc/
├── __init__.py     # Cog entry point
├── ufc.py          # All commands and cog logic
├── api.py          # ESPN + Sherdog data fetching
├── embeds.py       # Discord embed builders
└── info.json       # Red cog metadata
```

---

## Notes

- ESPN's API is undocumented/unofficial — it's widely used by sports hobbyists but could change without warning. If the card/results commands break, the API URL may need updating.
- The Sherdog scraper is a fallback for fighter bios/history when ESPN doesn't have detailed stats.
- All picks and standings are **per-server** and stored in Red's config — nothing leaves your bot.
