FROM python:3.9-slim
LABEL org.opencontainers.image.source="https://github.com/${GITHUB_REPOSITORY}"

WORKDIR /app

# Copy requirements and install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application code
COPY labs-operator.py .

ENTRYPOINT ["kopf", "run", "--verbose", "labs-operator.py"]
