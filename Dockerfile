FROM node:22-slim AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE run.py ./
COPY cat_fleet_chat ./cat_fleet_chat
RUN rm -rf ./cat_fleet_chat/static
COPY --from=web-build /web/dist ./cat_fleet_chat/static
# EXTRAS is empty for the hub. The Discord relay service builds with "[discord]".
ARG EXTRAS=""
RUN pip install --no-cache-dir ".${EXTRAS}"

ENV CAT_FLEET_HOST=0.0.0.0
ENV CAT_FLEET_PORT=8787
EXPOSE 8787
CMD ["python", "run.py"]
