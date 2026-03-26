# TCGTracker

A [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cog that monitors Pokémon TCG products at Best Buy by UPC using their official API, and alerts your Discord server when something restocks.

\---

## Features

* 💛 **Best Buy tracking** via their official free Products API — reliable, no scraping
* 💰 **Price vs. MSRP comparison** on every alert — shows if it's above or below retail
* 🔔 **One alert per restock cycle** — no spam; resets automatically when it goes back out of stock
* 📊 **Full status on demand** — `tcgcheck` shows every product's current stock state regardless of alert history
* 🏪 **In-store inventory by ZIP code** — add ZIP codes and `tcgcheck` will show nearby Best Buy store stock
* 📢 **Separate channels for online and in-store** — route each type of result to its own channel
* ⚙️ **Per-guild configuration** — each server has its own products, keys, and settings

\---

## Installation

```
\[p]repo add Crewcogs https://github.com/aKidWolfe96/crewcogs
\[p]cog install Crewcogs tcgtracker
\[p]load tcgtracker
```

\---

## Setup

### 1\. Best Buy API Key *(required)*

The API is free and takes about 2 minutes to get.

1. Sign up at [developer.bestbuy.com](https://developer.bestbuy.com)
2. Copy your key and run:

```
\[p]tcgset bestbuy\_key YOUR\_KEY\_HERE
```

The bot deletes your message immediately to keep the key out of chat history.

### 2\. Alert Channels \& Role

Set your online restock channel — background auto-alerts and `tcgcheck` online summaries post here:

```
\[p]tcgset channel #online-restocks
\[p]tcgset role @TCG Alerts
```

Optionally set a separate channel for in-store results. If not set, in-store results post wherever you run the command:

```
\[p]tcgset storechannel #in-store-stock
```

### 3\. Add Products to Track

```
\[p]tcgadd <upc> <msrp> <product name>
```

**Example:**

```
\[p]tcgadd 074427166076 44.99 Elite Gengar 9-Pocket PRO-Binder
```

> \*\*Note:\*\* Products must exist in Best Buy's API catalog to be tracked. If a product returns no results, it may not be indexed in their API yet even if it appears on their website. This is a Best Buy catalog limitation.

### 4\. In-Store Checks *(optional)*

Add ZIP codes to enable nearby store inventory lookups when running `tcgcheck`:

```
\[p]tcgset zip <your zip code>
```

Up to 10 ZIP codes per server.

\---

## Commands

### Settings (`\[p]tcgset`)

|Command|Description|
|-|-|
|`tcgset bestbuy\_key <key>`|Set Best Buy API key|
|`tcgset channel #channel`|Set the channel for online restock alerts|
|`tcgset storechannel #channel`|Set the channel for in-store availability results|
|`tcgset clearstorechannel`|Remove the in-store channel (falls back to command channel)|
|`tcgset role @role`|Set the ping role for restock alerts|
|`tcgset interval <seconds>`|Set check frequency (min: 60, max: 86400, default: 300)|
|`tcgset zip <zip>`|Add a ZIP code for in-store checks|
|`tcgset unzip <zip>`|Remove a ZIP code|
|`tcgset status`|Show current configuration|

### Product Management

|Command|Description|
|-|-|
|`\[p]tcgadd <upc> <msrp> <n>`|Add a product to track|
|`\[p]tcgremove <upc>`|Stop tracking a product|
|`\[p]tcglist`|List all tracked products and their current status|
|`\[p]tcgcheck`|Manually trigger an immediate check right now|
|`\[p]tcgreset <upc>`|Reset alert cooldown so the next check will re-alert if in stock|

All commands require **Admin** or **Manage Server** permissions.

\---

## How Alerts Work

### Background auto-alerts

The bot checks all tracked products on the configured interval (default every 5 minutes). When a product goes from out-of-stock to in-stock, it sends an embed to your alert channel and pings the configured role:

```
💛 Best Buy — IN STOCK!
Elite Gengar 9-Pocket PRO-Binder

💰 Price: $44.99 ✅ At/below MSRP ($44.99)
🔗 View Product

UPC: 074427166076  |  MSRP: $44.99
```

Once alerted, the bot won't ping again for that product until it goes back out of stock and restocks again. Use `\[p]tcgreset <upc>` to manually clear the cooldown.

### Manual `tcgcheck`

Running `\[p]tcgcheck` always shows the full current status for every product, regardless of whether an alert already fired:

```
🔍 Elite Gengar 9-Pocket PRO-Binder
UPC: 074427166076 · MSRP: $44.99

💛 Best Buy
🟢 IN STOCK · $44.99 ✅
View listing
```

Role pings only fire for genuinely new restocks found during the manual check.

\---

## Channel Routing

|Result type|Goes to|
|-|-|
|Background auto-alerts|Online alert channel|
|`tcgcheck` online status summary|Online alert channel|
|`tcgcheck` in-store ZIP results|In-store channel (falls back to command channel if not set)|

The "Checking…" and "✅ Check complete." messages always reply where you ran `tcgcheck`.

\---

## In-Store Results

When ZIP codes are configured, running `\[p]tcgcheck` sends a separate embed per product to your in-store channel:

```
🏪 Best Buy In-Store — ZIP XXXXX
Elite Gengar 9-Pocket PRO-Binder · UPC 074427166076

🟢 In Stock (2 locations)
• Best Buy — 123 Main St, Anytown, ST · 3.2 mi
• Best Buy — 456 Oak Ave, Nearbytown, ST · 8.1 mi
```

In-store checks run **only on manual `tcgcheck`** — not in the background loop.

\---

## Requirements

* [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) 3.5.0+
* Python 3.10+
* `aiohttp`

Installed automatically via `\[p]cog install`.

\---

## Contributing

Pull requests welcome. If Best Buy changes their API in a way that breaks tracking, open an issue with the error from your bot logs.

\---

## Disclaimer

This cog uses Best Buy's official public API. Be mindful of their rate limits — the default 5-minute check interval is intentionally conservative. Don't set it below 60 seconds.

