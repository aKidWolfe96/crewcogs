{
  "author": ["KrustyKrew"],
  "name": "Overwatch",
  "short": "Overwatch stats + a self-report spray-challenge board.",
  "description": "Links member BattleTags and pulls ranks/stats from the unofficial OverFast API, plus a manually-curated spray-challenge board members check off themselves. !ow tracker shows the whole server; !ow challenge seedcute preloads 51 cute-spray challenges.",
  "tags": ["overwatch", "games", "stats", "tracker", "sprays"],
  "requirements": ["aiohttp"],
  "min_bot_version": "3.5.0",
  "hidden": false,
  "disabled": false,
  "type": "COG",
  "end_user_data_statement": "This cog stores, per Discord user per server: the BattleTag you choose to link, the OverFast player id derived from it, and the list of spray-challenge IDs you mark complete. Use [p]ow unlink to remove your linked tag, or the bot's [p]mydata commands to delete all stored data."
}
