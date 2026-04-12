"""
ChannelView - Gunicorn Production Configuration
"""
import multiprocessing
import os

# Server socket
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Worker processes
# Rule of thumb: 2 * CPU cores + 1
# For video upload app, keep workers low to manage memory
workers = int(os.environ.get('WEB_CONCURRENCY', min(multiprocessing.cpu_count() * 2 + 1, 4)))
worker_class = 'sync'
worker_connections = 1000

# Timeouts
timeout = 120          # Allow long video uploads
graceful_timeout = 30
keepalive = 5

# Request limits
limit_request_line = 8190
limit_request_fields = 100

# Logging
accesslog = '-'        # stdout
errorlog = '-'         # stderr
loglevel = os.environ.get('LOG_LEVEL', 'info').lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'channelview'

# Server hooks
def on_starting(server):
    """Initialize database on startup."""
    pass

def post_fork(server, worker):
    """After worker fork - each worker gets its own DB connection."""
    server.log.info(f"Worker spawned (pid: {worker.pid})")

def worker_exit(server, worker):
    """Worker cleanup."""
    server.log.info(f"Worker exiting (pid: {worker.pid})")
