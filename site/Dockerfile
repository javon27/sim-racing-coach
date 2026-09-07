# Nestor marketing site — static files served by Caddy.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY index.html /srv/index.html
COPY assets /srv/assets
# Caddy reads $PORT at runtime (see Caddyfile); no EXPOSE needed for Railway.
