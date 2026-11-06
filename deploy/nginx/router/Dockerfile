FROM nginx:1.27-alpine

COPY deploy/nginx/router/default.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx/router/default.conf /etc/nginx/router-seed.conf
COPY deploy/nginx/router/router-entrypoint.sh /usr/local/bin/router-entrypoint.sh
RUN chmod +x /usr/local/bin/router-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/router-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
