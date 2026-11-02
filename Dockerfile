FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "streamfix serve --host 0.0.0.0 --port ${PORT}"]
