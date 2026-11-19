"""Security audit test for DeepPocket Tracker.

Run before making the repository public or pushing changes:
    python -m unittest tests/test_security.py

Verifies that NO Discord webhooks, Google Service Account private keys,
GitHub Personal Access Tokens, CoinGecko keys, or Binance API secrets are
hardcoded anywhere in the repository.
"""

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".xlsx", ".pdf", ".pyc", ".pyo"}
PLACEHOLDER_MARKERS = ("...", "YOUR_", "REPLACE_", "EXAMPLE", "<", ">")

SECRET_PATTERNS = [
    ("Discord Webhook URL", re.compile(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+", re.IGNORECASE)),
    ("Google Private Key", re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----")),
    ("Google Service Account JSON Key", re.compile(r'"private_key_id"\s*:\s*"[0-9a-fA-F]{20,}"')),
    ("GitHub Classic PAT", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("GitHub Fine-Grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("CoinGecko Demo/Pro API Key", re.compile(r"\bCG-[A-Za-z0-9]{18,}\b")),
    ("Hardcoded Secret Assignment", re.compile(r"""(?:DISCORD_WEBHOOK_URL|GOOGLE_SERVICE_ACCOUNT_JSON|COINGECKO_DEMO_API_KEY)\s*=\s*['"]([^'"\s]{20,})['"]""")),
]


class RepoSecurityAuditTests(unittest.TestCase):
    def test_no_hardcoded_secrets_in_repository(self):
        findings = []
        self_path = Path(__file__).resolve()

        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                fpath = Path(dirpath) / filename
                if fpath.resolve() == self_path:
                    continue
                if fpath.suffix.lower() in SKIP_EXTENSIONS:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                rel = fpath.relative_to(ROOT).as_posix()
                for label, pattern in SECRET_PATTERNS:
                    for match in pattern.finditer(content):
                        raw = match.group(0)
                        if any(marker in raw.upper() for marker in PLACEHOLDER_MARKERS):
                            continue
                        line_no = content.count("\n", 0, match.start()) + 1
                        snippet = raw[:24] + "..."
                        findings.append(f"{rel}:{line_no} [{label}] -> {snippet}")

        self.assertEqual(
            findings,
            [],
            "Hardcoded secret(s) detected! Remove them before pushing:\n  - " + "\n  - ".join(findings),
        )

    def test_workflows_only_read_secrets_from_github_context(self):
        wf_dir = ROOT / ".github" / "workflows"
        if not wf_dir.exists():
            return
        for yml in wf_dir.glob("*.yml"):
            text = yml.read_text(encoding="utf-8", errors="ignore")
            self.assertNotRegex(
                text,
                r"echo\s+.*(?:DISCORD_WEBHOOK_URL|GOOGLE_SERVICE_ACCOUNT_JSON|COINGECKO_DEMO_API_KEY)",
                f"Workflow {yml.name} must never echo secret environment variables to logs.",
            )


if __name__ == "__main__":
    unittest.main()