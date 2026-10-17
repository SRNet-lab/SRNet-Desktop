# vendor

Copies from
[SRNet-VPSHost-ExitPack](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack), which
is the source of truth:

```
listing.py, watcher.py, evm.py, keccak256.py   from seller/
lease_flow.py, admission.py            from contrib/exit-lease/
boardfmt.py                            from board/
seshdns.py                             from seller/
```

The client uses them to verify listings and the board, build claim messages and order the
connect flow. Update them by copying from upstream; don't edit them here.
Apache-2.0.
claim_receiver.py                      from seller/ (feat/claim-delivery), used by tests only
