"""The Telegram bot: lifecycle, commands, adapters and copy.

Four modules, split by what each one is allowed to know about:

- `runtime` — the engine, the HTTP client and the fetcher, opened once at startup
- `commands` — what each command does, as functions returning text; imports no telegram
- `handlers` — the PTB adapters, which are the only code here that touches an `Update`
- `copy` — every Spanish string the bot can say

Deliberately no re-exports. `runtime` imports `copy`, `commands` imports `runtime`, and
`handlers` imports all three, so anything imported here would be a package that imports
itself while being imported. The wiring lives in `price_tracker.main`.
"""
