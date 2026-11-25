ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm
FROM golang:1.26.10-bookworm AS core-build
WORKDIR /src
RUN curl -fsSL 'https://codeload.github.com/SagerNet/sing-box/tar.gz/c005f35300ce76427dc285481ec5695f5b179cd2' -o /tmp/sing-box.tar.gz \
    && echo '1c4d2b3bf261a8d98e1b026be0bbcddbcb3f53bbca77eb15e8f25f7d4e602e59  /tmp/sing-box.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/sing-box.tar.gz --strip-components=1
RUN CGO_ENABLED=0 go build -mod=readonly -buildvcs=false -trimpath \
      -tags with_utls,with_v2ray_api -ldflags '-s -w -X github.com/sagernet/sing-box/constant.Version=1.14.4' \
      -o /out/sing-box ./cmd/sing-box
COPY traffic_query.go ./cmd/yhl-traffic/main.go
RUN CGO_ENABLED=0 go build -mod=readonly -buildvcs=false -trimpath \
      -tags with_v2ray_api -ldflags '-s -w' -o /out/yhl-traffic ./cmd/yhl-traffic

FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 YHL_DATA=/data
WORKDIR /opt/yhl-ui
RUN install -d -m 0700 -o 10001 -g 10001 /data
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: -r requirements.txt
COPY --from=core-build /out/sing-box /out/yhl-traffic /usr/local/bin/
COPY --from=core-build /src/LICENSE /usr/share/doc/sing-box/LICENSE
COPY app.py core.py routing_rules.py storage.py style.css admin.js ./
COPY public/ ./public/
USER 10001:10001
ENTRYPOINT ["python", "app.py"]
CMD ["serve"]
