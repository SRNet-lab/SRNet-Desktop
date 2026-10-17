"""Compatibility shim: verification and sync now live in typed/sync.py."""
from typed.sync import (SyncError, accept_batch, apply_record, check_tomb, http_json,  # noqa
                        submit, sync_once, verify_record, verify_records, verify_segment)
