# syntax=docker/dockerfile:1

FROM node:26.13.0-slim AS build

WORKDIR /app

RUN npm install -g pnpm@12.17.0

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# Production runtime is caddy
FROM caddy:2-alpine

COPY --from=build /app/build /srv
