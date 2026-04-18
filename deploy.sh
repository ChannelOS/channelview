#!/bin/bash
# ChannelView Production Deploy Script — Cycle 37+ (48.1: landing-page sync)
# Location on server: /opt/channelview/deploy.sh
# This script pulls latest code from GitHub and deploys to Docker container

set -e
echo "=========================================="
echo "  ChannelView Deployment"
echo "  $(date)"
echo "=========================================="

REPO_DIR="/opt/channelview-repo"
CONTAINER="channelview-app"
CAREERS_DIR="/opt/channelview/careers-landing"

# Step 1: Pull latest from GitHub
echo ""
echo "[1/5] Pulling latest code from GitHub..."
cd "$REPO_DIR"
git pull origin main
echo "  Done."

# Step 2: Copy core application files
echo ""
echo "[2/5] Copying core files to container..."
docker cp app.py $CONTAINER:/app/app.py
docker cp database.py $CONTAINER:/app/database.py
docker cp voice_service.py $CONTAINER:/app/voice_service.py
docker cp static/js/app.js $CONTAINER:/app/static/js/app.js
docker cp static/css/app.css $CONTAINER:/app/static/css/app.css
docker cp templates/app.html $CONTAINER:/app/templates/app.html
echo "  Core files copied."

# Step 3: Copy additional files (templates, services, static assets)
echo ""
echo "[3/5] Copying additional files..."

# Templates (candidate-facing pages)
for tmpl in candidate_interview.html candidate_format_choice.html candidate_done.html candidate_error.html; do
    if [ -f "templates/$tmpl" ]; then
        docker cp "templates/$tmpl" $CONTAINER:/app/templates/$tmpl
        echo "  + templates/$tmpl"
    fi
done

# Services
for svc in email_service.py seed_rsc_defaults.py resume_service.py sms_service.py; do
    if [ -f "$svc" ]; then
        docker cp "$svc" $CONTAINER:/app/$svc
        echo "  + $svc"
    fi
done

# Static intro templates (copy individual files to avoid docker cp nesting bug)
if [ -d "static/intros" ]; then
    docker exec $CONTAINER mkdir -p /app/static/intros
    for introfile in static/intros/*; do
        if [ -f "$introfile" ]; then
            docker cp "$introfile" $CONTAINER:/app/static/intros/
            echo "  + $introfile"
        fi
    done
fi

echo "  Done."

# Step 4: Sync channelcareers.io landing page (nginx bind-mount, no container restart)
echo ""
echo "[4/5] Syncing channelcareers.io landing page..."
if [ -d "$REPO_DIR/channelcareers-landing" ]; then
    mkdir -p "$CAREERS_DIR/assets"
    # Copy index + any top-level files
    for f in "$REPO_DIR/channelcareers-landing"/*; do
        if [ -f "$f" ]; then
            cp "$f" "$CAREERS_DIR/$(basename "$f")"
            echo "  + $(basename "$f")"
        fi
    done
    # Copy assets (logos, images, css)
    if [ -d "$REPO_DIR/channelcareers-landing/assets" ]; then
        for a in "$REPO_DIR/channelcareers-landing/assets"/*; do
            if [ -f "$a" ]; then
                cp "$a" "$CAREERS_DIR/assets/$(basename "$a")"
                echo "  + assets/$(basename "$a")"
            fi
        done
    fi
    echo "  Landing page synced to $CAREERS_DIR"
else
    echo "  (no channelcareers-landing/ in repo — skipping)"
fi

# Step 5: Restart containers
echo ""
echo "[5/5] Restarting containers..."
docker restart $CONTAINER
echo "  Container restarted."

# Verify file sizes
echo ""
echo "=========================================="
echo "  Deployment Verif