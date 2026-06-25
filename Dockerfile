# ===== build stage =====
FROM node:20-bookworm-slim AS builder

WORKDIR /app
COPY package*.json ./
# --ignore-scripts: skip the postinstall hook (it runs scripts/copy-cesium.mjs,
# which isn't in the image yet). `npm run build` below copies Cesium's assets.
RUN npm ci --ignore-scripts

COPY . .

# Baked in at build time (next build), NOT read at runtime. Passed via --build-arg.
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_WS_URL
ARG NEXT_PUBLIC_OWM_API_KEY
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
ENV NEXT_PUBLIC_OWM_API_KEY=$NEXT_PUBLIC_OWM_API_KEY

RUN npm run build

# ===== runtime stage =====
FROM node:20-bookworm-slim

WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

EXPOSE 3000
CMD ["npm", "run", "start", "--", "-p", "3000"]
