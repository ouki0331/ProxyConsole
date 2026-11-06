FROM mitmproxy/mitmproxy:latest

WORKDIR /app/recorder
RUN mkdir -p /var/lib/proxyconsole/captures /var/lib/proxyconsole/blobs && \
    chmod -R 0777 /var/lib/proxyconsole

COPY services/recorder/parser.py /app/recorder/parser.py
COPY services/recorder/addon.py /app/recorder/addon.py
COPY services/recorder/capture_cli.py /app/recorder/capture_cli.py
COPY var/certificates/edge.crt /etc/proxyconsole/certs/edge.crt
