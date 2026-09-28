ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 iproute2 ndisc6 tzdata

# Pass the container environment (TZ from the Supervisor) on to CMD
ENV S6_KEEP_ENV=1

COPY rootfs /
RUN chmod a+x /usr/bin/thread_route_guard.py

CMD ["python3", "-u", "/usr/bin/thread_route_guard.py"]
