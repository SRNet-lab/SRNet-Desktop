# srnet-msgboard protocol v1 (phase 0)

Canonical bytes: `DOMAIN + "\n" + json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)` (UTF-8).
Domains: `srnet-msgboard-post/v1`, `-checkpoint/v1`, `-delegation/v1`, `-pow/v1`, `-chain/v1` (never shared with the exit board).

## Post envelope
```json
{"post": {"v":1, "board":"demo", "author":"<hex Ed25519 author key>", "ts":1792000000,
          "body":"<=500 chars, NFC, no control chars except \\n \\t",
          "reply_to":"<post id>|null", "pow":{"bits":16,"salt":"…"}},
 "sig": "<hex Ed25519 over canonical(post domain, post)>", "id": "<post id>"}
```
- Exactly these fields; anything else is refused. Author key is a dedicated signing key, separate from the Session Router/Noise transport key.
- `id` = z-base32 of the first 16 bytes of SHA-256(canonical signed bytes). Same post = same id on every host.
- PoW: SHA-256(canonical(pow domain, post)) has ≥ `pow.bits` leading zero bits, and `pow.bits` ≥ host policy.
- Host accepts `ts` within −600 s / +60 s of its clock (replay window); an already-stored id is idempotent (`dup:true`); `board` must match (cross-board replay refused).
- Test vectors: `tests/vectors/posts-v1.json` (regenerate with `scripts/gen_vectors.py`).

## Log, chain, checkpoint
- `link_n = SHA-256("srnet-msgboard-chain/v1\n" + link_{n-1} + "\n" + n + "\n" + id_n)`, `link_0 = 64×"0"`.
- Checkpoint `{v, board, host, seq, head, issued, expires}` signed by the host key; re-signed at most every 60 s or on new entries; valid 3 h.
- Delegation `{v, board, root, host, issued, expires, policy}` signed by the board's (offline) root key. Clients pin only the root key.
- Client rules: verify delegation → checkpoint (sig, expiry, not from future) → no rollback (seq < pinned) / no equivocation (same seq, other head) → each entry's sig, id, seq continuity and link → final link == checkpoint head. Only a fully verified segment is committed; otherwise fail over to the next host.

## HTTP (JSON only, `nosniff`, CSP `default-src 'none'; sandbox`)
`GET /v1/info | /v1/delegation | /v1/checkpoint | /v1/log?since=N&limit≤500 | /v1/post/<id> | /v1/thread/<id>`, `POST /v1/submit` (≤16 KB).
Mirrors serve the same GET API with the primary's own signed checkpoint and delegation, so readers verify them identically.

## Phase 1 additions
- Checkpoint gains `policy` (SHA-256 of canonical(`srnet-msgboard-policy/v1`, policy)) and `rseq` (record count).
- Records (`GET /v1/records?since=N`): `mod` = host-signed `{v,board,host,action:hide|unhide|remove|pin|unpin,target_type:post|author,target,reason,ts}` (`remove`, `pin`, `unpin` take `target_type:post` only)
  (domain `srnet-msgboard-mod/v1`); `delete` = author-signed `{v,board,author,targets[≤50],ts}` (domain `srnet-msgboard-delete/v1`). `rid` = first 32 hex of SHA-256 of canonical bytes.
- Tombstone entries `{seq,id,link,tomb:{by:"delete",record}|{by:"retention"}}`; links cover ids only, so chains still verify.
- `POST /v1/delete`; `POST /v1/publish` (mirror only: `{delegation, checkpoint, policy, entries, records}`).
- New keys (first post < `new_key_age_s`) need `new_key_pow_bits` and obey `new_key_rate`.

## Typed platform

## Encrypted file drops (`drop.*`)

Files are encrypted on the uploader's computer by default. The host stores and serves only
ciphertext, addressed by its SHA-256.

**Ciphertext format `srf1`.** `"SRF1" | u32 padded_len` header, then the padded plaintext in
64 KiB chunks, each sealed with ChaCha20-Poly1305 under a fresh random 32-byte key per file
(per version). Chunk nonce = 11-byte big-endian chunk index + 1 byte (`1` on the last chunk),
AAD = the 8-byte header, so chunks can't be reordered, dropped, truncated or extended.
Plaintext = `u32 meta_len | meta | file | zero padding`, `meta = {"name","mime","size"}`, padded
to the next power of two (at least 4 KiB). The host learns only the size bucket. The largest file is the drop policy's
`max_file_bytes` (default 25 MiB; protocol ceiling 256 MiB; a host with no value is treated as 4 MiB).

**Encryption keys.** Each client derives an X25519 key from its author seed
(HKDF-SHA256, info `srnet-typed/x25519/v1`) and publishes the public half as `enc_key` in its
`id.profile`. A key wrap is `{"to": enc_key, "epk": ephemeral X25519, "ct": sealed file key}`:
ECDH(epk, to) → HKDF-SHA256 (info `srnet-typed/file-wrap/v1` ‖ epk ‖ to) → ChaCha20-Poly1305
(zero nonce, single-use key) with AAD = ciphertext SHA-256 ‖ to. A wrap can't be moved to
another file or recipient.

**Records** (all in a `drop` collection):

| type | body | refs | rule |
|---|---|---|---|
| `drop.item@1` | `sha256, size, mode, wraps?, note?` (+ `name, mime` only when `mode=plain`) | `[previous version]?` | `mode` absent = `plain` (older records keep verifying) |
| `drop.grant@1` | `sha256, wrap` | `[item]` | counts only if signed by the item's author and `sha256` matches |
| `drop.revoke@1` | `to` | `[item]` | item author only |
| `drop.request@1` | `enc_key, note?` | `[item]` | anyone |

**Modes.**
- `gated` (default): the file key is wrapped to the owner and to each chosen recipient.
  Recipients can be named by enc key or by author key (resolved through their profile's
  `enc_key`). The owner can add people later with `drop.grant`, or approve a `drop.request`,
  which makes the owner's client wrap the key to the requester.
- `link`: also wrapped to the owner; the owner's client can produce
  `http://<host .sesh>/v1/blob/<sha256>#k=<base64url key>`. The key is in the URL fragment,
  which browsers and this client never send to the host.
- `plain`: explicit opt-in for public, indexable content. Not encrypted; `name`/`mime` in clear.

**Revocation is forward-only.** `drop.revoke` stops the owner's client from wrapping *new
versions* (a `drop.item` whose `refs[0]` is the previous version, encrypted under a new key) to
that recipient. It can't take back a file someone already downloaded, or a key they already
unwrapped.

Templates never see keys, wraps or links: the core strips them from views, decrypts files
itself, and shows links and access lists only in its own trusted sheets.

## Payments: SRNet balance (`shop.*`)

Every paid action across SRNet (exits, store items, and later paid files and rooms) uses one
flow: the core or CLI shows its own confirm prompt, then calls
`SRNetBalance.startLease(vendor_id, price, 1)` on the live contract. One prepaid hour begins at
once, so the seller can claim the whole amount immediately and the payment is final. The 3%
fee was taken when the buyer topped up; nothing more is charged. Each payment is capped at
20 USDC (the contract's `maxPerLease`).

- `shop.listing@1`: `currency` is `SRNET` (default) with `vendor_id` (bytes32) and `price`
  (USDC, at most 6 decimals, > 0 and ≤ 20). The legacy `ETH`/`USDC` direct form
  (`chain_id`, `pay_to`) is testnet-only.
- `shop.order@1`: `{lease_id, buyer, note?}`, refs `[listing]`. The buyer's receipt; the seller's
  client checks it on-chain (`leaseInfo`/`leaseExtra`: vendor id, amount, buyer) before shipping.
- Sellers bind a vendor id once with `configureVendor(vendorId, salt)`; `srnet vendor --shop-key K`
  derives `salt = keccak("srnet-shop/v1" ‖ seller key)`. The seller's signature on the listing ties
  the store key to that vendor id.

## Host remove (moderation)

`mod` with `action:"remove"` and `target_type:"post"`. The host stores the record and replaces
the entry with a tombstone `{"by":"remove","record":<the signed mod record>}` in the same step.
Chain links cover ids only, so `seq`/`link`/checkpoint verification is unchanged. A syncing
peer accepts a `remove` tombstone only if the record verifies against the host key from the
pinned delegation and its `target` is that entry's id. When a peer sees the record it redacts
its own copy (SQLite `secure_delete` plus a WAL checkpoint, so the bytes leave the disk). For a
`drop.item`, the host deletes the blob, and clients delete their cached copy. A remove can't be
undone or overridden. Clients show it as "Removed by host: <reason>". Hide/unhide are
unchanged.

## Room header and pins

The policy (whose hash the host signs into every checkpoint) carries `description` (500 chars
max) and `rules` (1000 chars max). Rooms and boards show them at the top. `mod` `pin`/`unpin`
records pin a post; the active set is computed in record order, at most 3 (the oldest pin drops
out), and a remove unpins. Hosts refuse a 4th pin.

## Drop limits and resumable uploads

Drop policy: `max_file_bytes`, `quota_bytes` (0 = none), also shown in `/v1/info`.
- `POST /v1/upload` `{"upload":{v,coll,author,sha256,size,ts},"sig"}` signed by the uploader
  (domain `srnet-typed/upload/v1`) answers `{have, chunk, done}`; it refuses with 413 over the
  limit and 403 over the uploader's quota.
- `POST /v1/upload/<sha256>?off=N` with up to 1 MiB of raw bytes; `off` must equal `have`
  (409 returns the right `have`). After the last byte the hash is checked and the blob stored.
- `GET /v1/upload/<sha256>` returns `{have}` to resume.
- The old `POST /v1/blob` single upload still works when no quota is set.
