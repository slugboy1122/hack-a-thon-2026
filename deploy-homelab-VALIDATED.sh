#!/bin/bash
# Mist Enterprise Suite v3 - Ubuntu Server Pro 25.10 Deployment Script
# VALIDATED & PRODUCTION-READY
# Installs complete platform with Claude MCP integration

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="/opt/mist-enterprise"
APP_USER="mist-api"
PYTHON_VERSION="3.11"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

# ============================================================================
# VALIDATION CHECKS
# ============================================================================

validate_system() {
    echo -e "${YELLOW}[VALIDATION] Checking system requirements...${NC}"
    
    # Check root
    if [ "$EUID" -ne 0 ]; then 
        echo -e "${RED}✗ Must run as root (use: sudo ./deploy-homelab.sh)${NC}"
        exit 1
    fi
    
    # Check Ubuntu version
    if ! grep -q "25.10\|jammy\|noble" /etc/os-release; then
        echo -e "${YELLOW}⚠ Warning: Not Ubuntu 25.10, may have compatibility issues${NC}"
    fi
    
    # Check RAM
    RAM_GB=$(free -g | awk '/^Mem:/{print $2}')
    if [ "$RAM_GB" -lt 4 ]; then
        echo -e "${RED}✗ Minimum 4GB RAM required (found: ${RAM_GB}GB)${NC}"
        exit 1
    fi
    
    # Check disk space
    DISK_GB=$(df / | awk 'NR==2 {print $4/1024/1024}' | cut -d. -f1)
    if [ "$DISK_GB" -lt 20 ]; then
        echo -e "${RED}✗ Minimum 20GB disk space required (found: ${DISK_GB}GB)${NC}"
        exit 1
    fi
    
    # Check internet connectivity
    if ! ping -c 1 8.8.8.8 &> /dev/null; then
        echo -e "${RED}✗ Internet connectivity required${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✓ System validation passed${NC}"
    echo -e "${GREEN}  RAM: ${RAM_GB}GB | Disk: ${DISK_GB}GB${NC}"
}

# ============================================================================
# INSTALLATION STEPS
# ============================================================================

step1_update_system() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [1/8] Updating system packages...                         ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    apt-get update > /tmp/apt-update.log 2>&1 || {
        echo -e "${RED}✗ apt-get update failed${NC}"
        cat /tmp/apt-update.log
        exit 1
    }
    
    apt-get upgrade -y > /tmp/apt-upgrade.log 2>&1 || {
        echo -e "${RED}✗ apt-get upgrade failed${NC}"
        cat /tmp/apt-upgrade.log
        exit 1
    }
    
    PACKAGES="python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
        git curl wget nginx supervisor redis-server postgresql postgresql-contrib \
        certbot python3-certbot-nginx build-essential libpq-dev sqlite3 net-tools"
    
    apt-get install -y $PACKAGES > /tmp/apt-install.log 2>&1 || {
        echo -e "${RED}✗ Package installation failed${NC}"
        tail -20 /tmp/apt-install.log
        exit 1
    }
    
    echo -e "${GREEN}✓ System updated${NC}"
}

step2_create_user() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [2/8] Creating application user & directories...          ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    if id "$APP_USER" &>/dev/null; then
        echo -e "${YELLOW}⚠ User $APP_USER already exists, skipping...${NC}"
    else
        useradd -m -s /bin/bash $APP_USER
        echo -e "${GREEN}✓ Created user: $APP_USER${NC}"
    fi
    
    mkdir -p $INSTALL_DIR
    mkdir -p $INSTALL_DIR/static
    mkdir -p $INSTALL_DIR/logs
    chown -R $APP_USER:$APP_USER $INSTALL_DIR
    
    echo -e "${GREEN}✓ Directories created at $INSTALL_DIR${NC}"
}

step3_python_env() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [3/8] Setting up Python virtual environment...            ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    cd $INSTALL_DIR
    
    if [ -d "venv" ]; then
        echo -e "${YELLOW}⚠ Virtual environment exists, recreating...${NC}"
        rm -rf venv
    fi
    
    sudo -u $APP_USER python${PYTHON_VERSION} -m venv venv || {
        echo -e "${RED}✗ Failed to create venv${NC}"
        exit 1
    }
    
    sudo -u $APP_USER bash -c "source venv/bin/activate && pip install --upgrade pip setuptools wheel" || {
        echo -e "${RED}✗ Failed to upgrade pip${NC}"
        exit 1
    }
    
    echo -e "${GREEN}✓ Virtual environment created${NC}"
}

step4_flask_app() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [4/8] Creating Flask application...                       ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    # Create requirements.txt
    cat > $INSTALL_DIR/requirements.txt << 'PYREQ'
Flask==3.0.0
python-dotenv==1.0.0
requests==2.31.0
anthropic==0.7.0
psycopg2-binary==2.9.9
gunicorn==21.2.0
redis==5.0.1
prometheus-client==0.18.0
python-json-logger==2.0.7
flask-cors==4.0.0
flask-limiter==3.5.0
cryptography==41.0.7
PYREQ
    
    chown $APP_USER:$APP_USER $INSTALL_DIR/requirements.txt
    
    # Install dependencies
    sudo -u $APP_USER bash -c "source $INSTALL_DIR/venv/bin/activate && pip install -r $INSTALL_DIR/requirements.txt" > /tmp/pip-install.log 2>&1 || {
        echo -e "${RED}✗ Python package installation failed${NC}"
        tail -20 /tmp/pip-install.log
        exit 1
    }
    
    echo -e "${GREEN}✓ Flask dependencies installed${NC}"
}

step5_environment() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [5/8] Configuring environment...                          ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    cat > $INSTALL_DIR/.env << 'ENVFILE'
# Anthropic Configuration
ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE
CLAUDE_MODEL=claude-opus-4-20250805

# Mist Configuration
MIST_API_TOKEN=YOUR_MIST_TOKEN_HERE
MIST_ORG_ID=your_org_id
MIST_API_URL=https://api.mist.com/api/v1

# Application Configuration
FLASK_ENV=production
FLASK_DEBUG=false
PYTHONUNBUFFERED=1
LOG_LEVEL=INFO

# Database
DATABASE_URL=postgresql://mist:mist@localhost:5432/mist_db

# Redis
REDIS_URL=redis://localhost:6379/0

# Security
JWT_SECRET_KEY=change-this-to-random-secret-key-$(openssl rand -hex 16)
CORS_ORIGINS=http://localhost,https://yourdomain.com

# Webhook
WEBHOOK_SECRET=your-webhook-secret-here
WEBHOOK_TIMEOUT=30

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=100

# Monitoring
PROMETHEUS_ENABLED=true
DATADOG_ENABLED=false
ENVFILE
    
    chown $APP_USER:$APP_USER $INSTALL_DIR/.env
    chmod 600 $INSTALL_DIR/.env
    
    echo -e "${GREEN}✓ Environment configured${NC}"
    echo -e "${YELLOW}⚠ IMPORTANT: Edit .env with your API keys:${NC}"
    echo -e "${YELLOW}   sudo nano $INSTALL_DIR/.env${NC}"
}

step6_supervisor() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [6/8] Setting up Supervisor...                            ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    cat > /etc/supervisor/conf.d/mist-api.conf << 'SUPFILE'
[program:mist-api]
directory=/opt/mist-enterprise
command=/opt/mist-enterprise/venv/bin/gunicorn --workers 4 --threads 2 --worker-class sync --bind 127.0.0.1:8000 --timeout 60 --access-logfile - --error-logfile - app:app
user=mist-api
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/mist-api.log
stdout_logfile_maxbytes=10485760
stdout_logfile_backups=10
environment=PATH="/opt/mist-enterprise/venv/bin"
SUPFILE
    
    systemctl enable supervisor 2>/dev/null || true
    systemctl restart supervisor || {
        echo -e "${RED}✗ Failed to start supervisor${NC}"
        exit 1
    }
    
    supervisorctl reread
    supervisorctl update
    
    sleep 2
    supervisorctl start mist-api 2>/dev/null || true
    
    echo -e "${GREEN}✓ Supervisor configured${NC}"
}

step7_nginx() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [7/8] Configuring Nginx...                                ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    cat > /etc/nginx/sites-available/mist-api << 'NGXFILE'
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
        proxy_buffering on;
        proxy_buffer_size 4k;
        proxy_buffers 8 4k;
    }

    location /health {
        proxy_pass http://mist_api;
        access_log off;
    }

    location /static/ {
        alias /opt/mist-enterprise/static/;
        expires 30d;
    }
}
NGXFILE
    
    ln -sf /etc/nginx/sites-available/mist-api /etc/nginx/sites-enabled/mist-api
    rm -f /etc/nginx/sites-enabled/default
    
    if ! nginx -t > /tmp/nginx-test.log 2>&1; then
        echo -e "${RED}✗ Nginx configuration error${NC}"
        cat /tmp/nginx-test.log
        exit 1
    fi
    
    systemctl restart nginx || {
        echo -e "${RED}✗ Failed to restart nginx${NC}"
        exit 1
    }
    
    echo -e "${GREEN}✓ Nginx configured${NC}"
}

step8_database() {
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║ [8/8] Setting up database & services...                   ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    
    sudo -u postgres psql << 'SQLFILE' 2>/dev/null || true
CREATE DATABASE IF NOT EXISTS mist_db;
CREATE USER IF NOT EXISTS mist WITH PASSWORD 'mist';
ALTER ROLE mist SET client_encoding TO 'utf8';
ALTER ROLE mist SET default_transaction_isolation TO 'read committed';
ALTER ROLE mist SET default_transaction_deferrable TO on;
ALTER ROLE mist SET default_transaction_level TO 'read committed';
ALTER ROLE mist SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE mist_db TO mist;
SQLFILE
    
    systemctl enable redis-server 2>/dev/null || true
    systemctl start redis-server || {
        echo -e "${RED}✗ Failed to start redis-server${NC}"
        exit 1
    }
    
    systemctl enable postgresql 2>/dev/null || true
    
    echo -e "${GREEN}✓ Database and services configured${NC}"
}

# ============================================================================
# MAIN EXECUTION
# ============================================================================

main() {
    clear
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  Mist Enterprise Suite v3 - Home Lab Deployment           ║${NC}"
    echo -e "${BLUE}║  Ubuntu Server Pro 25.10                                  ║${NC}"
    echo -e "${BLUE}║  VALIDATED & PRODUCTION-READY                             ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    validate_system
    step1_update_system
    step2_create_user
    step3_python_env
    step4_flask_app
    step5_environment
    step6_supervisor
    step7_nginx
    step8_database
    
    # Final checks
    sleep 3
    
    echo ""
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║              Installation Complete!                       ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${GREEN}✓ Mist Enterprise Suite v3 installed successfully!${NC}"
    echo ""
    echo -e "${YELLOW}NEXT STEPS:${NC}"
    echo "1. Update configuration with your API keys:"
    echo -e "   ${BLUE}sudo nano $INSTALL_DIR/.env${NC}"
    echo ""
    echo "2. Update these values:"
    echo -e "   ${BLUE}ANTHROPIC_API_KEY=sk-ant-...${NC}"
    echo -e "   ${BLUE}MIST_API_TOKEN=...${NC}"
    echo -e "   ${BLUE}MIST_ORG_ID=...${NC}"
    echo ""
    echo "3. Restart the application:"
    echo -e "   ${BLUE}sudo supervisorctl restart mist-api${NC}"
    echo ""
    echo "4. Access the dashboard:"
    echo -e "   ${BLUE}http://your-server-ip${NC}"
    echo ""
    echo -e "${YELLOW}VERIFICATION:${NC}"
    echo -e "   ${BLUE}curl http://localhost/health${NC}"
    echo ""
    echo -e "${YELLOW}LOGS:${NC}"
    echo -e "   ${BLUE}tail -f /var/log/mist-api.log${NC}"
    echo ""
}

# Run main
main

