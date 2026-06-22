# Roku Control

Roku Control monitors YouTube and Plex playback on Roku devices, records playback
position, resumes identified media, and provides basic playback and power controls
from a web UI.

## Where it must run

Run Roku Control on an always-on Linux computer in the **same home network as the
Roku devices**. Roku ECP and Plex Companion are LAN protocols; installing this on
a remote VPS will not control a Roku behind a home router.

The host needs:

- Python 3.11 or newer
- LAN access to each Roku on TCP 8060 and, for Plex, TCP 8324
- outbound HTTPS access to Plex and YouTube services
- a browser on the LAN that can reach the host's TCP 8001

Use DHCP reservations for the Roku addresses so they do not change. Guest Wi-Fi,
client isolation, or VLAN firewall rules can prevent discovery and control.

## Quick start

```bash
git clone https://github.com/blandfx/roku-control.git
cd roku-control
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

Open `http://HOST_LAN_IP:8001`, click `+`, and enter each Roku's LAN address.
The SQLite database is created automatically on first run.

### Plex

1. In the Roku Plex app, enable **Settings > Remote Control**.
2. In Roku Control, select **Connect Plex Account** and complete Plex sign-in.
3. The signed-in account must already have access to the desired Plex server and
   libraries.

By default, the Play Plex search indexes libraries named exactly `Kid Movies` and
`Kid TV Shows`. Different library names can be configured before startup:

```bash
export PLEX_MOVIE_LIBRARY="Movies"
export PLEX_TV_LIBRARY="TV Shows"
.venv/bin/python main.py
```

Plex server addresses and access tokens are discovered from the signed-in account.
For a shared server at another house, Roku Control ignores the server owner's
private-LAN connection and uses its advertised remote HTTPS or relay connection.

## Install as a system service

The included unit uses `/opt/roku-control` and a restricted `roku-control` user:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv
sudo useradd --system --home-dir /opt/roku-control --shell /usr/sbin/nologin roku-control
sudo git clone https://github.com/blandfx/roku-control.git /opt/roku-control
sudo chown -R roku-control:roku-control /opt/roku-control
sudo -u roku-control python3 -m venv /opt/roku-control/.venv
sudo -u roku-control /opt/roku-control/.venv/bin/pip install -r /opt/roku-control/requirements.txt
sudo install -m 0644 /opt/roku-control/roku-monitor.service /etc/systemd/system/roku-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now roku-control.service
sudo systemctl status roku-control.service
```

Edit the `Environment` lines in `/etc/systemd/system/roku-control.service` if the
Plex library names or port differ, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart roku-control.service
```

## Security and data

Roku Control has no built-in user authentication. Keep port 8001 restricted to a
trusted LAN, or place it behind an authenticated HTTPS reverse proxy. Do not expose
it directly to the public internet.

`roku_monitor.db` contains device history and the Plex authorization token. It is
excluded from Git and must not be copied into images, repositories, or public
backups. Disconnecting Plex in the UI removes the stored account token.

## Portability notes

No Roku address, Plex server ID, Plex token, public hostname, or installation home
is embedded in the application. A new installation starts with an empty database;
the new user adds local Roku addresses and authorizes their own Plex account.

The same feature set works at another house when:

- Roku Control runs inside that house's LAN;
- local firewall/client-isolation rules permit ports 8060 and 8324;
- Plex Remote Control is enabled on the Roku;
- the signed-in Plex user can access the configured libraries; and
- the Plex server has remote access or relay connectivity if it is outside that LAN.

## Test

```bash
.venv/bin/python -m unittest -v
```
