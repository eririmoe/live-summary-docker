"""Fresh live recordings take priority at resumable history checkpoints."""
import time
import core


class HistoryYield(Exception):
    pass


def next_live(db,now):
    return db.execute("SELECT * FROM jobs WHERE status IN ('queued','retry') AND retry_at<=? "
        "AND COALESCE(json_extract(payload,'$.origin'),'live')!='baidu_history' ORDER BY created DESC LIMIT 1",(now,)).fetchone()


def history_checkpoint():
    with core.database() as db:
        pending=next_live(db,time.time())
    if pending:
        raise HistoryYield()
