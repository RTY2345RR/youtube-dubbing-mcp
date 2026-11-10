# YouTube dubbing MCP server (stdio). ffmpeg comes from the imageio-ffmpeg wheel.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DUBBING_OUTPUT_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /data
VOLUME ["/data"]
LABEL io.modelcontextprotocol.server.name="io.github.RTY2345RR/youtube-dubbing-mcp"
ENTRYPOINT ["youtube-dubbing-mcp"]
