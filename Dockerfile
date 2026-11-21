ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS download
COPY fetch_core.py /tmp/fetch_core.py
RUN python /tmp/fetch_core.py

FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 YHL_DATA=/data
WORKDIR /opt/yhl-ui
RUN install -d -m 0700 -o 10001 -g 10001 /data
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: -r requirements.txt
COPY --from=download /tmp/sing-box /usr/local/bin/sing-box
COPY --from=download /tmp/sing-box.LICENSE /usr/share/doc/sing-box/LICENSE
COPY app.py core.py storage.py style.css ./
COPY public/ ./public/
USER 10001:10001
ENTRYPOINT ["python", "app.py"]
CMD ["serve"]
