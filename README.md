# stremio-watchlist

Drop a title, IMDb link or screenshot into a WhatsApp group → it appears as a
**🎬 WhatsApp Watchlist** row in Stremio (Movies + Series), on every device.

- Text / IMDb links → Cinemeta search (Stremio's free metadata API)
- Screenshots → tesseract OCR → local Ollama vision (`qwen2.5vl:7b`) fallback. No cloud LLM, ₹0 per item.
- `-title` removes an item. The bot confirms in the group with 🤖.
- The catalog is static JSON in `docs/`, served by GitHub Pages: no server or tunnel to keep alive.

Install in Stremio: `https://taaranreddy.github.io/stremio-watchlist/manifest.json`
