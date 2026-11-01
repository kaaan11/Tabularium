"""Provider token formats shared by every contains_secret.

Only prefixed formats: a generic "long base64" pattern would match digests
and identifiers. persistence, observability and tools each keep their own
key/value patterns and append these, so the three checks agree on tokens.
"""

from __future__ import annotations

import re

PROVIDER_TOKEN_PATTERNS = (
    # GitHub classic PAT: ghp_, gho_, ghu_, ghs_, ghr_ + at least 36 alphanumeric
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),
    # GitHub fine-grained PAT: github_pat_ + at least 82 [A-Za-z0-9_]
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}"),
    # OpenAI & Anthropic: sk- / sk-proj- / sk-ant- + at least 20 [A-Za-z0-9_-], word boundary required
    re.compile(r"\b(?:sk-(?:proj|ant)-[A-Za-z0-9_-]{20,}|sk-(?!proj-|ant-)[A-Za-z0-9_-]{20,})"),
    # AWS access key ID
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Slack token
    re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    # Google API key
    re.compile(r"\bAIza[0-9A-Za-z_-]{35,}"),
    # Stripe live key
    re.compile(r"\b(sk|rk)_live_[0-9A-Za-z]{24,}"),
    # JWT: three base64url parts, each at least 10 chars
    re.compile(r"\beyJ[A-Za-z0-9_-]{7,}\.eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]{10,}"),
)
