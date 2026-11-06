FROM nginx:1.27-alpine

COPY deploy/nginx/edge/default.conf /etc/nginx/conf.d/default.conf
COPY var/certificates/edge.crt /etc/nginx/certs/edge.crt
COPY var/certificates/edge.key /etc/nginx/certs/edge.key
