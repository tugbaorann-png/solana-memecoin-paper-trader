SOLANA FIRST-LIVE TEST PACKAGE
==============================

Purpose
-------
This is intentionally a ONE round-trip live test, not an unlimited live bot.
It reuses the existing solana_paper_trader.scanner filters, adds Jupiter Tokens
safety checks and executable Jupiter Swap V2 quotes, and signs transactions with
a Privy-managed Solana wallet. It never needs a Phantom seed phrase/private key.

Files to upload into the existing /solana-paper-trader root on the live-bot branch:
- live_trader.py
- bootstrap_privy_wallet.py
- FIRST_LIVE_README.txt

Default safety limits
---------------------
- 0.005 SOL per buy
- maximum 1 open position
- +20% executable-quote take profit
- -10% executable-quote stop loss
- open-position checks every 10 seconds
- 0.015 SOL kept as a minimum reserve before entry
- exactly 1 completed buy/sell round trip, then the bot locks new entries
- minimum Jupiter Organic Score: 35
- minimum holders: 300
- mint and freeze authority must be confirmed disabled
- standard SPL Token program only
- suspicious/banned tokens rejected
- top-holder concentration above 60% rejected when data is available
- developer balance above 20% rejected when data is available
- buy or sell price impact above 2.5% rejected
- quoted round-trip value below 94% of input rejected

Required Railway secrets (never put these in GitHub)
----------------------------------------------------
PRIVY_APP_ID
PRIVY_APP_SECRET
JUPITER_API_KEY
PRIVY_WALLET_ID       (set after bootstrap creates/reuses the managed wallet)

Arming switch
-------------
LIVE_TRADING_ENABLED=YES_I_UNDERSTAND

Without that exact value, the program can scan and validate candidates but cannot
sign or send a buy/sell. We will first deploy with live trading NOT armed.

Recommended persistent state path after a Railway Volume is mounted:
LIVE_STATE_PATH=/data/solana_live_bot_state.json

Live start command (only after bootstrap + funding + dry validation):
PYTHONPATH=. python live_trader.py

Bootstrap command (one-time; creates/reuses the Privy-managed Solana wallet):
PYTHONPATH=. python bootstrap_privy_wallet.py

Important
---------
The dedicated Privy wallet address will be different from the Phantom "robot"
address. Only after the bootstrap wallet and dry-run logs are verified should a
small test amount be transferred to the Privy wallet.

This first-live package intentionally stops opening new positions after one
completed round trip. A broader always-on version should only be enabled after
that real transaction is reconciled on-chain and state persistence is verified.
