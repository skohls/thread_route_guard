ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 iproute2 ndisc6 tzdata

COPY rootfs /
RUN chmod a+x /usr/bin/thread_route_guard.py

CMD ["python3", "-u", "/usr/bin/thread_route_guard.py"]
