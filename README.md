# WCL LAN Dashboard

A tiny Python/Flask dashboard for [Warcraft Logs](https://www.warcraftlogs.com/)
designed to run on a Raspberry Pi connected to a TV. It shows:

- The **latest dungeon** in your current log report (per-player Key % + DPS/HPS).
- The **session Key % average** across every completed dungeon in the report.

The numbers shown are scraped directly from the Warcraft Logs report website,
so they always match what you see in your browser. Just paste a report URL —
the dashboard automatically picks the most recent completed run.

## Setup

### 1. Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / Raspberry Pi
source .venv/bin/activate

pip install -r requirements.txt
```

No Warcraft Logs API key is required. The dashboard talks to the public
report pages using a Chrome TLS fingerprint via `curl_cffi`.

### 2. Run

```bash
python app.py
```

Open <http://localhost:8080> and paste a report URL (or code) — for example
`https://www.warcraftlogs.com/reports/ba8GyNv4nCKqgt7V`.

## Raspberry Pi kiosk tips

- Install Chromium and launch in kiosk mode on boot:

  ```bash
  chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8080
  ```

- Run the Flask app as a `systemd` service so it starts on boot. A minimal
  unit file:

  ```ini
  [Unit]
  Description=WCL LAN Dashboard
  After=network-online.target

  [Service]
  WorkingDirectory=/home/pi/wcl-lan-dashboard
  ExecStart=/home/pi/wcl-lan-dashboard/.venv/bin/python app.py
  Restart=on-failure
  User=pi

  [Install]
  WantedBy=multi-user.target
  ```

## Notes

- The API token is obtained via the OAuth2 client-credentials flow and cached
  in-memory until it expires.
- If `WCL_OAUTH_REDIRECT_URI` is configured and you connect via **Connect WCL**,
  the dashboard can also query the `/api/v2/user` endpoint with your
  authorization-code token.
- Without OAuth connection, only the public client API is used and reports must
  be publicly readable.
- Parse percentages and the session average are computed from the rankings
  returned by WCL. Untimed or still-in-progress dungeons with no rankings yet
  are skipped.
