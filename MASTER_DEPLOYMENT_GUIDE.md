# Mist Enterprise Suite v3 - Master Deployment Guide
## Complete Reference for Ubuntu Server Pro 25.10

---

## PART 1: QUICK START (30 MINUTES)

### Prerequisites
- Ubuntu Server Pro 25.10
- 4GB+ RAM, 20GB+ disk
- SSH access
- Anthropic API key (console.anthropic.com)
- Mist API token + Org ID

### Deployment (3 Steps)

```bash
# 1. Download script
curl -O https://your-repo/deploy-homelab.sh
chmod +x deploy-homelab.sh

# 2. Run (takes 15 min)
sudo ./deploy-homelab.sh

# 3. Configure
sudo nano /opt/mist-enterprise/.env
# Add: ANTHROPIC_API_KEY=sk-ant-...
# Add: MIST_API_TOKEN=...
# Add: MIST_ORG_ID=...

# Restart
sudo supervisorctl restart mist-api

# Access
# http://your-server-ip/
```

---

## PART 2: COMPLETE UBUNTU SETUP

### System Update
```bash
sudo apt-get update && sudo apt-get upgrade -y
```

### Install All Dependencies
```bash
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev \
  git curl wget nginx supervisor redis-server postgresql postgresql-contrib \
  certbot python3-certbot-nginx build-essential libpq-dev
```

### Create User & Directory
```bash
sudo useradd -m -s /bin/bash mist-api
sudo mkdir -p /opt/mist-enterprise
sudo chown mist-api:mist-api /opt/mist-enterprise
```

### Python Virtual Environment
```bash
cd /opt/mist-enterprise
sudo -u mist-api python3.11 -m venv venv
sudo -u mist-api bash -c "source venv/bin/activate && pip install --upgrade pip"
```

### Install Python Packages
```bash
sudo -u mist-api bash << 'EOF'
source /opt/mist-enterprise/venv/bin/activate
pip install Flask==3.0.0 python-dotenv==1.0.0 requests==2.31.0 \
  anthropic==0.7.0 psycopg2-binary==2.9.9 gunicorn==21.2.0 \
  redis==5.0.1 prometheus-client==0.18.0 python-json-logger==2.0.7 \
  flask-cors==4.0.0 flask-limiter==3.5.0 cryptography==41.0.7
EOF
```

### Create Environment File
```bash
sudo -u mist-api cat > /opt/mist-enterprise/.env << 'ENV'
ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE
MIST_API_TOKEN=YOUR_MIST_TOKEN_HERE
MIST_ORG_ID=your_org_id
FLASK_ENV=production
FLASK_DEBUG=false
DATABASE_URL=postgresql://mist:mist@localhost:5432/mist_db
REDIS_URL=redis://localhost:6379/0
JWT_SECRET_KEY=change-this-to-random-secret-key
CORS_ORIGINS=http://localhost,https://yourdomain.com
WEBHOOK_SECRET=your-webhook-secret
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=100
ENV

sudo chmod 600 /opt/mist-enterprise/.env
```

### Copy Flask App (app.py to /opt/mist-enterprise/)
```bash
# app.py provided in package - copy it here
sudo cp app.py /opt/mist-enterprise/app.py
sudo chown mist-api:mist-api /opt/mist-enterprise/app.py
```

### Configure Supervisor
```bash
sudo tee /etc/supervisor/conf.d/mist-api.conf > /dev/null << 'SUP'
[program:mist-api]
directory=/opt/mist-enterprise
command=/opt/mist-enterprise/venv/bin/gunicorn --workers 4 --threads 2 \
  --worker-class sync --bind 127.0.0.1:8000 --timeout 60 app:app
user=mist-api
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/mist-api.log
stdout_logfile_maxbytes=10485760
stdout_logfile_backups=10
environment=PATH="/opt/mist-enterprise/venv/bin"
SUP

sudo systemctl enable supervisor
sudo systemctl start supervisor
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start mist-api
```

### Configure Nginx
```bash
sudo tee /etc/nginx/sites-available/mist-api > /dev/null << 'NGX'
upstream mist_api {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name _;
    client_max_body_size 10M;

    location / {
        proxy_pass http://mist_api;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
    location /health {
        proxy_pass http://mist_api;
        access_log off;
    }
}
NGX

sudo ln -sf /etc/nginx/sites-available/mist-api /etc/nginx/sites-enabled/mist-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

### Setup Database
```bash
sudo -u postgres psql << 'SQL'
CREATE DATABASE mist_db;
CREATE USER mist WITH PASSWORD 'mist';
ALTER ROLE mist SET client_encoding TO 'utf8';
ALTER ROLE mist SET default_transaction_isolation TO 'read committed';
ALTER ROLE mist SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE mist_db TO mist;
SQL

sudo systemctl enable redis-server
sudo systemctl start redis-server
```

### Deploy Dashboard
```bash
mkdir -p /opt/mist-enterprise/static
sudo cp mist-enterprise-suite-v3-ai.html /opt/mist-enterprise/static/index.html
sudo chown -R mist-api:mist-api /opt/mist-enterprise/static
```

---

## PART 3: CLAUDE MCP INTEGRATION

### What is MCP?
Model Context Protocol allows Claude to access Mist Cloud data directly.

### 5 MCP Tools

1. **find_mist_entity** - Search devices, sites, networks
   ```
   "List my offline APs" → Returns device list
   ```

2. **get_mist_config** - Review configurations
   ```
   "Show SSID settings" → Returns formatted config
   ```

3. **get_mist_stats** - Performance metrics
   ```
   "What's the CPU usage?" → Returns metrics
   ```

4. **get_mist_insights** - AI troubleshooting
   ```
   "Why are devices offline?" → Root cause analysis
   ```

5. **search_mist_data** - Query event logs
   ```
   "Show auth failures" → Returns logs
   ```

### Configuration
In app.py:
```python
MCP_SERVERS = {
    "mist-cloud": {
        "type": "url",
        "url": "https://mcp.mist.com/api/v1",
        "auth": {"type": "bearer", "token": os.environ['MIST_API_TOKEN']}
    }
}
```

---

## PART 4: MIST ENTERPRISE API REFERENCE

### Base URL
```
https://api.mist.com/api/v1
```

### Authentication
```bash
-H "Authorization: Token YOUR_TOKEN"
```

### Core Endpoints

**List Sites**
```bash
GET /orgs/{org_id}/sites
```

**List Devices**
```bash
GET /sites/{site_id}/devices
```

**Get Device Stats**
```bash
GET /sites/{site_id}/devices/{device_id}/stats
```

**Create SSID**
```bash
POST /sites/{site_id}/wlans
{
  "ssid": "MyNetwork",
  "enabled": true,
  "security": {"type": "wpa2"}
}
```

**Reboot Device**
```bash
POST /sites/{site_id}/devices/{device_id}/reboot
```

**Create Webhook**
```bash
POST /orgs/{org_id}/webhooks
{
  "url": "https://your-webhook-url",
  "events": ["device_down", "device_up"]
}
```

---

## PART 5: AUTOMATION GUIDE

### Trigger Types

**Device Events**
```python
{
  "type": "device_offline",
  "duration_minutes": 60
}
```

**Threshold Events**
```python
{
  "type": "metric_threshold",
  "metric": "cpu_usage",
  "operator": "greater_than",
  "threshold": 80,
  "duration_seconds": 300
}
```

**Scheduled**
```python
{
  "type": "schedule",
  "cron": "0 2 * * *"  # 2 AM daily
}
```

### Action Types

**Notifications**
```python
{
  "type": "notification",
  "channels": ["slack", "email"],
  "message": "Device {device_id} went offline"
}
```

**Device Actions**
```python
{
  "type": "device_action",
  "action": "reboot"
}
```

**Integrations**
```python
{
  "type": "ticket",
  "system": "servicenow",
  "title": "Alert: {alarm_type}"
}
```

### Example Automation
```bash
curl -X POST http://localhost/api/v1/automations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Auto-reboot offline APs",
    "trigger": {"type": "device_offline", "duration_minutes": 60},
    "actions": [
      {"type": "device_action", "action": "reboot"},
      {"type": "notification", "channels": ["slack"], "message": "Rebooted {device_id}"}
    ],
    "enabled": true
  }'
```

---

## PART 6: INTEGRATIONS GUIDE

### Slack
```python
import requests
webhook = "https://hooks.slack.com/services/..."
requests.post(webhook, json={"text": "Alert: Device offline"})
```

### PagerDuty
```python
requests.post(
    "https://api.pagerduty.com/incidents",
    headers={"Authorization": f"Token token={TOKEN}"},
    json={"incident": {"title": "Alert", "service": {"id": SERVICE_ID}}}
)
```

### Datadog
```python
from datadog import initialize, api
api.Metric.send(metric='mist.device.cpu', points=75, tags=['device:ap-001'])
```

### ServiceNow
```python
from pysnow import Client
snow = Client(host='instance.service-now.com', user=USER, password=PWD)
incident.create({'short_description': 'Mist Alert'})
```

### AWS Lambda
```python
import boto3
lambda_client = boto3.client('lambda')
lambda_client.invoke(FunctionName='mist-automation', InvocationType='Event')
```

---

## PART 7: OPERATIONS & DEPLOYMENT

### Service Management

**Check Status**
```bash
sudo supervisorctl status
docker-compose ps
systemctl status nginx
```

**View Logs**
```bash
tail -f /var/log/mist-api.log
sudo supervisorctl tail -f mist-api
docker-compose logs -f mist-api
```

**Health Checks**
```bash
curl http://localhost/health
curl http://localhost/ready
```

### Maintenance

**Backup Database**
```bash
sudo -u postgres pg_dump mist_db > backup.sql
```

**Restore Database**
```bash
sudo -u postgres psql mist_db < backup.sql
```

**Update Application**
```bash
cd /opt/mist-enterprise
source venv/bin/activate
pip install --upgrade anthropic
sudo supervisorctl restart mist-api
```

### Performance Tuning

**Increase Workers**
```bash
sudo nano /etc/supervisor/conf.d/mist-api.conf
# Change: --workers 4 --threads 2
# For 8 cores: --workers 8 --threads 2
sudo supervisorctl restart mist-api
```

**PostgreSQL**
```bash
sudo nano /etc/postgresql/*/main/postgresql.conf
# shared_buffers = 256MB
# effective_cache_size = 1GB
# work_mem = 16MB
```

### Security

**Firewall**
```bash
sudo ufw enable
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

**SSL/TLS**
```bash
sudo certbot --nginx -d your-domain.com
```

**File Permissions**
```bash
sudo chmod 600 /opt/mist-enterprise/.env
sudo chown mist-api:mist-api /opt/mist-enterprise/.env
```

---

## PART 8: API CHEATSHEET

### Environment
```bash
export MIST_TOKEN="your_token"
export ORG_ID="your_org_id"
export SITE_ID="your_site_id"
export API_KEY="sk-ant-your-key"
```

### Essential Commands

**List Sites**
```bash
curl https://api.mist.com/api/v1/orgs/$ORG_ID/sites \
  -H "Authorization: Token $MIST_TOKEN"
```

**List Devices**
```bash
curl https://api.mist.com/api/v1/sites/$SITE_ID/devices \
  -H "Authorization: Token $MIST_TOKEN"
```

**Get Device Stats**
```bash
curl https://api.mist.com/api/v1/sites/$SITE_ID/devices/$DEVICE_ID/stats \
  -H "Authorization: Token $MIST_TOKEN"
```

**Create SSID**
```bash
curl -X POST https://api.mist.com/api/v1/sites/$SITE_ID/wlans \
  -H "Authorization: Token $MIST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ssid":"MyNet","enabled":true,"security":{"type":"open"}}'
```

**Reboot Device**
```bash
curl -X POST https://api.mist.com/api/v1/sites/$SITE_ID/devices/$DEVICE_ID/reboot \
  -H "Authorization: Token $MIST_TOKEN"
```

### Python Pagination
```python
page = 1
all_items = []
while True:
    items = requests.get(f"{URL}?page={page}", headers=HEADERS).json()
    if not items: break
    all_items.extend(items)
    page += 1
```

### Error Codes
```
200 = Success
400 = Bad request
401 = Unauthorized
403 = Forbidden
404 = Not found
429 = Rate limited (wait before retry)
500 = Server error
```

---

## PART 9: DASHBOARD FEATURES

### 8 Integrated Tabs

1. **Dashboard** - Real-time monitoring & events
2. **Devices** - Inventory management
3. **Webhooks** - Event simulator
4. **Automation** - Workflow engine
5. **Integrations** - 8+ platform connectors
6. **Analytics** - Performance metrics
7. **Code** - API examples
8. **🤖 AI Assistant** - Claude with MCP

### Each Tab Provides
- Real-time data
- Interactive controls
- Code examples
- Copy-paste ready configs

---

## PART 10: TROUBLESHOOTING

### Dashboard Not Loading
```bash
sudo supervisorctl status
tail -f /var/log/mist-api.log
curl http://localhost/health
sudo systemctl restart nginx
```

### Claude AI Not Working
```bash
echo $ANTHROPIC_API_KEY
python3 -c "from anthropic import Anthropic; print('OK')"
sudo supervisorctl tail -100 mist-api
```

### Mist API Errors
```bash
curl -H "Authorization: Token $MIST_API_TOKEN" \
  https://api.mist.com/api/v1/self
```

### High Resource Usage
```bash
htop
sudo nano /etc/supervisor/conf.d/mist-api.conf
# Reduce workers
sudo supervisorctl restart mist-api
```

### Database Issues
```bash
psql -U mist -d mist_db -h localhost
sudo systemctl restart postgresql
sudo -u postgres pg_dump mist_db > backup.sql
```

---

## PART 11: DOCKER ALTERNATIVE

### Quick Docker Setup
```bash
# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER

# Create .env
cat > .env << 'ENV'
ANTHROPIC_API_KEY=sk-ant-...
MIST_API_TOKEN=...
MIST_ORG_ID=...
JWT_SECRET_KEY=random-secret
CORS_ORIGINS=http://localhost
ENV

# Start services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f mist-api

# Access
# http://localhost/
```

---

## PART 12: FILE MANIFEST

### Application Files (1.4MB)
- **mist-enterprise-suite-v3-ai.html** (411KB) - Full dashboard
- **app.py** - Flask backend with Claude + MCP
- **requirements.txt** - Python packages
- **docker-compose.yml** - Container config
- **Dockerfile** - Image definition
- **nginx.conf** - Web server config
- **deploy-homelab.sh** - Automated script

### Documentation Files (350KB+)
- **HOMELAB_QUICKSTART.md** (10KB) - Fast setup
- **UBUNTU_HOMELAB_DEPLOYMENT.md** (25KB) - Manual guide
- **CLAUDE_MCP_INTEGRATION_GUIDE.md** (15KB) - AI details
- **MIST_ENTERPRISE_GUIDE.md** (17KB) - API reference
- **MIST_AUTOMATION_GUIDE.md** (24KB) - Automation
- **MIST_INTEGRATIONS_GUIDE.md** (17KB) - Integrations
- **DEPLOYMENT_AND_OPERATIONS_GUIDE.md** (21KB) - Operations
- **MIST_API_CHEATSHEET.md** (6KB) - Quick ref
- **README.md** (12KB) - Overview
- **DEPLOYMENT_FILES_SUMMARY.md** (9KB) - Package info
- **MASTER_DEPLOYMENT_GUIDE.md** - This file (all in one)

---

## PART 13: QUICK REFERENCE

### First Time?
1. Read this guide (30 min)
2. Run deployment script (15 min)
3. Configure API keys (5 min)
4. Access dashboard (2 min)
5. Try Claude AI (5 min)

### Already Running?
1. Check status: `sudo supervisorctl status`
2. View logs: `tail -f /var/log/mist-api.log`
3. Health check: `curl http://localhost/health`
4. Restart: `sudo supervisorctl restart mist-api`

### Common Tasks

**Restart Service**
```bash
sudo supervisorctl restart mist-api
```

**Update Config**
```bash
sudo nano /opt/mist-enterprise/.env
sudo supervisorctl restart mist-api
```

**View Logs**
```bash
tail -f /var/log/mist-api.log
```

**Backup Data**
```bash
sudo -u postgres pg_dump mist_db > backup.sql
```

**Test API**
```bash
curl -X POST http://localhost/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query":"List my devices"}'
```

---

## PART 14: GETTING HELP

### If Something Goes Wrong

1. **Check logs first**
   ```bash
   tail -f /var/log/mist-api.log
   sudo supervisorctl tail -100 mist-api
   ```

2. **Verify services**
   ```bash
   sudo supervisorctl status
   systemctl status nginx
   systemctl status postgresql
   systemctl status redis-server
   ```

3. **Test endpoints**
   ```bash
   curl http://localhost/health
   curl http://localhost/ready
   ```

4. **Check configuration**
   ```bash
   sudo cat /opt/mist-enterprise/.env
   ```

5. **Review documentation**
   - API issues? → MIST_ENTERPRISE_GUIDE.md
   - Automation? → MIST_AUTOMATION_GUIDE.md
   - Claude? → CLAUDE_MCP_INTEGRATION_GUIDE.md
   - Setup? → UBUNTU_HOMELAB_DEPLOYMENT.md

---

## PART 15: SUMMARY

### What You Have
✅ Production-ready dashboard
✅ Flask backend with Claude AI
✅ PostgreSQL database
✅ Redis cache
✅ Nginx reverse proxy
✅ Supervisor auto-restart
✅ Complete documentation
✅ API examples

### What You Can Do
✅ Monitor devices in real-time
✅ Create automations
✅ Connect integrations
✅ Query with Claude AI
✅ Generate code examples
✅ Troubleshoot issues
✅ Scale to multiple servers

### Deployment Time
- Automated: 15 minutes
- Docker: 10 minutes
- Manual: 60 minutes

### Support
- Documentation: 350KB+
- Examples: 50+
- APIs: Fully documented
- Troubleshooting: Complete

---

## NEXT STEPS

1. **Download** all files from `/mnt/user-data/outputs/`
2. **Choose** deployment method (automated recommended)
3. **Gather** API keys
4. **Run** deployment
5. **Configure** .env
6. **Access** dashboard
7. **Try** Claude AI

**Total time to production: 30 minutes** ⚡

---

**Version**: 3.0
**Status**: ✅ Ready for Deployment
**Updated**: April 2024
**All files in**: `/mnt/user-data/outputs/`

Happy Deploying! 🚀

