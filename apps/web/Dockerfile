# Contexto de build: raiz do repo (não ./apps/web), porque este app faz
# parte de um workspace npm e depende de packages/contracts + do
# package-lock.json compartilhado. Ver docker-compose.yml.
FROM node:22-alpine AS build
WORKDIR /repo

COPY package.json package-lock.json* ./
COPY apps/web/package.json apps/web/package.json
COPY packages/contracts/package.json packages/contracts/package.json
RUN npm ci

COPY apps/web apps/web
COPY packages/contracts packages/contracts

# O Next inlina `NEXT_PUBLIC_*` no bundle em build time. Sem receber isso como
# argumento, a imagem congelaria a porta default e continuaria apontando para
# ela mesmo depois de `API_PORT` mudar no `.env`.
ARG NEXT_PUBLIC_API_URL=http://localhost:8001
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build -w apps/web

FROM node:22-alpine AS runner
WORKDIR /repo
ENV NODE_ENV=production
COPY --from=build /repo/node_modules ./node_modules
COPY --from=build /repo/package.json ./package.json
COPY --from=build /repo/apps/web ./apps/web
COPY --from=build /repo/packages/contracts ./packages/contracts

WORKDIR /repo/apps/web
EXPOSE 3000
CMD ["npm", "run", "start"]
