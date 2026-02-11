# Websites

## Structure

### 1) Install dependencies (Windows PowerShell)
```powershell
python -m pip install -r requirements.txt
```

### 2) Create an account
This project uses the open-source **FastAPI-Users** framework for authentication.

Users are stored in a local SQLite DB at `server/sensitive/users.db` (gitignored).

Create an admin user (superuser):
```powershell
python tools/manage_users.py add admin@example.com --admin
```

Remove a user:
```powershell
python tools/manage_users.py remove admin@example.com --admin
```

Overwrite a user:
```powershell
python tools/manage_users.py remove admin@example.com --admin --force
```

createa a api key:
```powershell
tools/manage_users.py apikey <email>
```

### 3) Start FastAPI (serves the site)
```powershell
python -m uvicorn server.app:app --host 127.0.0.1 --port 8000
```

Production:
- `APP_ENV=production` - set through environment Linux example: Environment="APP_ENV=production"
- `AUTH_JWT_SECRET` - Environment="AUTH_JWT_SECRET=put-a-long-random-string-here"
- Use `tools/manage_users.py apikey <email>` to rotate each account's API key (stored hashed in SQLite).

### 4) Start Nginx
1- install nginx
2- do "path to nginx.exe" "path to nginx folder" "path to nginx config folder"

## Cloudflare
DNS record (shares same ip):
    Use "curl ifconfig.me" to check for current IP
    Type A on domain
    CNName on www.domain
Security: 
    SSL/TLS - Full (strict)
    Private key: RSA, 3 years, key is downlaoded and send pasted to server
    Edge Certificates
    Always use HTTPS
    Automatic HTTPS rewrites

## Linux
Cert:
    sudo mkdir -p /etc/ssl/primowang.com
    sudo nano /etc/ssl/primowang.com/fullchain.pem
    sudo nano /etc/ssl/primowang.com/privkey.pem

    sudo chmod 644 /etc/ssl/primowang.com/fullchain.pem Owner read + write, other/group read only
    sudo chmod 600 /etc/ssl/primowang.com/privkey.pem Owner read + write, other/group no access
    sudo chown root:root /etc/ssl/primowang.com/fullchain.pem /etc/ssl/primowang.com/privkey.pem ensure proper ownership
Test + reload
    sudo nginx -t
    sudo systemctl reload nginx
Cloudflare tunnel
    cloudflared tunnel login
    cloudflared tunnel create primowang-tunnel
    mkdir -p ~/.cloudflared
    nano ~/.cloudflared/config.yml

    cloudflared tunnel route dns primowang-tunnel primowang.com
    cloudflared tunnel route dns primowang-tunnel www.primowang.com
running python
    sudo nano /etc/systemd/system/webrepo.service
    sudo systemctl daemon-reload
    sudo systemctl reset0fauked webrepo
    sudo systemctl start webrepo
Running linux tunnel
    sudo cloudflared service install
    cloudflared service installed

    sudo systemctl daemon-reload
    sudo systemctl enable cloudflared
    sudo systemctl start cloudflared



