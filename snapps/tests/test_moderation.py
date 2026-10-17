"""Host remove (posts and files), room header + pins, and file-drop limits / resumable uploads."""
import hashlib, json, os, unittest, urllib.request
from pathlib import Path
from helpers import F
from typed import testbed as T, core as CORE
from typed.hub import sign_upload
from typed.sync import SyncError, check_tomb, http_json, verify_record


def disk_has(root, needle: bytes) -> list:
    hits = []
    for p in Path(root).rglob("*"):
        if p.is_file() and needle in p.read_bytes():
            hits.append(str(p))
    return hits


class Remove(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = T.start(hub_override={k: {"new_key_rate": [[1000, 3600]], "rate": [[1000, 3600]]}
                                        for k in ("room", "drop")})
        cls.a, cls.b = T.new_core(cls.env, "alice"), T.new_core(cls.env, "bob")
        cls.a.sync(); cls.b.sync()

    @classmethod
    def tearDownClass(cls):
        T.stop(cls.env)

    def post(self, core, coll, t, body):
        r = core.draft(coll, t, body, [])["result"]
        self.assertTrue(r.get("ok"), r)
        return r

    def test_remove_board_post_everywhere(self):
        h = self.env["board"]
        marker = "SECRET-" + os.urandom(6).hex()
        p = self.post(self.a, "board", "board.post", {"text": marker, "reply_to": None})
        keep = self.post(self.a, "board", "board.post", {"text": "harmless", "reply_to": None})
        h.push()
        self.b.sync("board")
        mirror_dir = self.env["tmp"] / "board-mirror"
        self.assertTrue(disk_has(mirror_dir, marker.encode()))
        self.assertTrue(disk_has(self.b.dir, marker.encode()))
        env = h.moderate("remove", "post", p["id"], "illegal content")
        self.assertEqual(disk_has(self.env["tmp"] / "board", marker.encode()), [])  # host disk
        self.assertEqual(h.push()[h.mirrors[0]]["ok"], True)
        self.assertEqual(disk_has(mirror_dir, marker.encode()), [])                 # mirror
        self.assertIsNone(self.b.sync("board")["board"]["error"])
        self.assertEqual(disk_has(self.b.dir, marker.encode()), [])                 # client
        # the chain still verifies for a brand-new client syncing from zero (tombstone path)
        c = T.new_core(self.env, "carol-" + os.urandom(3).hex())
        self.assertIsNone(c.sync("board")["board"]["error"])
        self.assertEqual(c.colls["board"].store.get(p["id"])["tomb"]["by"], "remove")
        self.assertIsNotNone(c.colls["board"].store.get(keep["id"]).get("env"))
        # shown as a placeholder; no override brings it back
        self.b.prefs["revealed"].append(p["id"])
        self.b.prefs["moderation"] = "ignore"
        try:
            v = self.b.view("board")
        finally:
            self.b.prefs["moderation"] = "honour"
        self.assertFalse(any(x["id"] == p["id"] for x in v["view"]["posts"]))
        self.assertIn({"id": p["id"], "seq": v["removed"][0]["seq"], "reason": "illegal content"},
                      v["removed"])
        self.assertNotIn(marker, json.dumps(v))
        self.assertEqual(env["mod"]["action"], "remove")

    def test_forged_and_non_host_removes_rejected(self):
        h, board = self.env["board"], "board"
        host_pub = F.pub_hex(h.key)
        pid = self.post(self.a, "board", "board.post", {"text": "x", "reply_to": None})["id"]
        evil = F.new_key()
        forged = F.make_mod(evil, board, "remove", "post", pid, "forged")
        for tomb in ({"by": "remove", "record": forged},
                     {"by": "remove", "record": F.make_mod(h.key, board, "hide", "post", pid, "")},
                     {"by": "remove", "record": F.make_mod(h.key, board, "remove", "post",
                                                           "a" * 26, "other post")},
                     {"by": "remove"}):
            with self.assertRaises((SyncError, F.FormatError)):
                check_tomb(pid, tomb, board, host_pub)
        good = {"by": "remove", "record": F.make_mod(h.key, board, "remove", "post", pid, "ok")}
        with self.assertRaises(SyncError):
            check_tomb(pid, good, board, None)  # never accepted without the delegated host key
        self.assertEqual(check_tomb(pid, good, board, host_pub)["by"], "remove")
        # a record signed by a non-host key is rejected
        with self.assertRaises(F.FormatError):
            verify_record({"rseq": 1, "kind": "mod", "rec": forged,
                           "rid": F.record_id(F.MOD_DOMAIN, forged["mod"])}, host_pub, board)
        # and nobody can push a mod record through the public endpoints
        code, _ = http_json(self.env["board_url"] + "/v1/submit", forged)
        self.assertEqual(code, 400)
        # a tampered sig on a real host record fails too
        bad = json.loads(json.dumps(good["record"])); bad["mod"]["reason"] = "edited"
        with self.assertRaises(F.FormatError):
            check_tomb(pid, {"by": "remove", "record": bad}, board, host_pub)
        # remove can't target an author
        with self.assertRaises(F.FormatError):
            F.make_mod(h.key, board, "remove", "author", host_pub, "")
        self.assertIsNotNone(self.b.sync("board"))

    def test_remove_file_deletes_blob(self):
        data = b"FILEBYTES-" + os.urandom(8).hex().encode()
        r = self.a.upload("files", data, "f.txt", "text/plain", "", mode="plain")["result"]
        self.assertTrue(r["ok"], r)
        sha = hashlib.sha256(data).hexdigest()
        self.b.sync("files")
        self.assertEqual(self.b.blob("files", sha), data)  # bob now caches it
        node = self.env["hub"].nodes["files"]
        node.moderate("remove", "post", r["id"], "copyright")
        self.assertIsNone(node.blobs.get(sha))
        self.assertEqual(disk_has(self.env["tmp"] / "hub" / "files", data), [])
        self.b.sync("files")
        self.assertFalse((Path(self.b.dir) / "blobs" / sha).exists())
        self.assertIsNone(self.b.blob("files", sha))
        self.assertFalse(any(i["id"] == r["id"] for i in self.b.view("files")["view"]["items"]))
        self.assertEqual(self.b.view("files")["removed"][-1]["reason"], "copyright")

    def test_hide_unchanged(self):
        r = self.post(self.a, "lobby", "room.msg", {"text": "hide me"})
        self.env["hub"].nodes["lobby"].moderate("hide", "post", r["id"], "spam")
        self.b.sync("lobby")
        self.assertIsNotNone(self.b.colls["lobby"].store.get(r["id"]).get("env"))
        self.b.prefs["revealed"].append(r["id"])
        self.assertTrue(any(l["id"] == r["id"] for l in self.b.view("lobby")["view"]["lines"]))

    def test_admin_cli_remove(self):
        from typed import admin
        r = self.post(self.a, "lobby", "room.msg", {"text": "CLI-REMOVE-ME"})
        admin.main(["--data", str(self.env["tmp"] / "hub"), "--coll", "lobby",
                    "remove", "post", r["id"], "--reason", "doxxing"])
        self.assertEqual(disk_has(self.env["tmp"] / "hub" / "lobby", b"CLI-REMOVE-ME"), [])
        self.assertIsNone(self.b.sync("lobby")["lobby"]["error"])
        self.assertIn("doxxing", [x["reason"] for x in self.b.view("lobby")["removed"]])

    # ---- header + pins ----
    def test_header_and_pins(self):
        from typed import admin
        node = self.env["hub"].nodes["lobby"]
        admin.main(["--data", str(self.env["tmp"] / "hub"), "--coll", "lobby", "header",
                    "--description", "The SRNet demo lobby", "--rules", "Be kind. No spam."])
        node._policy = None  # same process: drop the cached copy (a separate CLI run uses mtime)
        self.assertIsNone(self.b.sync("lobby")["lobby"]["error"])
        v = self.b.view("lobby")
        self.assertEqual(v["header"], {"description": "The SRNet demo lobby",
                                       "rules": "Be kind. No spam."})
        ids = [self.post(self.a, "lobby", "room.msg", {"text": f"pin {i}"})["id"] for i in range(4)]
        for old in node.pins():
            node.moderate("unpin", "post", old, "")
        for i in ids[:3]:
            node.moderate("pin", "post", i, "")
        with self.assertRaises(F.FormatError):
            node.moderate("pin", "post", ids[3], "")  # at most 3
        node.moderate("unpin", "post", ids[0], "")
        node.moderate("pin", "post", ids[3], "")
        self.b.sync("lobby")
        self.assertEqual([p["id"] for p in self.b.view("lobby")["pinned"]], ids[1:])
        with self.assertRaises(F.FormatError):
            F.make_mod(node.key, "lobby", "pin", "author", F.pub_hex(node.key), "")


class FileLimits(unittest.TestCase):
    LIM, QUOTA = 3 * 1024 * 1024, 5 * 1024 * 1024

    @classmethod
    def setUpClass(cls):
        cls.env = T.start(mirror=False, hub_override={"drop": {
            "max_file_bytes": cls.LIM, "quota_bytes": cls.QUOTA,
            "new_key_rate": [[1000, 3600]], "rate": [[1000, 3600]]}})
        cls.a = T.new_core(cls.env, "alice")
        cls.a.sync()
        cls.url = cls.env["hub_url"] + "/c/files"
        cls.blobs = cls.env["hub"].nodes["files"].blobs

    @classmethod
    def tearDownClass(cls):
        T.stop(cls.env)

    def test_advertised(self):
        _, info = http_json(self.url + "/v1/info")
        self.assertEqual((info["max_file_bytes"], info["quota_bytes"]), (self.LIM, self.QUOTA))
        self.assertEqual(self.a.max_file_bytes(self.a.colls["files"]), self.LIM)

    def test_client_refuses_before_upload(self):
        r = self.a.upload("files", b"x" * (self.LIM + 1), "big.bin", "application/octet-stream")
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])
        self.assertEqual(os.listdir(self.blobs.up), [])

    def test_host_enforces_limit(self):
        k = F.new_key()
        code, r = http_json(self.url + "/v1/upload", sign_upload(k, "files", "ab" * 32, self.blobs.max_bytes + 1))
        self.assertEqual(code, 413)
        code, r = http_json(self.url + "/v1/upload", sign_upload(k, "other", "ab" * 32, 10))
        self.assertEqual(code, 400)  # signed for another collection
        bad = sign_upload(k, "files", "ab" * 32, 10); bad["upload"]["size"] = 11
        self.assertEqual(http_json(self.url + "/v1/upload", bad)[0], 400)  # bad signature
        req = urllib.request.Request(self.url + "/v1/blob", data=b"x" * 10)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)  # unsigned single-shot refused while a quota is set
        self.assertEqual(e.exception.code, 403)
        # a drop.item claiming more than the limit is refused by the host rules
        r = self.a.draft("files", "drop.item", {"sha256": "cd" * 32, "size": self.LIM + 1,
                                                "name": "x", "mime": "text/plain", "mode": "plain"}, [])
        self.assertFalse(r["result"]["ok"])

    def test_quota_and_resume(self):
        u = T.new_core(self.env, "uploader")
        u.sync()
        data = os.urandom(int(2.5 * 1024 * 1024))
        sha = hashlib.sha256(data).hexdigest()
        real, sent, fail = CORE.urllib.request.urlopen, [], [True]

        def flaky(req, *a, **kw):
            url = getattr(req, "full_url", req)
            if "/v1/upload/" in url:
                sent.append(url)
                if len(sent) == 2 and fail[0]:
                    fail[0] = False
                    raise OSError("connection dropped")
            return real(req, *a, **kw)
        CORE.urllib.request.urlopen = flaky
        try:
            r = u.upload("files", data, "big.bin", "application/octet-stream", mode="plain")
        finally:
            CORE.urllib.request.urlopen = real
        self.assertFalse(r["ok"])  # interrupted after one chunk
        _, st = http_json(f"{self.url}/v1/upload/{sha}")
        self.assertEqual(st["have"], 1 << 20)
        sent.clear()
        CORE.urllib.request.urlopen = flaky
        try:
            r = u.upload("files", data, "big.bin", "application/octet-stream", mode="plain")
        finally:
            CORE.urllib.request.urlopen = real
        self.assertTrue(r["result"]["ok"], r)
        self.assertEqual(len(sent), 2)  # resumed at 1 MiB: only the last two chunks were sent
        self.assertIn(f"off={1 << 20}", sent[0])
        self.assertEqual(self.blobs.get(sha), data)
        self.assertEqual(self.blobs.used(u.me), len(data))
        # quota: 2.5 MiB used of 5 MiB, a second 2.9 MiB file is refused
        with self.assertRaises(Exception) as e:
            u._put_blob(u.colls["files"], os.urandom(int(2.9 * 1024 * 1024)))
        self.assertIn("quota", str(e.exception))
        # removing the file frees the quota
        self.env["hub"].nodes["files"].moderate("remove", "post", r["result"]["id"], "test")
        self.assertEqual(self.blobs.used(u.me), 0)

    def test_encrypted_big_file_roundtrip(self):
        data = os.urandom(2 * 1024 * 1024 + 7)
        r = self.a.upload("files", data, "e.bin", "application/octet-stream", mode="gated")
        self.assertTrue(r["result"]["ok"], r)
        b = T.new_core(self.env, "a2", key_seed=None)
        self.assertEqual(self.a.open_file("files", r["result"]["id"])[0], data)


if __name__ == "__main__":
    unittest.main()
