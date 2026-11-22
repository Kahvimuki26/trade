# Put the dashboard on your own domain (e.g. trade.icemail.fun)

This uses a **Cloudflare Tunnel**. It's free and needs no router or port-forwarding
changes, and your home IP stays hidden. Cloudflare forwards `https://trade.<yourdomain>`
to the dashboard running on your computer.

```
phone / laptop  ──https──>  Cloudflare  ──tunnel──>  your computer (tradebot web, localhost:8000)
```

> **The computer running tradebot has to be on** for the site to work and for the bot to trade.
> If you want it running 24/7 without your PC, see "Running it on a server" at the bottom.

## Step 1: Set a password for the dashboard

Anyone who opens the page can start/stop the bot and change settings, so lock it down.
Open the `.env` file in the project folder (create it from `.env.example` if you haven't)
and add a line:

```
TRADEBOT_PASSWORD=pick-a-long-password-here
```

Restart the dashboard. The browser now asks for a login (any username, that password).
As a safety net, tradebot **refuses requests coming through Cloudflare when no password is set**.

## Step 2: Create the tunnel (Cloudflare dashboard, about 5 minutes)

1. Log in at **dash.cloudflare.com**. In the left menu open **Zero Trust**
   (the first time, it asks you to pick a team name and the **Free** plan; a card is not charged).
2. Go to **Networks → Tunnels → Create a tunnel**, choose **Cloudflared**, and name it `tradebot`.
3. Pick your operating system (Windows / Mac). Cloudflare shows an install command.
   Copy it and run it:
   - **Windows:** open *Command Prompt as administrator* (Start menu → type `cmd` → right-click → Run as administrator) and paste it.
   - **Mac:** paste it into Terminal. On Mac you may need to install cloudflared first with `brew install cloudflared`.

   This installs the tunnel as a background service that starts with your computer.
4. Click **Next** to add a **Public hostname**:
   - Subdomain: `trade`
   - Domain: `icemail.fun`
   - Service type: `HTTP`
   - URL: `localhost:8000`
5. Save. Cloudflare creates the `trade.icemail.fun` DNS record automatically.

Start the dashboard (`start.bat` / `start.command`) and open **https://trade.icemail.fun**.

## Step 3 (recommended): Add a Cloudflare login in front of it

This means strangers never even reach your computer. Only people who log in with your
email get through.

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. Application domain: `trade.icemail.fun`.
3. Add a policy: Action **Allow**, Include → **Emails** → your own email address.
4. Save. Opening the site now asks for your email and sends you a one-time code.

With this plus `TRADEBOT_PASSWORD`, you have two locks.

## Troubleshooting

| Problem | Fix |
|---|---|
| `502 Bad Gateway` / "tunnel error" | The dashboard isn't running. Start it with `start.bat` / `start.command`. |
| "Set TRADEBOT_PASSWORD before exposing tradebot…" | Do Step 1, then restart the dashboard. |
| Page asks for a password you don't know | It's the `TRADEBOT_PASSWORD` value in `.env`. Any username works. |
| Tunnel shows "Inactive" in Cloudflare | The cloudflared service isn't running on your PC. Re-run the install command from Step 2.3. |

## Running it on a server (optional, later)

To keep it running 24/7, put the project on a small Linux VPS (about $4–6/month; Hetzner,
DigitalOcean etc.) or a Raspberry Pi. Install Python, then run `bash start.command` (it works on
Linux too), and install cloudflared there instead of on your PC. Point the tunnel's public
hostname at `localhost:8000` the same way.
