"""Keep credentials out of configured API URLs and out of error messages.

A URL such as ``https://user:secret@host/graphql`` carries a credential in its
userinfo. ``httpx`` repeats the full request URL in its error messages
(``Server error '502 Bad Gateway' for url 'https://user:secret@host/graphql'``),
so a configured endpoint with userinfo would put the secret into tracebacks and
``Harbor:`` error lines. Harbor reads credentials from environment variables
only, so it refuses such URLs when the configuration is loaded, and redacts
userinfo from any URL in an HTTP error message as a second line of defence.
"""
from __future__ import annotations

import re

# The authority of a URL runs from ``//`` to the first ``/``, ``?`` or ``#``;
# userinfo is everything in it up to the last ``@``.
_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/?#]*@")
# The same shape anywhere in free text, such as an exception message. Quotes
# and whitespace end the match so a quoted URL is redacted on its own.
_USERINFO_IN_TEXT = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/?#\s'\"<>]*@")

REDACTED = "[redacted]"


def has_userinfo(url: str | None) -> bool:
    """True if ``url`` names a user, password or token before its host."""

    return bool(_USERINFO.match(str(url or "").strip()))


def reject_userinfo(url: str | None, label: str, token_hint: str) -> None:
    """Fail closed on a URL with userinfo, without repeating the URL.

    ``token_hint`` names where the credential belongs instead, for example
    ``"LINEAR_API_KEY or HARBOR_LINEAR_API_KEY"``.
    """

    if has_userinfo(url):
        raise ValueError(
            f"the {label} URL must not contain a user name, password or token (the part before '@'); "
            f"remove it and supply the credential through {token_hint}"
        )


def redact_userinfo(text: object) -> str:
    """``text`` with the userinfo of every URL in it replaced by ``[redacted]``."""

    return _USERINFO_IN_TEXT.sub(rf"\1{REDACTED}@", str(text))
