# The console is a static bundle, and this image serves it as one.
#
# It used to run `npm run dev --host 0.0.0.0` as root: Vite's development
# server — source files, a live-reload socket and a history of file-read
# advisories when exposed — published by docker compose on every host
# interface. Nothing about the console needs a server that can do more than
# hand out files it was built with.

FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Baked in at build time, as a static bundle's configuration has to be.
# docker-compose.yml passes it as a build argument from the same variable the
# development server used to read at runtime.
ARG react_PUBLIC_API_BASE_URL=http://localhost:8000
ENV react_PUBLIC_API_BASE_URL=${react_PUBLIC_API_BASE_URL}
RUN npm run build

# Unprivileged nginx: runs as uid 101 and listens on 8080, so the container
# needs no root and no capability to bind a low port.
FROM nginxinc/nginx-unprivileged:1.29-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
