# GoldenTrinity — Fly.io Infrastructure
# This deploys TimescaleDB, Redis, and n8n as separate Fly apps.
# The 4 Python services run as a single combined app (services-app).
#
# Free tier gives you:
#   - 3 shared VMs always-on
#   - 3GB persistent storage total
#   - Sufficient for all 4 containers + 4 Python services
#
# Deploy order:
#   1. timescaledb-app  (database first)
#   2. redis-app
#   3. n8n-app
#   4. services-app     (Python services last — they need the above running)

# ── App names (change gt- prefix if taken) ────────────────────────────────
TIMESCALEDB_APP=gt-timescaledb
REDIS_APP=gt-redis
N8N_APP=gt-n8n
SERVICES_APP=gt-services
