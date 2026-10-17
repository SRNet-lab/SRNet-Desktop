# SRNet mail: wire format and host API

Code: `snapps/typed/mail.py` (format), `mailhost.py` (host), `mailclient.py` (client),
`mailcli.py` (`srnet mail`). Tests: `snapps/tests/test_mail.py`.

```

Address card (shared as a srnet://mail link):
  {"ed": <Ed25519 identity hex>, "x": <X25519 hex>, "hosts": [1..3 mail host URLs], "ts": int}
  sig = Ed25519(ed) over canonical("srnet-mail/addr/v1", card).  The link may also carry
  `name=` (a *suggested* petname; unsigned, never trusted).

Message (inner, signed by the sender, then sealed):
  {"v":1,"from","from_x","from_hosts","to","msg_id","ts","subject","body","attach":[links],
   "reply_to"} + "sig" over canonical("srnet-mail/msg/v1", inner).
  Plaintext = u32 len | JSON | zero pad to a power-of-two bucket (1 KiB .. 64 KiB).

Seal ("SRM1"): MAGIC | epk(32) | nonce(12) | ChaCha20-Poly1305(ct+tag)
  key = HKDF-SHA256(X25519(e, recipient_x), salt=MAGIC, info="srnet-mail/seal/v1"+epk+rx)
  AAD = MAGIC + rx + msg_id, so a host can't relabel a blob or move it to another inbox.
  The host sees: recipient, msg_id, padded size, time.  Never the sender or the content.

Contact tag (host-side spam sorting without revealing the sender):
  tag = HMAC-SHA256(X25519(sender_x, recipient_x), "srnet-mail/tag/v1|" + to + "|" + host_id)[:16]
  The recipient uploads the tags of accepted contacts; mail with a matching tag lands in
  "inbox", everything else in the small "requests" pile.  Tags differ per host.


The host stores opaque sealed blobs per recipient (Ed25519 identity), deletes them when the
recipient acks them after saving (fetch, then ack), and purges expired ones.  It never sees content or senders.

  GET  /v1/mail/info                       host id, limits, optional delegation
  POST /v1/mail/put        {to,msg_id,blob,ttl?,tag?,stamp?}
  POST /v1/mail/challenge  {to}            -> {nonce}   (single use, 120 s)
  POST /v1/mail/auth       {to,op,nonce,args,sig}   op: register | list | fetch | ack | tags

Piles: mail whose contact tag matches one the recipient uploaded goes to "inbox"; everything
else to "requests" (smaller size limit, fewer messages, rate-limited per recipient).
`stamp` is accepted and passed to Host.stamp_check (a no-op hook for a future paid stamp).
```

## Auth

Every inbox operation (`register`, `list`, `fetch`, `ack`, `tags`) takes a fresh nonce from
`/v1/mail/challenge` (single use, 120 s) and an Ed25519 signature by the inbox key over
`canonical("srnet-mail/auth/v1", {host, to, op, nonce, args: sha256(args)})`. `host` is
the mail host's own key, so a signature can't be replayed on another host. Clients pin
the host key on first use.

## Fetch, then ack

`fetch` returns sealed blobs but keeps them. The client writes them to disk, then sends `ack`
with the ids it saved (including ones it rejected); only `ack` deletes. Unacked mail stays until
its TTL. A crash between fetch and ack just means the next fetch sees duplicates, which the
client drops by msg id.

## Defaults

| Setting | Value |
|---|---|
| TTL | 7 days default, 30 days max (longer is clamped) |
| Sealed size | 1 KiB to 64 KiB buckets (power of two) |
| Inbox pile | 256 messages / 8 MiB per recipient |
| Requests pile | 32 messages, 16 KiB each, 20 per hour per recipient |
| Inboxes per host | 1000, open registration (`--closed` to stop new ones) |
| Host disk | 1 GiB total |
| Purge sweep | every 60 s |
| Mail hosts per address | 1 to 3 |

## Not in v1

Global names / `.sesh` naming, paid stamps (the `stamp` field and `Host.stamp_check` hook are
there), push notifications, read receipts, group mail. Hosts still see recipient, size
bucket and timing.
