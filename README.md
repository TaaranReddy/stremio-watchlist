# stremio-watchlist

Drop anything into the WhatsApp **🎬 Watchlist** group → it appears in the **🎬 WhatsApp Watchlist**
row in Stremio (Movies + Series), on every device.

| You send | What happens |
|---|---|
| A title, or a list ("interstellar, the prestige and severance") | Every title is added; typos corrected |
| "Full series" / "all the movies" | Adds the whole franchise of the title just mentioned |
| IMDb link | Added directly |
| Instagram / TikTok / YouTube reel or carousel (+ optional text) | Caption + Apple Vision OCR of 16 frames + Whisper transcript + key frames are read together; **every** recommended title is added ("Top 5 rom-coms" → all 5). Titles only mentioned as context (studio's other works, comparisons) are skipped |
| Screenshot / poster / forwarded clip | OCR first, then vision; low-confidence guesses are rejected |
| `-rocky` or "remove rocky" | Removed |
| Chit-chat / feedback | Ignored, no bot reply |

The bot replies with 🤖. Logic lives in `ident.py`: evidence → one DeepSeek-Flash read (local Qwen3-VL
fallback) → strict IMDb-suggestion resolution with year/type checks, so a phrase like "for the boys"
can't become a 1991 film.

`tools/vocr` is built from `tools/vocr.swift` (`swiftc -O tools/vocr.swift -o tools/vocr`).

The catalog is static JSON in `docs/`, served by GitHub Pages: no server or tunnel to keep alive.
Install in Stremio: `https://taaranreddy.github.io/stremio-watchlist/manifest.json`
