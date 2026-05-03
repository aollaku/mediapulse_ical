FROM ubuntu:latest

WORKDIR /app

RUN apt update && apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-full \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN python3 -m venv /app/.venv
RUN /app/.venv/bin/pip install --upgrade pip
RUN /app/.venv/bin/pip install -r requirements.txt

COPY ./ ./


CMD ["/app/.venv/bin/python", "app.py"]