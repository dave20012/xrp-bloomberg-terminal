FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

ENV STREAMLIT_SERVER_PORT=8080

CMD streamlit run main.py --server.port $PORT --server.address 0.0.0.0
