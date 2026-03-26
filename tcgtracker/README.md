# TCGTracker

A [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cog that monitors Pokémon TCG products across major retailers by UPC and alerts your Discord server when something restocks — online or in-store.

---

## Features

- 🔍 **Tracks by UPC** across Best Buy, Walmart, Target, GameStop, and Pokémon Center
- 💰 **Price vs. MSRP comparison** on every alert — shows if it's above or below retail
- 🔔 **One alert per restock cycle** — no spam; resets automatically when it goes back out of stock
- 📊 **Full status on demand** — `tcgcheck` shows every retailer's current stock state regardless of alert history
- 🏪 **In-store inventory by ZIP code** — add ZIP codes and `tcgcheck` will show nearby store stock
- 📢 **Separate channels for online and in-store** — route each type of result to its own channel
- ⚙️ **Per-guild configuration** — each server has its own products, keys, and settings

---

## Installation

```
[p]repo add Crewcogs https://github.com/aKidWolfe96/tcgtracker
[p]cog install Crewcogs TCGTracker
[p]load TCGTracker
```

---

## Setup

### 1. Best Buy API Key *(optional but recommended)*
Best Buy is the only retailer that requires an API key. It's free and takes about 2 minutes to get.

1. Sign up at [developer.bestbuy.com](https://developer.bestbuy.com)
2. Copy your key and run:
```
[p]tcgset bestbuy_key YOUR_KEY_HERE
```
The bot will delete your message immediately to keep the key out of chat history.

> Without a Best Buy key, Best Buy will be skipped. All other retailers work without any key.

### 2. Alert Channels & Role

Set your online restock channel — this is where background auto-alerts and online status from `tcgcheck` will post:
```
[p]tcgset channel #online-restocks
[p]tcgset role @TCG Alerts
```

Optionally set a separate channel for in-store results from `tcgcheck`. If not set, in-store results post to wherever you run the command:
```
[p]tcgset storechannel #in-store-stock
```

### 3. Add Products to Track
```
[p]tcgadd <upc> <msrp> <product name>
```
**Example:**
```
[p]tcgadd 820650855344 44.99 Scarlet & Violet Destined Rivals ETB
```

### 4. In-Store Checks *(optional)*
Add ZIP codes to enable nearby store inventory lookups when running `tcgcheck`:
```
[p]tcgset zip <your zip code>
```
Up to 10 ZIP codes per server. Results appear in `tcgcheck` output grouped by retailer and location, posted to your configured in-store channel.

---

## Commands

### Settings (`[p]tcgset`)

| Command | Description |
|---|---|
| `tcgset channel #channel` | Set the channel for online restock alerts |
| `tcgset storechannel #channel` | Set the channel for in-store availability results |
| `tcgset clearstorechannel` | Remove the in-store channel (falls back to command channel) |
| `tcgset role @role` | Set the ping role for online restock alerts |
| `tcgset bestbuy_key <key>` | Set Best Buy API key |
| `tcgset interval <seconds>` | Set check frequency (min: 60, max: 86400, default: 300) |
| `tcgset zip <zip>` | Add a ZIP code for in-store checks |
| `tcgset unzip <zip>` | Remove a ZIP code |
| `tcgset status` | Show current configuration |

### Product Management

| Command | Description |
|---|---|
| `[p]tcgadd <upc> <msrp> <name>` | Add a product to track |
| `[p]tcgremove <upc>` | Stop tracking a product |
| `[p]tcglist` | List all tracked products and their current status |
| `[p]tcgcheck` | Manually trigger an immediate check right now |
| `[p]tcgreset <upc>` | Reset alert cooldown so the next check will re-alert if in stock |

All commands require **Admin** or **Manage Server** permissions.

---

## How Alerts Work

### Background auto-alerts
The bot checks all tracked products on the configured interval (default every 5 minutes). When a product flips from out-of-stock to in-stock at a retailer, it sends an embed to your online alert channel and pings the configured role:

```
💛 Best Buy — IN STOCK!
Scarlet & Violet Destined Rivals ETB

💰 Price: $44.99 ✅ At/below MSRP ($44.99)
🔗 View Product

UPC: 820650855344  |  MSRP: $44.99
```

Once alerted, the bot **won't ping again** for that retailer until the product goes out of stock and comes back — preventing duplicate pings. Use `[p]tcgreset <upc>` to manually clear the cooldown if needed.

### Manual `tcgcheck`
Running `[p]tcgcheck` always shows the **full current status** for every retailer, regardless of whether an alert already fired. You get a snapshot embed per product:

```
🔍 Scarlet & Violet Destined Rivals ETB
UPC: 820650855344 · MSRP: $44.99

💛 Best Buy          🎯 Target
🟢 IN STOCK          🔴 Out of stock
$44.99 ✅            —
View listing

🎮 GameStop          💙 Walmart
🟢 IN STOCK          🔴 Out of stock
$49.99 ⚠️ (+$5.00)  —
```

Role pings still only fire for **genuinely new** restocks detected during the manual check — so running it won't spam your members for things that were already alerted.

---

## Channel Routing

You can configure two separate channels to keep things organized:

| Result type | Goes to |
|---|---|
| Background auto-alerts (new restocks) | Online alert channel |
| `tcgcheck` online status summary | Online alert channel |
| `tcgcheck` in-store ZIP results | In-store channel (falls back to command channel if not set) |

The "Checking…" and "✅ Check complete." status messages always reply in the channel where you ran `tcgcheck`, so you always get feedback no matter where you invoke it.

---

## In-Store Results

When ZIP codes are configured, running `[p]tcgcheck` sends a separate embed per product to your in-store channel showing nearby store inventory:

```
🏪 In-Store Availability Near ZIP XXXXX
Scarlet & Violet Destined Rivals ETB · UPC 820650855344

🎮 GameStop — IN STOCK (2 locations)
• GameStop #1234 — 123 Main St, Anytown, ST · 3.2 mi · Qty: 2
• GameStop #5678 — 456 Oak Ave, Nearbytown, ST · 8.1 mi

🔴 No stock found at Best Buy, Walmart, or Target within 25 miles.
```

In-store checks run **only on manual `tcgcheck`** — not in the background loop — to avoid hammering retailer APIs.

---

## Retailer Notes

| Retailer | Method | Key Required? | Notes |
|---|---|---|---|
| Best Buy | Official API | ✅ Yes (free) | Most reliable; same key for online + in-store |
| Walmart | HTML scrape | ❌ No | Uses `__NEXT_DATA__` JSON; may break if Walmart changes their page structure |
| Target | Internal Redsky API | ❌ No | Uses an unofficial but widely-known endpoint; may break if Target rotates the key |
| GameStop | HTML scrape | ❌ No | Minimal bot protection; generally stable |
| Pokémon Center | HTML scrape | ❌ No | Searched by **product name**, not UPC — use specific names to avoid false positives |

---

## Requirements

- [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) 3.5.0+
- Python 3.10+
- `aiohttp`
- `beautifulsoup4`

These are installed automatically when you install the cog via `[p]cog install`.

---

## Contributing

Pull requests welcome. If a retailer's scraper breaks (they do sometimes), opening an issue with the error from your bot logs is the fastest way to get it fixed.

---

## Disclaimer

This cog scrapes or uses unofficial APIs for several retailers. It is intended for personal use to track your own purchases. Be mindful of check intervals — the default 5-minute interval is conservative for a reason. Don't set it below 60 seconds.
