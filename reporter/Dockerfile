FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
    libffi-dev shared-mime-info fonts-liberation cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY report.py queries.py entrypoint.sh ./
COPY templates/ templates/
RUN chmod +x entrypoint.sh

# Monthly: 1st of month at 07:30 UTC
# Quarterly: 1st of Jan/Apr/Jul/Oct at 08:00 UTC
RUN echo "30 7 1 * * root . /etc/environment; cd /app && python report.py --month auto >> /var/log/reporter.log 2>&1" \
    > /etc/cron.d/reporter \
 && echo "0 8 1 1,4,7,10 * root . /etc/environment; cd /app && python report.py --quarter auto >> /var/log/reporter.log 2>&1" \
    >> /etc/cron.d/reporter \
 && chmod 0644 /etc/cron.d/reporter

CMD ["/app/entrypoint.sh"]
