FROM python:3.13-slim AS build
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY open_scion ./open_scion
RUN python -m pip install --no-cache-dir --no-deps --prefix=/install .

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /install /usr/local
RUN groupadd --gid 10001 scion \
    && useradd --uid 10001 --gid scion --no-create-home scion \
    && mkdir -p /var/lib/open-scion \
    && chown scion:scion /var/lib/open-scion
USER 10001:10001
VOLUME ["/var/lib/open-scion"]
EXPOSE 7433
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7433/ready', timeout=3).read()"]
ENTRYPOINT ["scion", "serve", "--data", "/var/lib/open-scion", "--host", "0.0.0.0"]
CMD ["--port", "7433", "--max-workers", "16", "--access-log"]
