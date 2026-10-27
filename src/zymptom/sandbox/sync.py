"""Manual mailbox exchange through the authoritative Python merge service."""

from zymptom.sync.service import SyncService


def sync_disposable(session, mailbox):
    journal = mailbox.connect()
    provider = f"firebase-sandbox:{journal}"
    service = SyncService(session.episodes, before_merge=session.before_destructive_change)
    pending = service.pending(provider)
    for change in pending.changes:
        mailbox.upload(change)
        service.acknowledge(provider, [change.change_id])
    incoming = mailbox.pull()
    result = service.apply(incoming, source_provider=provider)
    return result, len(service.unresolved_conflicts())
