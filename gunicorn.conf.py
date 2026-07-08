import os
import threading

from app import refresh_offering_cache_loop

bind = f"0.0.0.0:{os.environ.get('PORT', 10000)}"
workers = 1
worker_class = "sync"
timeout = 30


def post_fork(server, worker):
    # Background offering-cache refresh, started per-worker after fork
    # (not at module import time) — same fix that stabilized gx-schedule.
    threading.Thread(target=refresh_offering_cache_loop, daemon=True).start()
