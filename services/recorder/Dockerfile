FROM mitmproxy/mitmproxy:latest

WORKDIR /app/recorder
RUN mkdir -p /var/lib/proxyconsole/captures \
             /var/lib/proxyconsole/blobs \
             /var/lib/proxyconsole/database \
             /app/migrations && \
    chown -R mitmproxy:mitmproxy /var/lib/proxyconsole /app && \
    chmod -R 0777 /var/lib/proxyconsole

COPY migrations/001_capture_index.sql /app/migrations/001_capture_index.sql
COPY migrations/002_retention_policy.sql /app/migrations/002_retention_policy.sql
COPY migrations/003_routes.sql /app/migrations/003_routes.sql
COPY migrations/004_discovery.sql /app/migrations/004_discovery.sql
COPY migrations/005_request_drafts.sql /app/migrations/005_request_drafts.sql
COPY services/recorder/parser.py /app/recorder/parser.py
COPY services/recorder/retention.py /app/recorder/retention.py
COPY services/recorder/docker_discovery.py /app/recorder/docker_discovery.py
COPY services/recorder/route_manager.py /app/recorder/route_manager.py
COPY services/recorder/draft_replay.py /app/recorder/draft_replay.py
COPY services/recorder/index_store.py /app/recorder/index_store.py
COPY services/recorder/addon.py /app/recorder/addon.py
COPY services/recorder/capture_cli.py /app/recorder/capture_cli.py
COPY services/recorder/api_server.py /app/recorder/api_server.py
COPY web/console /app/web/console
COPY var/certificates/edge.crt /etc/proxyconsole/certs/edge.crt
