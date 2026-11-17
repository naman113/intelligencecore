# The dashboards origin: nginx serving the vendored chart library and proxying
# /d/ to the gateway. No toolchain. vendor.json and README.md stay out of the image.

FROM nginx:alpine
# nginx.conf is an envsubst template. The defaults reproduce Phase 4 (listen :80,
# gateway app:8080, resolver from resolv.conf); docker-compose.vm.yml overrides the first two.
ENV IC_LISTEN=80 IC_GATEWAY=http://app:8080 IC_RESOLVER_IPV6=off NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER=^(IC_|NGINX_LOCAL_RESOLVERS$)
# If resolv.conf names no nameserver (15-local-resolvers.envsh then exports an empty value and nginx
# refuses to start), fall back to Docker's embedded DNS: the value this image always used.
RUN printf '%s\n' '[ -n "${NGINX_LOCAL_RESOLVERS:-}" ] || export NGINX_LOCAL_RESOLVERS=127.0.0.11' \
    > /docker-entrypoint.d/16-resolver-fallback.envsh && chmod +x /docker-entrypoint.d/16-resolver-fallback.envsh
# A failed render must fail closed, not serve the stock page.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY vendor/ /usr/share/nginx/d_lib/

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -q --spider http://127.0.0.1/d/_lib/chart.js-4.5.1/chart.umd.min.js || exit 1
