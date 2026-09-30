FROM python:3.12-slim
RUN pip install --no-cache-dir browser-buddy-mcp==0.1.1
CMD ["browser-buddy-mcp"]
