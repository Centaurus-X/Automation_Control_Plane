# -*- coding: utf-8 -*-

"""Bounded retention maintenance for high-volume Master journal tables."""

from functools import partial


def cleanup_history(database, **kwargs):
    """Delete bounded expired heartbeat and completed message history."""
    heartbeat_days = min(365, max(1, int(kwargs.get("heartbeat_days", 7))))
    message_days = min(3650, max(1, int(kwargs.get("message_days", 30))))
    batch_size = min(10000, max(100, int(kwargs.get("batch_size", 2000))))

    def write(connection):
        heartbeats = connection.execute(
            "DELETE FROM master_control.node_heartbeats WHERE heartbeat_id IN ("
            "SELECT heartbeat_id FROM master_control.node_heartbeats "
            "WHERE received_at<clock_timestamp()-(%s*interval '1 day') "
            "ORDER BY heartbeat_id LIMIT %s)",
            (heartbeat_days, batch_size),
        ).rowcount
        inbox = connection.execute(
            "DELETE FROM master_control.inbox WHERE (site_id,source_service,source_instance_id,message_id) IN ("
            "SELECT site_id,source_service,source_instance_id,message_id FROM master_control.inbox "
            "WHERE state IN ('COMPLETED','REJECTED') "
            "AND completed_at<clock_timestamp()-(%s*interval '1 day') "
            "ORDER BY completed_at LIMIT %s)",
            (message_days, batch_size),
        ).rowcount
        outbox = connection.execute(
            "DELETE FROM master_control.outbox WHERE message_id IN ("
            "SELECT message_id FROM master_control.outbox "
            "WHERE state IN ('ACKED','FAILED','CANCELLED') "
            "AND updated_at<clock_timestamp()-(%s*interval '1 day') "
            "ORDER BY updated_at LIMIT %s)",
            (message_days, batch_size),
        ).rowcount
        return {"heartbeats": heartbeats, "inbox": inbox, "outbox": outbox}

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_maintenance_capabilities(database):
    return {"cleanup": partial(cleanup_history, database)}
