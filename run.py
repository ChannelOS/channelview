#!/usr/bin/env python3
"""
ChannelView - Startup Script
Run this to start the application: python3 run.py
"""
from database import init_db
from app import app

if __name__ == '__main__':
    print("\nð¥ ChannelView - Async Video Interview Platform")
    print("=" * 48)
    init_db()
    print("â Database initialized")
    print("â Starting server on http://localhost:5000")
    print("=" * 48)
    print("Open http://localhost:5000 in your browser to get started\n")
    app.run(host='0.0.0.0', port=5000, debug=True)
