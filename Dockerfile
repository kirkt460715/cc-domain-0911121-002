ARG PRODUCT_RUNTIME=mcr.microsoft.com/playwright/python:v1.56.0-noble
ARG NODE_RUNTIME=node:22.23.0-bookworm-slim
FROM $NODE_RUNTIME AS node
FROM $PRODUCT_RUNTIME
USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN python -m pip show playwright >/dev/null 2>&1 || python -m pip install --no-cache-dir playwright==1.56.0
WORKDIR /app
COPY . /app
ENTRYPOINT []
CMD ["sh", "demo_support/verify.sh"]
