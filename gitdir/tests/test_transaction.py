#!/usr/bin/python3
"""
Crash-window and contract tests for the per-target transaction engine.

These tests use local file:// sources (no network) and inject failures at the
exact durability boundaries described in the implementation plan.
"""

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from gitdir import transaction as tx


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


FILES = {
    "a.txt": b"alpha content\n",
    "sub/b.txt": b"beta beta beta\n" * 100,
    "sub/deep/c.txt": b"c" * 5000,
    "top.dat": b"\x00\x01\x02\x03" * 250,
}


def build_sources(base):
    """Materialise FILES under base and return matching manifest entries."""
    entries = []
    for rel, data in sorted(FILES.items()):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        entries.append({
            "path": rel,
            "url": "file://" + path,
            "git_sha": git_blob_sha(data),
            "size": len(data),
        })
    return entries


class CountingFetcher(object):
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, url):
        self.calls.append(url)
        if self.fail_on and self.fail_on in url:
            raise RuntimeError("simulated source failure")
        return open(url[len("file://"):], "rb")


def walk_files(root):
    out = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(current, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as handle:
                out[rel] = handle.read()
    return out


class TransactionTestBase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="gitdir-tx-test-")
        self.src = os.path.join(self.work, "src")
        os.makedirs(self.src)
        self.entries = build_sources(self.src)
        self.target = os.path.join(self.work, "downloaded", "thing")
        self.staging = tx.staging_path(self.target)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def assertTreePublished(self):
        self.assertTrue(os.path.isdir(self.target))
        published = walk_files(self.target)
        self.assertEqual(set(published), set(FILES))
        for rel, data in FILES.items():
            self.assertEqual(published[rel], data, rel)
        self.assertFalse(os.path.exists(self.staging))


class FreshPublishTests(TransactionTestBase):
    def test_fresh_run_publishes_and_cleans_staging(self):
        fetcher = CountingFetcher()
        result = tx.run(self.target, self.entries, fetcher)
        self.assertEqual(result, "published")
        self.assertTreePublished()

    def test_staging_is_sibling_of_target(self):
        tx.run(self.target, self.entries, CountingFetcher())
        self.assertEqual(os.path.dirname(self.staging), os.path.dirname(self.target))
        self.assertTrue(os.path.basename(self.staging).startswith(tx.STAGING_PREFIX))

    def test_existing_target_is_never_overwritten(self):
        os.makedirs(self.target)
        with open(os.path.join(self.target, "mine.txt"), "wb") as handle:
            handle.write(b"untouched")
        with self.assertRaises(tx.TargetExistsError):
            tx.run(self.target, self.entries, CountingFetcher())
        self.assertTrue(os.path.exists(os.path.join(self.target, "mine.txt")))
        self.assertFalse(os.path.exists(self.staging))

    def test_checksum_mismatch_aborts_and_keeps_staging(self):
        bad_entries = [dict(e) for e in self.entries]
        bad_entries[0] = dict(bad_entries[0], git_sha="0" * 40)
        with self.assertRaises(tx.VerificationError):
            tx.run(self.target, bad_entries, CountingFetcher())
        self.assertFalse(os.path.exists(self.target))
        self.assertTrue(os.path.isdir(self.staging))

    def test_rename_with_retry_refuses_existing_destination(self):
        src = os.path.join(self.work, "srcdir")
        dst = os.path.join(self.work, "dstdir")
        os.makedirs(src)
        os.makedirs(dst)
        with self.assertRaises(tx.TargetExistsError):
            tx._rename_with_retry(src, dst)
        self.assertTrue(os.path.isdir(src))
        self.assertTrue(os.path.isdir(dst))


class CrashWindowTests(TransactionTestBase):
    def _interrupt_after_first_file(self):
        fail_url = self.entries[1]["url"]
        fetcher = CountingFetcher(fail_on=fail_url)
        with self.assertRaises(RuntimeError):
            tx.run(self.target, self.entries, fetcher, lock_timeout=5)
        self.assertFalse(os.path.exists(self.target))
        self.assertTrue(os.path.isdir(self.staging))
        return fetcher

    def test_report_recovers_status_readonly(self):
        self._interrupt_after_first_file()
        status = tx.report(self.target, requested_entries=self.entries)
        self.assertEqual(status["stage"], tx.STAGE_ACTIVE)
        self.assertTrue(status["manifest_matches"])
        self.assertEqual(status["files_done"], 1)
        self.assertEqual(status["expected_files"], len(self.entries))

    def test_resume_completes_interrupted_download_without_refetching(self):
        first = self._interrupt_after_first_file()
        # Drop an unplanned stray file into the staged tree; resume must sweep it.
        stray = os.path.join(self.staging, "root", "stray.tmp")
        os.makedirs(os.path.dirname(stray), exist_ok=True)
        with open(stray, "wb") as handle:
            handle.write(b"partial junk")
        second = CountingFetcher()
        result = tx.resume(self.target, self.entries, second, lock_timeout=5)
        self.assertEqual(result, "resumed")
        self.assertTreePublished()
        # Already verified file is not re-fetched; failed file is.
        self.assertNotIn(self.entries[0]["url"], second.calls)
        self.assertIn(self.entries[1]["url"], second.calls)
        self.assertGreaterEqual(len(first.calls), 2)

    def test_crash_after_prepared_before_rename(self):
        with mock.patch.object(tx, "_rename_noreplace",
                               side_effect=RuntimeError("crash at rename")):
            with self.assertRaises(RuntimeError):
                tx.run(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        status = tx.report(self.target, requested_entries=self.entries)
        self.assertEqual(status["stage"], tx.STAGE_PREPARED)
        self.assertTrue(status["root_exists"])
        self.assertFalse(os.path.exists(self.target))
        result = tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "resumed")
        self.assertTreePublished()

    def test_crash_between_rename_and_commit_record(self):
        original_append = tx.Journal.append

        def crash_on_committed(self, payload):
            if payload.get("t") == "COMMITTED":
                raise RuntimeError("crash right after rename")
            return original_append(self, payload)

        with mock.patch.object(tx.Journal, "append", crash_on_committed):
            with self.assertRaises(RuntimeError):
                tx.run(self.target, self.entries, CountingFetcher(), lock_timeout=5)

        # Rename is durable, COMMITTED was never written.
        self.assertTrue(os.path.isdir(self.target))
        self.assertTrue(os.path.isdir(self.staging))
        status = tx.report(self.target, requested_entries=self.entries)
        self.assertEqual(status["stage"], tx.STAGE_PREPARED)
        result = tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "recovered-committed")
        self.assertTreePublished()

    def test_crash_during_cleanup_after_cleaned_record(self):
        with mock.patch.object(tx.shutil, "rmtree",
                               side_effect=RuntimeError("crash during cleanup")):
            with self.assertRaises(RuntimeError):
                tx.run(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertTrue(os.path.isdir(self.target))
        self.assertTrue(os.path.isdir(self.staging))
        result = tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "recovered-committed")
        self.assertTreePublished()

    def test_post_rename_verify_failure_is_reported_not_overwritten(self):
        original_append = tx.Journal.append

        def crash_on_committed(self, payload):
            if payload.get("t") == "COMMITTED":
                raise RuntimeError("crash right after rename")
            return original_append(self, payload)

        with mock.patch.object(tx.Journal, "append", crash_on_committed):
            with self.assertRaises(RuntimeError):
                tx.run(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        # External actor mutated the final tree before recovery: it must be
        # reported and left untouched.
        with open(os.path.join(self.target, "intruder.txt"), "wb") as handle:
            handle.write(b"not part of the plan")
        with self.assertRaises(tx.InconsistentTransactionError):
            tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertTrue(os.path.exists(os.path.join(self.target, "intruder.txt")))
        self.assertTrue(os.path.isdir(self.staging))


class JournalIntegrityTests(TransactionTestBase):
    def _journal_path(self):
        return os.path.join(self.staging, tx.JOURNAL_NAME)

    def test_torn_tail_is_tolerated(self):
        fail_url = self.entries[2]["url"]
        with self.assertRaises(RuntimeError):
            tx.run(self.target, self.entries, CountingFetcher(fail_on=fail_url),
                   lock_timeout=5)
        with open(self._journal_path(), "ab") as handle:
            handle.write(b"eyJ0IjoiRklMTSJ9.brokenpartial")
        status = tx.report(self.target, requested_entries=self.entries)
        self.assertTrue(status["torn_tail"])
        result = tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "resumed")
        self.assertTreePublished()

    def test_corrupt_history_is_refused_by_all_mutating_actions(self):
        fail_url = self.entries[1]["url"]
        with self.assertRaises(RuntimeError):
            tx.run(self.target, self.entries, CountingFetcher(fail_on=fail_url),
                   lock_timeout=5)
        path = self._journal_path()
        with open(path, "rb") as handle:
            lines = handle.read().split(b"\n")
        # Tamper with the BEGIN line checksum (a non-tail record).
        body, check = lines[0].rsplit(b".", 1)
        lines[0] = body + b"." + (b"f" if check[:1] != b"f" else b"e") + check[1:]
        with open(path, "wb") as handle:
            handle.write(b"\n".join(lines))

        status = tx.report(self.target, requested_entries=self.entries)
        self.assertEqual(status["stage"], tx.STAGE_CORRUPT)
        with self.assertRaises(tx.CorruptJournalError):
            tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        with self.assertRaises(tx.CorruptJournalError):
            tx.cleanup(self.target, self.entries, lock_timeout=5)
        self.assertTrue(os.path.isdir(self.staging))


class ManifestGateTests(TransactionTestBase):
    def setUp(self):
        super(ManifestGateTests, self).setUp()
        fail_url = self.entries[1]["url"]
        with self.assertRaises(RuntimeError):
            tx.run(self.target, self.entries, CountingFetcher(fail_on=fail_url),
                   lock_timeout=5)

    def test_stale_manifest_resume_is_rejected(self):
        changed = [dict(e) for e in self.entries]
        changed[0] = dict(changed[0], url=changed[0]["url"] + "?different=1")
        status = tx.report(self.target, requested_entries=changed)
        self.assertFalse(status["manifest_matches"])
        with self.assertRaises(tx.ManifestMismatchError):
            tx.resume(self.target, changed, CountingFetcher(), lock_timeout=5)
        with self.assertRaises(tx.ManifestMismatchError):
            tx.cleanup(self.target, changed, lock_timeout=5)
        # Staging stays exactly where the operator can inspect it.
        self.assertTrue(os.path.isdir(self.staging))
        self.assertFalse(os.path.exists(self.target))

    def test_matching_manifest_resume_still_works_after_rejection(self):
        changed = [dict(e) for e in self.entries]
        changed[0] = dict(changed[0], url=changed[0]["url"] + "?different=1")
        with self.assertRaises(tx.ManifestMismatchError):
            tx.resume(self.target, changed, CountingFetcher(), lock_timeout=5)
        result = tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "resumed")
        self.assertTreePublished()

    def test_cleanup_then_fresh_run(self):
        result = tx.cleanup(self.target, self.entries, lock_timeout=5)
        self.assertEqual(result, "cleaned")
        self.assertFalse(os.path.exists(self.staging))
        self.assertFalse(os.path.exists(self.target))
        result = tx.run(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "published")
        self.assertTreePublished()


class SameFilesystemTests(TransactionTestBase):
    def test_different_st_dev_aborts_publish(self):
        real_stat = os.stat
        parent = os.path.dirname(self.target)

        def fake_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if os.path.abspath(str(path)) == os.path.abspath(parent):
                values = list(result)  # st_dev is field index 2
                values[2] = result.st_dev + 9999
                return os.stat_result(values)
            return result

        with mock.patch.object(tx.os, "stat", side_effect=fake_stat):
            with self.assertRaises(tx.SameFilesystemError):
                tx.run(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertFalse(os.path.exists(self.target))
        self.assertTrue(os.path.isdir(self.staging))
        # Once the condition is gone, resume publishes successfully.
        result = tx.resume(self.target, self.entries, CountingFetcher(), lock_timeout=5)
        self.assertEqual(result, "resumed")
        self.assertTreePublished()


class ConcurrencyTests(TransactionTestBase):
    def test_second_process_joins_completed_publish(self):
        gate = threading.Event()
        release = threading.Event()

        def blocking_fetcher(url):
            if not gate.is_set():
                gate.set()
                release.wait(timeout=10)
            return open(url[len("file://"):], "rb")

        results = []

        def first():
            results.append(tx.run(self.target, self.entries, blocking_fetcher,
                                  lock_timeout=15))

        def second():
            results.append(tx.run(self.target, self.entries, CountingFetcher(),
                                  lock_timeout=15))

        t1 = threading.Thread(target=first)
        t1.start()
        self.assertTrue(gate.wait(timeout=10))
        t2 = threading.Thread(target=second)
        t2.start()
        # Give t2 time to block on the lock held by t1, then let t1 finish.
        threading.Event().wait(0.5)
        release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(sorted(results), ["joined", "published"])
        self.assertTreePublished()

    def test_crashed_holder_is_continued_by_follower(self):
        fail_url = self.entries[1]["url"]
        with self.assertRaises(RuntimeError):
            tx.run(self.target, self.entries, CountingFetcher(fail_on=fail_url),
                   lock_timeout=5)
        follower = CountingFetcher()
        result = tx.run(self.target, self.entries, follower, lock_timeout=5)
        self.assertEqual(result, "joined")
        self.assertTreePublished()


class PathNormalizationTests(unittest.TestCase):
    def test_dotfiles_survive_and_traversal_is_rejected(self):
        entries = [{
            "path": "./.config/keep", "url": "file:///x", "git_sha": None,
            "size": None,
        }]
        normalized = tx.normalize_entries(entries)
        self.assertEqual(normalized[0]["path"], ".config/keep")
        self.assertEqual(tx.manifest_id(entries), tx.manifest_id(normalized))
        for evil in ("../escape", "a/../../b", "/abs"):
            with self.assertRaises(tx.TransactionError):
                tx.normalize_entries([{"path": evil, "url": "u"}])


class DiscoverTests(TransactionTestBase):
    def test_discover_finds_staging_dirs(self):
        base = os.path.join(self.work, "out")
        os.makedirs(base)
        os.makedirs(os.path.join(base, tx.STAGING_PREFIX + "aaaa"))
        os.makedirs(os.path.join(base, tx.STAGING_PREFIX + "bbbb"))
        os.makedirs(os.path.join(base, "normal-dir"))
        found = tx.discover(base)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(os.path.basename(p).startswith(tx.STAGING_PREFIX)
                            for p in found))


if __name__ == "__main__":
    unittest.main(verbosity=2)
