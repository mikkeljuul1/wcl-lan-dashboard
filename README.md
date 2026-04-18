# WCL LAN Dashboard

A tiny Python/Flask dashboard for [Warcraft Logs](https://www.warcraftlogs.com/)
designed to run on a Raspberry Pi connected to a TV. It shows:

- The **latest dungeon** in your current log report (per-player Key % + DPS/HPS).
- The **session Key % average** across every completed dungeon in the report.

The dashboard polls the Warcraft Logs v2 GraphQL API every minute, so new
pulls appear automatically without interaction.

## Setup

### 1. Create a Warcraft Logs API client

1. Sign in and go to <https://www.warcraftlogs.com/api/clients/>.
2. Click **Create Client**.
3. Name: anything (e.g. `lan-dashboard`). Redirect URL: `http://localhost`.
4. Leave **Public Client** unchecked.
5. Copy the **Client ID** and **Client Secret**.

### 2. Configure and install

```bash
cp .env.example .env
# edit .env and fill in WCL_CLIENT_ID / WCL_CLIENT_SECRET

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / Raspberry Pi
source .venv/bin/activate

pip install -r requirements.txt
```

If you want user-authenticated API access (recommended for best data parity),
also set these values in `.env`:

```bash
WCL_OAUTH_REDIRECT_URI=http://localhost:8080/auth/wcl/callback
FLASK_SECRET_KEY=replace_with_a_long_random_string
```

The redirect URI must be added to your Warcraft Logs API client settings.

### 3. Run

```bash
python app.py
```

Open <http://localhost:8080> and paste a report URL (or code) — for example
`https://www.warcraftlogs.com/reports/ba8GyNv4nCKqgt7V`.

If OAuth is configured, click **Connect WCL** in the header to authorize the
dashboard against your Warcraft Logs account.

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
