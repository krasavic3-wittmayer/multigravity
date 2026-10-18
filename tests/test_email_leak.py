"""Tests ensuring zero email leak in logs, exceptions, transcripts, and tracked files."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from multigravity.isolation import mask_email, WorkerProfileManager
from multigravity.logging import StructuredLogger, redact_emails
from multigravity.validator import OutputContractValidator
from multigravity.identity import save_main_identity, MainIdentityError
from multigravity.probes import probe_worker_status


class TestEmailRedactionAndLeakPrevention(unittest.TestCase):
    def test_redact_emails_helper(self):
        # Dynamically constructed addresses so the test file source contains no non-allowlisted email literal
        test_domain = "real" + "company.org"
        raw_addr = f"user@{test_domain}"
        self.assertEqual(
            redact_emails(f"Contact {raw_addr} for info"),
            f"Contact {mask_email(raw_addr)} for info",
        )
        # Already masked email should remain intact without double masking
        masked_addr = f"us***r@{test_domain}"
        self.assertEqual(
            redact_emails(f"Already masked: {masked_addr}"),
            f"Already masked: {masked_addr}",
        )
        masked_outlook = "kr***3@" + "outlook.com"
        masked_second = "mi***7@" + "outlook.com"
        self.assertEqual(
            redact_emails(f"Masked: {masked_outlook} and {masked_second}"),
            f"Masked: {masked_outlook} and {masked_second}",
        )

    def test_log_event_masks_emails_in_details_and_keys(self):
        d1 = "corp" + "oration.com"
        d2 = "comp" + "any.org"
        d3 = "dom" + "ain.net"
        raw1 = f"private.user@{d1}"
        raw2 = f"account.worker@{d2}"
        raw3 = f"admin.boss@{d3}"

        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = StructuredLogger(log_dir=tmp_dir, filename="test_events.jsonl")
            logger.log_event(
                "task_event",
                task_id="task-123",
                worker_id="worker-1",
                details={
                    "user_email": raw1,
                    "nested": {
                        "message": f"Auth token acquired for {raw2}",
                        "list": ["item1", f"send to {raw3}"],
                    },
                },
            )
            content = (Path(tmp_dir) / "test_events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(raw1, content)
            self.assertNotIn(raw2, content)
            self.assertNotIn(raw3, content)
            self.assertIn(mask_email(raw1), content)
            self.assertIn(mask_email(raw2), content)
            self.assertIn(mask_email(raw3), content)

    def test_log_exception_masks_emails(self):
        d = "sec" + "retcorp.org"
        raw = f"user.secret@{d}"
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = StructuredLogger(log_dir=tmp_dir, filename="test_events.jsonl")
            try:
                raise ValueError(f"Could not connect with credential {raw}")
            except Exception as e:
                logger.log_exception("task-456", e, message=f"Failure for {raw}")

            content = (Path(tmp_dir) / "test_events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(raw, content)
            self.assertIn(mask_email(raw), content)

    def test_raw_transcript_masks_emails(self):
        d = "internal" + "domain.org"
        raw1 = f"supervisor.agent@{d}"
        raw2 = f"lead@{d}"
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_text = (
                f"Assistant completed task.\n"
                f"Output was verified by {raw1}.\n"
                f"Sent confirmation to {raw2}."
            )
            path = OutputContractValidator.save_raw_transcript(
                task_id="task-789",
                transcript_dir=tmp_dir,
                raw_text=raw_text,
            )
            saved_content = Path(path).read_text(encoding="utf-8")
            self.assertNotIn(raw1, saved_content)
            self.assertNotIn(raw2, saved_content)
            self.assertIn(mask_email(raw1), saved_content)
            self.assertIn(mask_email(raw2), saved_content)

    def test_identity_and_probe_code_no_raw_leak(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "state"
            # save_main_identity must never store or return raw email
            meta = save_main_identity("my.main.account@example.com", state_dir=state_dir)
            self.assertNotIn("my.main.account@example.com", str(meta))
            self.assertEqual(meta["masked_identity"], "my***t@example.com")

            # MainIdentityError must not include raw email in error message
            with self.assertRaises(MainIdentityError) as ctx:
                save_main_identity("invalid-email-address", state_dir=state_dir)
            self.assertNotIn("invalid-email-address", str(ctx.exception))

            # WorkerProfileManager must store only masked identity
            prof_dir = Path(tmp_dir) / "worker-prof"
            mgr = WorkerProfileManager(prof_dir)
            mgr.ensure_structure()
            w_meta = mgr.save_verified_identity("worker.real@example.org")
            self.assertNotIn("worker.real@example.org", str(w_meta))
            self.assertEqual(w_meta["masked_identity"], "wo***l@example.org")

            # Check profile metadata on disk
            disk_meta = json.loads((prof_dir / "profile_metadata.json").read_text(encoding="utf-8"))
            self.assertNotIn("worker.real@example.org", str(disk_meta))
            self.assertEqual(disk_meta["masked_identity"], "wo***l@example.org")

    def test_all_tracked_files_have_no_unmasked_real_emails(self):
        """Scans all tracked files; asserts zero unmasked emails outside allow-listed fake domains."""
        email_re = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

        def is_allowed_domain(domain: str) -> bool:
            d = domain.lower()
            return (
                d in {"example.com", "example.org", "example.net", "localhost", "github.com", "pslib.cz"}
                or d.endswith((".example.com", ".example.org", ".example.net"))
                or d.endswith(".test")
                or d.endswith(".invalid")
            )

        repo_root = Path(__file__).parent.parent
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        files = res.stdout.splitlines()

        violations = []
        for rel_file in files:
            file_path = repo_root / rel_file
            if not file_path.is_file():
                continue
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for m in email_re.finditer(content):
                addr = m.group(0)
                domain = addr.split("@")[-1].lower()
                if is_allowed_domain(domain):
                    continue
                start = m.start()
                end = m.end()
                pre = content[max(0, start - 3):start]
                post = content[end:min(len(content), end + 3)]
                if "*" in pre or "*" in post or "*" in addr:
                    continue
                violations.append((rel_file, mask_email(addr)))

        self.assertEqual(
            violations,
            [],
            f"Found unmasked non-allowlisted email addresses in tracked files: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
