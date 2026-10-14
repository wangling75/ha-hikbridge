"""hikbridge - Hikvision/EZVIZ PTZ + intercom bridge over HCNetSDK."""

__version__ = "0.2.22"

#: Result of the start-up timezone detection, surfaced through /health so the
#: add-on log time can be checked without digging through container logs.
timezone_status = "not attempted"
