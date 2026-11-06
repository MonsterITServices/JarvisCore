# Deploy Computer on Ubuntu Server

This keeps the Flask app private on loopback and uses Apache on port 8080. Replace example values such as `192.168.1.50` and the LAN CIDR with your server's actual reserved IP and subnet.

## Quick Install

From Windows PowerShell, copy the project folder to your Ubuntu server:

```powershell
scp -r C:\JarvisCore <linux-user>@<server-ip>:/tmp/
ssh <linux-user>@<server-ip>
sudo bash /tmp/JarvisCore/deploy/ubuntu/install.sh
```

The installer asks for the server LAN IPv4, trusted LAN CIDR, tunnel hostname, optional Ollama model, and the first Computer username and password. It retains existing accounts on subsequent runs. The 30B default model is a large download; choose a smaller model when RAM is limited. The app itself stays bound to `127.0.0.1:5000`.

Open your HTTPS tunnel address to sign in. The existing Cloudflare tunnel is not modified; its route must already point to the Computer backend or Apache on port 8080. Add and remove users in Computer Settings; all users have full access, including user management. The last user cannot be removed. Accounts and active sessions are stored in `/var/lib/jarvis/.local/share/jarviscore/auth.sqlite3`; back this file up securely. Sign-in over remote plain HTTP is disabled. Local loopback HTTP is allowed for development.

The script has not been executed in this Windows workspace because Bash/WSL is unavailable here. Run it on the Ubuntu machine; read the script first if you need to adapt it for an existing Apache/firewall setup.

## 1. Prepare the server

Reserve a stable LAN address for the Ubuntu laptop in your router (DHCP reservation is preferred). From Windows PowerShell, copy the project to the server:

```powershell
scp -r C:\JarvisCore\* <linux-user>@192.168.1.50:/tmp/jarvis-source/
```

Connect to Ubuntu:

```powershell
ssh <linux-user>@192.168.1.50
```

Install system packages and create a dedicated service user:

```bash
sudo apt update
sudo apt install -y python3 python3-venv apache2 apache2-utils
sudo adduser --system --group --home /var/lib/jarvis jarvis
sudo install -d -o root -g jarvis -m 0750 /opt/jarvis/app
sudo install -d -o jarvis -g jarvis -m 0750 /var/lib/jarvis/Projects
sudo install -d -o jarvis -g jarvis -m 0700 /var/lib/jarvis/.local/share/jarviscore
sudo cp -a /tmp/jarvis-source/. /opt/jarvis/app/
sudo chown -R root:jarvis /opt/jarvis/app
sudo chown root:jarvis /opt/jarvis/app/config
sudo chmod 2770 /opt/jarvis/app/config
sudo chown jarvis:jarvis /opt/jarvis/app/config/rules.json
```

Set `code_workbench.workspace_root` in `/opt/jarvis/app/config/rules.json` to `/var/lib/jarvis/Projects`. This keeps generated projects outside the Computer application tree.

## 2. Install Python dependencies

```bash
sudo -u jarvis python3 -m venv /opt/jarvis/venv
sudo -u jarvis /opt/jarvis/venv/bin/pip install --upgrade pip
sudo -u jarvis /opt/jarvis/venv/bin/pip install -r /opt/jarvis/app/requirements.txt
```

Install Ollama using its official Linux instructions, then fetch the configured local model. The 30B model is large and needs substantial memory; choose a smaller Ollama model and set `JARVIS_OLLAMA_MODEL` if the laptop cannot run it. RapidOCR's ONNX models are included with its Python package and run locally.

```bash
ollama pull qwen3-coder:30b
```

Verify Ollama answers locally before continuing:

```bash
ollama run qwen3-coder:30b "Reply with: ready"
```

## 3. Configure runtime environment

Create a root-owned environment file. Add secrets only on the server; do not put them in Git or the Windows-shared project copy.

```bash
sudo install -o root -g jarvis -m 0640 /dev/null /etc/jarvis.env
sudo nano /etc/jarvis.env
```

Example contents:

```ini
HOME=/var/lib/jarvis
JARVIS_OLLAMA_MODEL=qwen3-coder:30b
BRAVE_SEARCH_API_KEY=
JARVIS_HA_URL=
JARVIS_HA_TOKEN=
JARVIS_PUBLIC_ORIGIN=https://jarvis.monsterit.co.uk
```

Leave optional keys blank until configured. Set the public origin to your HTTPS tunnel hostname. The app binds Flask to `127.0.0.1:5000`; do not change it to `0.0.0.0`. For a manual installation, create the first user before starting the service:

```bash
cd /opt/jarvis/app
sudo -u jarvis env HOME=/var/lib/jarvis /opt/jarvis/venv/bin/python -m integrations.auth --bootstrap
```

## 4. Run Gunicorn under systemd

Create `/etc/systemd/system/jarvis.service`:

```ini
[Unit]
Description=Computer local AI assistant
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=jarvis
Group=jarvis
WorkingDirectory=/opt/jarvis/app
EnvironmentFile=/etc/jarvis.env
Environment=HOME=/var/lib/jarvis
Environment=JARVIS_KNOWLEDGE_DB=/var/lib/jarvis/.local/share/jarviscore/knowledge.sqlite3
ExecStart=/opt/jarvis/venv/bin/gunicorn --bind 127.0.0.1:5000 --workers 1 --threads 4 --timeout 240 app:app
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/opt/jarvis/app/config /var/lib/jarvis/Projects /var/lib/jarvis/.local/share/jarviscore

[Install]
WantedBy=multi-user.target
```

One Gunicorn worker is intentional: staged code diffs are held in process memory between preview and approval. Enable the service and test the loopback backend:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis
sudo systemctl status jarvis
curl http://127.0.0.1:5000/login
```

## 5. Put Apache on port 8080

Create `/etc/apache2/sites-available/jarvis.conf` and replace the example IP and LAN subnet:

```apache
Listen 8080
<VirtualHost *:8080>
    ServerName 192.168.1.50
    LimitRequestBody 27262976
    ProxyPreserveHost On
    ProxyTimeout 240
    ProxyPass / http://127.0.0.1:5000/ timeout=240
    ProxyPassReverse / http://127.0.0.1:5000/
    RequestHeader set X-Forwarded-Proto "http"

    <Location />
        Require ip 192.168.1.0/24
    </Location>
</VirtualHost>
```

Enable the site:

```bash
sudo a2enmod proxy proxy_http headers authz_host
sudo a2dissite 000-default
sudo a2ensite jarvis
sudo apache2ctl configtest
sudo systemctl restart apache2
```

For a tunnel routed through Apache, also configure the public hostname virtual host as shown in the installer, with `Require local` when cloudflared runs on the same host. Preserve existing Apache sites and tunnel routes.

## 6. Restrict the firewall to your LAN

Use the actual trusted subnet, not necessarily this example:

```bash
sudo ufw allow OpenSSH
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw enable
sudo ufw status
```

Set the same LAN subnet in `security_policy.trusted_networks` in `config/rules.json`. Do not port-forward port 8080 to the internet. Use the HTTPS tunnel for sign-in and microphone access. Cloudflare Access provides an additional recommended protection layer.

## 7. Check logs and update

```bash
sudo journalctl -u jarvis -f
sudo journalctl -u apache2 -f
```

After changing application files or Python dependencies:

```bash
sudo systemctl restart jarvis
```

The code workbench writes only under `/var/lib/jarvis/Projects`, presents a diff, and requires explicit approval. It does not execute generated code. Kali/SSH tooling, host scanning, and exploit testing are not part of this deployment.
