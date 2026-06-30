FROM node:22-alpine AS dev
WORKDIR /app
COPY services/frontend2/package.json services/frontend2/package-lock.json ./
RUN npm install --legacy-peer-deps
EXPOSE 3001
CMD ["npm", "run", "dev"]

FROM node:22-alpine AS deps
WORKDIR /app
COPY services/frontend2/package.json services/frontend2/package-lock.json ./
RUN npm install --legacy-peer-deps

FROM node:22-alpine AS builder
WORKDIR /app
# Single knob for the web base path, baked in at build time (default "" = web
# root). Override with --build-arg NEXT_PUBLIC_BASE_PATH=/app/v2 and update the
# matching nginx `location` to serve under a sub-path. Consumed by next.config.ts
# and src/lib/route-paths.ts.
ARG NEXT_PUBLIC_BASE_PATH=""
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH
# Display timezone for all Intl.DateTimeFormat calls in the client bundle.
ARG NEXT_PUBLIC_TZ="UTC"
ENV NEXT_PUBLIC_TZ=$NEXT_PUBLIC_TZ
COPY --from=deps /app/node_modules ./node_modules
COPY services/frontend2/ ./
RUN npm run build

FROM alpine:3.22 AS runner
WORKDIR /app
RUN apk add --no-cache nodejs npm
ENV NODE_ENV=production
ENV PORT=3001
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.ts ./next.config.ts
COPY --from=builder /app/public ./public
EXPOSE 3001
CMD ["npm", "run", "start"]
