FROM mitmproxy/mitmproxy:latest

WORKDIR /app/recorder
RUN mkdir -p /var/lib/proxyconsole/captures \
             /var/lib/proxyconsole/blobs \
             /var/lib/proxyconsole/database \
             /app/migrations && \
    chown -R mitmproxy:mitmproxy /var/lib/proxyconsole /app && \
    chmod -R 0777 /var/lib/proxyconsole

COPY migrations/001_capture_index.sql /app/migrations/001_capture_index.sql
COPY services/recorder/parser.py /app/recorder/parser.py
COPY services/recorder/index_store.py /app/recorder/index_store.py
COPY services/recorder/addon.py /app/recorder/addon.py
COPY services/recorder/capture_cli.py /app/recorder/capture_cli.py
COPY services/recorder/api_server.py /app/recorder/api_server.py
COPY var/certificates/edge.crt /etc/proxyconsole/certs/edge.crt
