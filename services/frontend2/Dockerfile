FROM node:22-alpine AS deps
WORKDIR /app
COPY services/frontend2/package.json services/frontend2/package-lock.json ./
RUN npm install --legacy-peer-deps

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY services/frontend2/ ./
RUN npm run build

FROM alpine:3.22 AS runner
WORKDIR /app
RUN apk add --no-cache nodejs npm
ENV NODE_ENV=production
ENV PORT=3002
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.ts ./next.config.ts
COPY --from=builder /app/public ./public
EXPOSE 3002
CMD ["npm", "run", "start"]
