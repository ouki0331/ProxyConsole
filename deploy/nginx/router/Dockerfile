FROM nginx:1.27-alpine

RUN mkdir -p /var/lib/proxyconsole/router && chmod -R 0777 /var/lib/proxyconsole

COPY deploy/nginx/router/default.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx/router/default.conf /etc/nginx/router-seed.conf
COPY deploy/nginx/router/router-entrypoint.sh /usr/local/bin/router-entrypoint.sh
RUN chmod +x /usr/local/bin/router-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/router-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
