FROM docker:28-cli

COPY deploy/watchdog/self_heal.sh /usr/local/bin/self-heal
# Git on Windows may check scripts out with CRLF; normalize inside the Linux image.
RUN sed -i 's/\r$//' /usr/local/bin/self-heal && chmod 0555 /usr/local/bin/self-heal

HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD test -f /tmp/watchdog-heartbeat && test "$(( $(date +%s) - $(stat -c %Y /tmp/watchdog-heartbeat) ))" -lt 60

ENTRYPOINT ["/usr/local/bin/self-heal"]
