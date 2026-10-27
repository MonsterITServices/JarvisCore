# Deploy Jarvis on Ubuntu Server

This keeps the Flask app private on loopback and exposes it through Nginx on port 80 with Basic Auth. Replace example values such as `192.168.1.50` and the LAN CIDR with your server's actual reserved IP and subnet.

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
sudo apt install -y python3 python3-venv nginx apache2-utils
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

Set `code_workbench.workspace_root` in `/opt/jarvis/app/config/rules.json` to `/var/lib/jarvis/Projects`. This keeps generated projects outside the Jarvis application tree.

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
```

Leave optional keys blank until configured. The app binds Flask to `127.0.0.1:5000`; do not change it to `0.0.0.0`.

## 4. Run Gunicorn under systemd

Create `/etc/systemd/system/jarvis.service`:

```ini
[Unit]
Description=Jarvis local AI assistant
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
curl http://127.0.0.1:5000/api/health
```

## 5. Put Nginx on port 80

Create `/etc/nginx/sites-available/jarvis` and replace the example IP:

```nginx
server {
    listen 80;
    server_name 192.168.1.50;

    client_max_body_size 26m;
    auth_basic "Jarvis LAN";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 240s;
    }
}
```

Create a login and enable the site:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd jarvis
sudo ln -s /etc/nginx/sites-available/jarvis /etc/nginx/sites-enabled/jarvis
sudo nginx -t
sudo systemctl reload nginx
```

If the default Nginx site conflicts on port 80, disable only that default site:

```bash
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

## 6. Restrict the firewall to your LAN

Use the actual trusted subnet, not necessarily this example:

```bash
sudo ufw allow OpenSSH
sudo ufw allow from 192.168.1.0/24 to any port 80 proto tcp
sudo ufw enable
sudo ufw status
```

Set the same LAN subnet in `security_policy.trusted_networks` in `config/rules.json`. Do not port-forward port 80 from your router to the internet. Basic Auth over plain HTTP is only appropriate on a trusted network; credentials are not encrypted in transit. For untrusted Wi-Fi or remote use, configure HTTPS/VPN before using it.

Open `http://192.168.1.50` from a device on the allowed LAN and sign in. Text chat and the code workbench will work. Browser microphone access generally requires a secure context; voice may require internal HTTPS even though the app is reachable on port 80.

## 7. Check logs and update

```bash
sudo journalctl -u jarvis -f
sudo journalctl -u nginx -f
```

After changing application files or Python dependencies:

```bash
sudo systemctl restart jarvis
```

The code workbench writes only under `/var/lib/jarvis/Projects`, presents a diff, and requires explicit approval. It does not execute generated code. Kali/SSH tooling, host scanning, and exploit testing are not part of this deployment.
