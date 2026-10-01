# Deployment

## 1. Host location

Latency to your venues' matching engines matters (`api.mexc.com`, `fapi.binance.com`,
`api-futures.kucoin.com`). Run on a VPS in the same region as the exchange's matching engine
(Singapore / Tokyo / Frankfurt depending on your account's routing) — typically single-digit
milliseconds vs 100–300 ms from home connections. The bot is a single Python process (asyncio,
a few hundred MB) — 1 vCPU / 1 GB RAM is enough for all three venues.

## 2. Install

```bash
git clone <your-repo> awesome && cd awesome
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.toml config.toml.bak      # keep the reference defaults
python3 tests/run_all.py            # 122 tests must pass before you trust it
python3 run.py --port 8080
```

Keep `data/` on a persistent disk: it contains one SQLite DB per venue
(`data/venues/{mexc,binance,kucoin}.db` — trades, equity curve, trailing state) and the machine key
for the encrypted credentials. **Back up `data/`** — losing the machine
key means re-entering the API keys from the dashboard.

## 3. Reverse proxy + TLS

The dashboard is plain HTTP and has no user accounts; protect it:

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    location / {
        auth_basic "bot";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;      # /ws needs this
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

Also set `web.api_token` in `config.toml` for defence in depth; the dashboard sends it as
`X-API-Token`.

## 4. systemd unit

```ini
[Unit]
Description=AO Divergence Futures Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/awesome
ExecStart=/home/trader/awesome/.venv/bin/python run.py --port 8080
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/trader/awesome/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now awesome-bot
journalctl -u awesome-bot -f
```

## 5. Going live checklist

- [ ] `python3 tests/run_all.py` passes.
- [ ] Paper mode has run long enough to see several full trade lifecycles (entry → trailing → exit).
- [ ] You have read the Signals tab and agree with the filter rejections.
- [ ] API key: futures orders enabled, withdrawals disabled, **IP whitelisted** to the VPS.
- [ ] Mode switched to `live` in Settings (the engine restarts itself).
- [ ] First live trade observed end to end, including a trailing stop modification.
- [ ] `data/` backed up and a restart tested (`restore()` must re-attach stops without
      re-opening positions).
- [ ] Alerts configured (the log is the source of truth; wire `data/bot.log` into your monitoring).

## 6. Operations

| Task | How |
|---|---|
| Pause new entries | dashboard ⏸ Pause (engine keeps managing open positions) |
| Emergency flatten | dashboard *Flatten all* |
| Restart engine | dashboard *Restart engine* (or `systemctl restart`) |
| Reset paper account | dashboard Settings → *Reset paper account* |
| Rotate API keys | dashboard Settings → *Clear*, then enter new keys, then *Test* |
| Reset the dashboard token | clear the browser's `ao.token` (or change `web.api_token` and restart) |
| Inspect why a trade was skipped | Signals tab (stores the full rejection list) |
| Tune parameters | Settings tab (validated, hot-applied, persisted) |

## 6b. Hardening checklist (from the 2026-10 audit)

| Item | Why | Where |
|---|---|---|
| Set `web.api_token` | live start is **refused** without it while the dashboard is bound to a public address; with it, every REST call needs `X-API-Token` and the WS needs `?token=` (the dashboard asks once) | `config.toml` → `[web]` |
| IP-restrict the API keys | a leaked key can then only be used from your VPS | venue key settings |
| Keep the halts on | 40 % drawdown / 25 % daily loss are the only thing between a bad day and a blown account | `[risk]` |
| Expect affordability rejections on a small book | the venue's smallest order can exceed 8 % of a $20 account; such symbols are filtered out of the universe and rejected with a reason in the Signals tab | `universe.only_affordable_orders` |
| Never run two instances on one account | both would manage the same positions | — |

See [`AUDIT_2026-10.md`](AUDIT_2026-10.md) for the full findings list.

## 7. Known limitations

* Live exchange connectivity could not be verified from the development sandbox (TLS to MEXC,
  Binance and KuCoin is blocked there); the REST/WS clients are written against the official docs
  and the paper path exercises all the same code, with signing vectors and order-mapping pinned by
  `tests/test_venues.py`. Validate each venue with a small live trade before trusting it with size.
* Testnet/local replay: run the venue in `paper` mode first; there is no built-in exchange testnet
  switch (Binance/KuCoin testnets use different hosts — set `rest_base`/`ws_url` per venue to use
  them, but keep in mind their symbol universes and rate limits differ).
* The synthetic simulator is a GARCH-lite model, not a market replay — it is for exercising the
  pipeline, not for estimating live edge.
* Single-process by design; to run multiple strategies, use multiple isolated configs/data dirs
  (see CONFIGURATION.md) on different ports.
