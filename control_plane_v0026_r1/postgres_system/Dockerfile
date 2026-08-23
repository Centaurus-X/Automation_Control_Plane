FROM postgres:18.4-bookworm

LABEL org.opencontainers.image.title="automation-postgres" \
      org.opencontainers.image.version="0.14.0"

COPY scripts/container-entrypoint.sh /usr/local/bin/automation-postgres-entrypoint
RUN chmod 0755 /usr/local/bin/automation-postgres-entrypoint

ENTRYPOINT ["automation-postgres-entrypoint"]
CMD ["postgres"]
