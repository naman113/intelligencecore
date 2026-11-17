# Build the SPA, then serve it from nginx. Two stages so the runtime image
# carries no node_modules and no toolchain — it is static files plus a proxy.

FROM node:24-alpine AS build
WORKDIR /app

# Dependencies first, so a source-only change reuses this layer. `npm ci` is
# deliberate: it installs exactly the lockfile, the same way `uv sync --frozen`
# does on the backend, so every client VM gets identical bytes.
COPY package.json package-lock.json ./
RUN npm ci --no-fund --no-audit

COPY . .
RUN npm run build

FROM nginx:alpine
# nginx.conf is an envsubst template (Quick 043). The defaults reproduce Docker
# compose exactly; Railway sets IC_GATEWAY and IC_RESOLVER_IPV6=on. The resolver
# address is read from the container's own resolv.conf by 15-local-resolvers.envsh.
ENV IC_GATEWAY=http://app:8080 IC_RESOLVER_IPV6=off NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER=^(IC_|NGINX_LOCAL_RESOLVERS$)
# If resolv.conf names no nameserver (15-local-resolvers.envsh then exports an empty value and nginx
# refuses to start), fall back to Docker's embedded DNS: the value this image always used.
RUN printf '%s\n' '[ -n "${NGINX_LOCAL_RESOLVERS:-}" ] || export NGINX_LOCAL_RESOLVERS=127.0.0.11' \
    > /docker-entrypoint.d/16-resolver-fallback.envsh && chmod +x /docker-entrypoint.d/16-resolver-fallback.envsh
# A failed render must fail closed, not serve the stock page.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
