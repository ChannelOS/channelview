#!/bin/bash
# ChannelView Production Deploy Script — Cycle 36+
# Location on server: /opt/channelview/deploy.sh
# This script pulls latest code from GitHub and deploys to Docker container

set -e
echo "=========================================="
echo "  ChannelView Deployment"
echo "  $(date)"
echo "=========================================="

REPO_DIR="/opt/channelview-repo"
CONTAINER="channelview-app"

# Step 1: Pull latest from GitHub
echo ""
echo "[1/4] Pulling latest code from GitHub..."
cd "$REPO_DIR"
git pull origin main
echo "  Done."

# Step 2: Copy core application files
echo ""
echo "[2/4] Copying core files to container..."
docker cp app.py $CONTAINER:/app/app.py
docker cp database.py $CONTAINER:/app/database.py
docker cp voice_service.py $CONTAINER:/app/voice_service.py
docker cp static/js/app.js $CONTAINER:/app/static/js/app.js
docker cp static/css/app.css $CONTAINER:/app/static/css/app.css
docker cp templates/app.html $CONTAINER:/app/templates/app.html
echo "  Core files copied."

# Step 3: Copy additional files (templates, services, static assets)
echo ""
echo "[3/4] Copying additional files..."

# Templates (candidate-facing pages)
for tmpl in candidate_interview.html candidate_format_choice.html; do
    if [ -f "templates/$tmpl" ]; then
        docker cp "templates/$tmpl" $CONTAINER:/app/templates/$tmpl
        echo "  + templates/$tmpl"
    fi
done

# Services
for svc in email_service.py seed_rsc_defaults.py; do
    if [ -f "$svc" ]; then
        docker cp "$svc" $CONTAINER:/app/$svc
        echo "  + $svc"
    fi
done

# Static intro templates
if [ -d "static/intros" ]; then
    docker cp static/intros $CONTAINER:/app/static/intros
    echo "  + static/intros/ (directory)"
fi

echo "  Done."

# Step 4: Restart containers
echo ""
echo "[4/4] Restarting containers..."
docker restart $CONTAINER
echo "  Container restarted."

# Verify file sizes
echo ""
echo "=========================================="
echo "  Deployment Verification"
echo "=========================================="
echo "File sizes inside container:"
docker exec $CONTAINER sh -c 'wc -c /app/app.py /app/database.py /app/voice_service.py /app/static/js/app.js /app/static/css/app.css /app/templates/app.html 2>/dev/null || true'
echo ""
echo "Deployment complete! Check https://mychannelview.com"
echo "=========================================="
