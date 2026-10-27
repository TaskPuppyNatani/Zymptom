"""Provider-neutral local synchronization; no transport or network integration."""

from .contract import Change, SyncBatch, SyncError

__all__ = ["Change", "DeviceIdentity", "SyncBatch", "SyncError"]


def __getattr__(name):
    # The transport/contract can be imported on platforms where the existing
    # desktop identity lock implementation is unavailable.
    if name == "DeviceIdentity":
        from .identity import DeviceIdentity
        return DeviceIdentity
    raise AttributeError(name)
