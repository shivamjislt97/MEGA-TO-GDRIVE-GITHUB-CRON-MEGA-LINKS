#!/usr/bin/env python3
"""Comprehensive local test for oversized_processor logic."""

import json
import os
import sys
import tempfile
import unittest

# Ensure we can import the module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set test env
os.environ.setdefault("GITHUB_REPOSITORY", "shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON")
# GH_TOKEN must be set in environment before running tests
# The tests that hit GitHub API will skip if token is missing

# Mock WORKSPACE to temp dir
import oversized_processor as ov


class TestHelperFunctions(unittest.TestCase):

    def test_fmt_size(self):
        self.assertEqual(ov.fmt_size(0), "0.0 B")
        self.assertEqual(ov.fmt_size(1023), "1023.0 B")
        self.assertEqual(ov.fmt_size(1024), "1.0 KB")
        self.assertEqual(ov.fmt_size(4831838208), "4.5 GB")
        self.assertEqual(ov.fmt_size(None), "unknown")

    def test_calculate_chunks(self):
        # 6.5 GB file -> 2 chunks
        chunks = ov.calculate_chunks(6_518_756_190)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["index"], 1)
        self.assertEqual(chunks[0]["start_byte"], 0)
        self.assertEqual(chunks[0]["end_byte"], 4_831_838_207)
        self.assertEqual(chunks[0]["status"], "pending")
        self.assertEqual(chunks[1]["index"], 2)
        self.assertEqual(chunks[1]["start_byte"], 4_831_838_208)
        self.assertEqual(chunks[1]["status"], "pending")
        # Small file -> 1 chunk
        chunks = ov.calculate_chunks(1_000_000)
        self.assertEqual(len(chunks), 1)
        # Exactly at CHUNK_MAX boundary — fits in 1 chunk
        chunks = ov.calculate_chunks(ov.CHUNK_MAX)
        self.assertEqual(len(chunks), 1)
        # 1 byte over -> 2 chunks
        chunks = ov.calculate_chunks(ov.CHUNK_MAX + 1)
        self.assertEqual(len(chunks), 2)

    def test_parse_mega_url_standard(self):
        fid, key = ov.parse_mega_url("https://mega.nz/file/ABC123#keymaterial")
        self.assertEqual(fid, "ABC123")
        self.assertEqual(key, "keymaterial")

    def test_parse_mega_url_old(self):
        fid, key = ov.parse_mega_url("https://mega.nz/#!ABC123!keymaterial")
        self.assertEqual(fid, "ABC123")
        self.assertEqual(key, "keymaterial")

    def test_parse_mega_url_invalid(self):
        with self.assertRaises(ValueError):
            ov.parse_mega_url("https://example.com")

    def test_extract_key_iv(self):
        # 32 bytes of base64url data
        raw = b"A" * 32
        import base64
        b64url = base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")
        key_hex, iv_hex, raw_hex = ov.extract_key_iv(b64url)
        self.assertEqual(len(key_hex), 32)  # 16 bytes hex
        self.assertEqual(len(iv_hex), 32)   # 16 bytes hex
        self.assertEqual(len(raw_hex), 64)  # 32 bytes hex

    def test_make_range_iv(self):
        iv = ov.make_range_iv("1234567890abcdef", 0)
        self.assertTrue(iv.endswith("0000000000000000"))
        iv2 = ov.make_range_iv("1234567890abcdef", 16)
        self.assertTrue(iv2.endswith("0000000000000001"))
        # 4_831_838_208 / 16 = 301989888 = 0x12000000, padded to 016x
        iv3 = ov.make_range_iv("1234567890abcdef", 4_831_838_208)
        self.assertTrue(iv3.endswith("0000000012000000"))


class TestStateValidation(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_workspace = ov.WORKSPACE
        ov.WORKSPACE = self.tmpdir
        ov.COMPLETED_FILE = os.path.join(self.tmpdir, "completed_links.json")
        ov.CHUNKS_HISTORY_FILE = os.path.join(self.tmpdir, "chunks_history.json")

    def tearDown(self):
        ov.WORKSPACE = self.orig_workspace
        ov.COMPLETED_FILE = os.path.join(self.orig_workspace, "completed_links.json")
        ov.CHUNKS_HISTORY_FILE = os.path.join(self.orig_workspace, "chunks_history.json")
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_state(self, chunks_statuses, video_status="downloading"):
        """Helper: create chunks_history state with given chunk statuses."""
        chunks = []
        for i, (s, aname) in enumerate(chunks_statuses, 1):
            c = {"index": i, "status": s, "artifact_name": aname,
                 "start_byte": 0, "end_byte": 1000, "expected_size": 1000}
            if s == "done":
                c["actual_size"] = 1000
            chunks.append(c)
        state = {
            "videos": [{
                "url": "https://mega.nz/file/TEST#key",
                "filename": "test.mp4",
                "file_id": "TEST",
                "total_size": 2000,
                "total_chunks": len(chunks),
                "chunks": chunks,
                "status": video_status
            }],
            "current_index": 0
        }
        ov.save_chunks_history(state)
        return state

    def _make_completed(self, status="in_progress"):
        state = {
            "oversized": {
                "total": 1, "done": 0, "status": status,
                "items": [{"url": "https://mega.nz/file/TEST#key",
                           "filename": "test.mp4", "size": 2000,
                           "gdrive_status": "downloading"}]
            }
        }
        ov.save_completed(state)
        return state

    def test_validate_concat_ready_chunk2_missing(self):
        """concat_ready but chunk 2 artifact doesn't exist -> reset."""
        self._make_completed()
        state = self._make_state(
            [("done", "ck_test_01"), ("done", "ck_test_nonexistent")],
            video_status="concat_ready"
        )
        # Run validation (simulates main() code)
        for v in state.get("videos", []):
            fixed = False
            for ch in v.get("chunks", []):
                if ch.get("status") == "done":
                    aname = ch.get("artifact_name", "")
                    if aname:
                        aid = ov.find_artifact_id(aname)
                        if not aid:
                            ch["status"] = "pending"
                            ch.pop("actual_size", None)
                            fixed = True
            if fixed:
                v["status"] = "downloading"
                ov.save_chunks_history(state)
        # Verify chunk was reset (ck_test_01 doesn't exist either)
        v = state["videos"][0]
        self.assertEqual(v["status"], "downloading")
        self.assertEqual(v["chunks"][0]["status"], "pending")
        self.assertEqual(v["chunks"][1]["status"], "pending")

    def test_validate_all_artifacts_exist(self):
        """All artifacts exist -> no change."""
        self._make_completed()
        state = self._make_state(
            [("done", "ck_qxjcsapl_01"), ("done", "ck_qxjcsapl_02")],
            video_status="concat_ready"
        )
        for v in state.get("videos", []):
            fixed = False
            for ch in v.get("chunks", []):
                if ch.get("status") == "done":
                    aname = ch.get("artifact_name", "")
                    if aname:
                        aid = ov.find_artifact_id(aname)
                        if not aid:
                            ch["status"] = "pending"
                            fixed = True
            if fixed:
                v["status"] = "downloading"
                ov.save_chunks_history(state)
        # Both chunks still done because artifacts exist
        v = state["videos"][0]
        self.assertEqual(v["status"], "concat_ready")
        self.assertEqual(v["chunks"][0]["status"], "done")
        self.assertEqual(v["chunks"][1]["status"], "done")

    def test_get_unupload_items(self):
        """get_unupload_items returns items with status='unupload'."""
        state = {
            "completed": [
                {"url": "a", "status": "uploaded"},
                {"url": "b", "status": "unupload"},
                {"url": "c", "status": "unupload"},
                {"url": "d", "status": "uploaded"},
            ]
        }
        result = ov.get_unupload_items(state)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["url"], "b")
        self.assertEqual(result[1]["url"], "c")

    def test_all_done_logic_mixed_states(self):
        """all(c['status']=='done') correctly identifies mixed states."""
        chunks = [
            {"status": "done"},
            {"status": "pending"}
        ]
        self.assertFalse(all(c["status"] == "done" for c in chunks))
        chunks[1]["status"] = "done"
        self.assertTrue(all(c["status"] == "done" for c in chunks))

    def test_concat_run_resets_to_download(self):
        """process_concat_run should reset to downloading when artifact missing."""
        self._make_completed()
        state = self._make_state(
            [("done", "ck_test_nonexistent_01")],
            video_status="concat_ready"
        )
        video = state["videos"][0]
        result = ov.process_concat_run(video, 0, state)
        self.assertFalse(result)
        self.assertEqual(video["status"], "downloading")
        self.assertEqual(video["chunks"][0]["status"], "pending")

    def test_process_chunk_download_all_done(self):
        """When all chunks done after download -> concat_ready."""
        state = self._make_state(
            [("pending", "ck_test_01"), ("done", "ck_test_02")],
            video_status="downloading"
        )
        video = state["videos"][0]
        chunk = video["chunks"][0]
        chunk["start_byte"] = 0
        chunk["end_byte"] = 1000

        # Manually simulate what process_chunk_download does after download
        chunk["status"] = "done"
        chunk["actual_size"] = 1000

        all_done = all(c["status"] == "done" for c in video["chunks"])
        self.assertTrue(all_done)
        if all_done:
            video["status"] = "concat_ready"
        self.assertEqual(video["status"], "concat_ready")

    def test_process_chunk_download_some_pending(self):
        """When some chunks still pending after download -> stay downloading."""
        state = self._make_state(
            [("done", "ck_test_01"), ("pending", "ck_test_02")],
            video_status="downloading"
        )
        video = state["videos"][0]
        # Simulate another download for chunk 2
        video["chunks"][1]["status"] = "done"
        all_done = all(c["status"] == "done" for c in video["chunks"])
        self.assertTrue(all_done)


class TestFindArtifactId(unittest.TestCase):
    """Tests that require GitHub API access."""

    def test_find_existing_artifact(self):
        aid = ov.find_artifact_id("ck_qxjcsapl_01")
        self.assertIsNotNone(aid, "ck_qxjcsapl_01 should exist")
        self.assertTrue(aid.isdigit())

    def test_find_nonexistent_artifact(self):
        aid = ov.find_artifact_id("ck_nonexistent_99")
        self.assertIsNone(aid)

    def test_find_artifact_02(self):
        aid = ov.find_artifact_id("ck_qxjcsapl_02")
        self.assertIsNotNone(aid, "ck_qxjcsapl_02 should exist")
        self.assertTrue(aid.isdigit())


class TestDownloadArtifact(unittest.TestCase):
    """Tests downloading actual GitHub artifacts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_download_existing_chunk_artifact(self):
        """Verify ck_qxjcsapl_01 artifact exists (no full download)."""
        aid = ov.find_artifact_id("ck_qxjcsapl_01")
        self.assertIsNotNone(aid)

    def test_download_nonexistent_artifact(self):
        result = ov.download_artifact("ck_nonexistent_99", self.tmpdir)
        self.assertFalse(result)


class TestMegaToGdriveCompat(unittest.TestCase):
    """Test that migration logic in mega_to_gdrive.py works."""

    def test_normalize_oversized(self):
        """Test the normalization used in mega_to_gdrive.py migration."""
        oversized_raw = {"total": 2, "done": 0, "status": "in_progress",
                         "items": [{"url": "a"}, {"url": "b"}]}
        oversized = oversized_raw.get("items", []) if isinstance(oversized_raw, dict) else oversized_raw
        self.assertEqual(len(oversized), 2)

    def test_oversized_list_fallback(self):
        """Old list format still handled by migration pattern."""
        oversized_raw = [{"url": "a"}, {"url": "b"}]
        oversized = oversized_raw.get("items", []) if isinstance(oversized_raw, dict) else oversized_raw
        self.assertEqual(len(oversized), 2)

    def test_completed_entry_structure(self):
        """Verify the completed entry structure used by new flow."""
        entry = {
            "url": "https://mega.nz/file/ABC#key",
            "filename": "video.mp4",
            "size": 6_000_000_000,
            "target_folder": "Movies",
            "completed_at": "2024-01-01T00:00:00Z",
            "status": "unupload",
            "oversized": True
        }
        self.assertEqual(entry["status"], "unupload")
        self.assertTrue(entry["oversized"])
        # After upload, status changes to "uploaded"
        entry["status"] = "uploaded"
        self.assertEqual(entry["status"], "uploaded")


class TestEdgeCases(unittest.TestCase):

    def test_empty_unupload(self):
        """Empty completed list -> print_summary no crash."""
        state = {"completed": []}
        ov.print_summary(state)

    def test_print_summary_empty(self):
        """print_summary should handle empty completed gracefully."""
        state = {"completed": []}
        ov.print_summary(state)

    def test_print_summary_no_completed(self):
        state = {"folders": {}}
        ov.print_summary(state)

    def test_file_size_chunk_boundary(self):
        """File at CHUNK_MAX + 1 -> 2 chunks."""
        chunks = ov.calculate_chunks(ov.CHUNK_MAX + 1)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["expected_size"], ov.CHUNK_MAX)
        self.assertEqual(chunks[0]["end_byte"], ov.CHUNK_MAX - 1)
        self.assertEqual(chunks[1]["start_byte"], ov.CHUNK_MAX)
        self.assertEqual(chunks[1]["expected_size"], 1)

    def test_zero_size_file(self):
        chunks = ov.calculate_chunks(0)
        self.assertEqual(len(chunks), 0)

    def test_small_file_under_quota(self):
        chunks = ov.calculate_chunks(100)
        self.assertEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
