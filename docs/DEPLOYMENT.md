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
python3 tests/run_all.py            # Python suite; also run the JavaScript dashboard suite below
node --test tests/test_dashboard.js
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
Description=Crypto Hunter Trading System
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

## 5. Live-readiness gate — currently NOT CLEARED

**Do not enable live order routing on this checkout yet.** The latest
[code-hygiene audit](CODE_HYGIENE_AUDIT_2026-10-02.md) still lists uncertain-order recovery,
incident resolution, partial-fill/external-exit accounting, and authenticated venue acceptance
as blockers. Passing tests or paper fills do not close those gaps. Keep all venue modes in `paper`.

Before this guidance can change, the release owner must document and verify all of the following:

- [ ] The latest audit explicitly changes its live-trading decision after each blocker is fixed.
- [ ] Python and dashboard JavaScript suites plus static checks pass on the release commit.
- [ ] Order lookup, delayed/partial fills, cancel/replace and restart recovery are tested per venue.
- [ ] Protective-stop adoption/replacement and external exits are reconciled to authoritative fills.
- [ ] Dashboard/API token, TLS/reverse proxy, IP restrictions, exchange key permissions and backups are reviewed.
- [ ] Exchange-specific acceptance evidence is recorded without inferring it from paper/synthetic tests.

Do not treat this checklist as permission to switch `mode` to `live`; the current release gate is closed.

## 6. Operations

| Task | How |
|---|---|
| Pause new entries | dashboard ⏸ Pause (engine keeps managing open positions) |
| Emergency flatten | dashboard *Flatten all* |
| Restart engine | dashboard *Restart engine* (or `systemctl restart`) |
| Reset paper account | dashboard Settings → *Reset paper account* |
| Rotate API keys | dashboard Settings → *Clear*, then enter new keys, then *Test* |
| Reset the dashboard token | clear the browser's `ao.token` (or change `web.api_token` and restart) |
| Inspect why a trade was skipped | Dashboard → Signals page (stores the full rejection list) |
| Tune parameters | Dashboard → Settings page (validated, hot-applied, persisted) |

## 6b. Hardening checklist (from the 2026-10 audit)

| Item | Why | Where |
|---|---|---|
| Set `web.api_token` | live start is **refused** without it while the dashboard is bound to a public address; with it, every REST call needs `X-API-Token` and the WS needs `?token=` (the dashboard asks once) | `config.toml` → `[web]` |
| IP-restrict the API keys | a leaked key can then only be used from your VPS | venue key settings |
| Keep the halts on | 40 % drawdown / 25 % daily loss halt new entries; they do not cap losses on existing positions | `[risk]` |
| Expect affordability rejections on a small book | the venue's smallest order can exceed 8 % of a $20 account; such symbols are filtered out of the universe and rejected with a reason in the Signals tab | `universe.only_affordable_orders` |
| Never run two instances on one account | both would manage the same positions | — |

See [`CODE_HYGIENE_AUDIT_2026-10-02.md`](CODE_HYGIENE_AUDIT_2026-10-02.md) for current findings and blockers; `AUDIT_2026-10.md` is historical.

## 7. Known limitations

* Live exchange connectivity has not been certified by this audit. The REST/WS clients are written
  against venue documentation, and tests include signing vectors, mappings and paper/mocked paths;
  that is not proof of exchange acceptance. The live-readiness gate above stays closed until
  documented per-venue acceptance and recovery evidence is reviewed.
* Testnet/local replay: run the venue in `paper` mode first; there is no built-in exchange testnet
  switch (Binance/KuCoin testnets use different hosts — set `rest_base`/`ws_url` per venue to use
  them, but keep in mind their symbol universes and rate limits differ).
* The synthetic simulator is a GARCH-lite model, not a market replay — it is for exercising the
  pipeline, not for estimating live edge.
* Single-process by design; to run multiple strategies, use multiple isolated configs/data dirs
  (see CONFIGURATION.md) on different ports.

## Netlify-hosted dashboard

The static dashboard can be served from Netlify while the engine runs on your own server.
An edge function forwards every `/api/*` call to the engine, so the browser never talks to your
server directly and no CORS setup is needed.

1. In the Netlify UI, open **Project configuration → Environment variables** and add
   `ENGINE_URL` with your server's address, e.g. `http://203.0.113.10:8080`
   (a bare `203.0.113.10` or `203.0.113.10:8080` also works; the port defaults to `8080`).
2. Make sure the engine's port is reachable from the internet (firewall / security group).
3. Redeploy. Until `ENGINE_URL` is set, the dashboard shows an "Engine address not configured" error.

Set `web.api_token` before exposing the engine publicly — the dashboard prompts for it once.
