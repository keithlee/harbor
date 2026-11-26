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
import unicodedata

# The authority of a URL runs from ``//`` to the first ``/``, ``?`` or ``#``;
# userinfo is everything in it up to the last ``@``. Anything else before
# that ``@`` (a backslash, a quote) is treated as userinfo too: refusing too
# much is safe, refusing too little is not.
_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/?#]*@")
_AUTHORITY = re.compile(r"^[^:/?#]*://([^/?#]*)")
# Characters that NFKC normalization turns into ``@``: the fullwidth (U+FF20)
# and small (U+FE6B) commercial at. ``urlsplit`` and httpx normalize them, so
# they can delimit userinfo just like ``@``.
_AT = "@\uff20\ufe6b"
# The same shape anywhere in free text, such as an exception message. Only
# whitespace ends it early: httpx leaves quotes and other punctuation in
# userinfo unencoded, so a quote is no evidence that the userinfo has ended.
# The lookbehind lets a scheme start only at the beginning of a word, so a
# long run of letters is scanned once, not once per starting position.
_USERINFO_IN_TEXT = re.compile(rf"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]*://)[^/?#\s]*[{_AT}]")
# ASCII control characters (C0 and DEL). ``str.strip`` leaves NUL and most of
# these in place, and URL parsers disagree on whether to drop them, so a URL
# with one could look host-only to one check and carry userinfo to another.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

REDACTED = "[redacted]"


def _normalized(url: str | None) -> str:
    return unicodedata.normalize("NFKC", _CONTROL.sub("", str(url or ""))).strip()


def has_userinfo(url: str | None) -> bool:
    """True if ``url`` names a user, password or token before its host.

    Control characters are removed and the text is NFKC-normalized first, so
    ``"\\x00https://u:p@h"`` and a fullwidth ``＠`` are still recognized.
    """

    return bool(_USERINFO.match(_normalized(url)))


def reject_userinfo(url: str | None, label: str, token_hint: str) -> None:
    """Fail closed on a URL with control characters, userinfo or a non-ASCII authority.

    The message never repeats the URL. ``token_hint`` names where the
    credential belongs instead, for example
    ``"LINEAR_API_KEY or HARBOR_LINEAR_API_KEY"``.
    """

    text = str(url or "").strip()
    if _CONTROL.search(text):
        raise ValueError(f"the {label} URL must not contain control characters")
    if has_userinfo(text):
        raise ValueError(
            f"the {label} URL must not contain a user name, password or token (the part before '@'); "
            f"remove it and supply the credential through {token_hint}"
        )
    authority = _AUTHORITY.match(text)
    if authority and not authority.group(1).isascii():
        # Parsers disagree on how to normalize these (``urlsplit`` refuses
        # some and repeats the netloc in its error; httpx reads others as a
        # port), so refuse them all instead of guessing what a host means.
        raise ValueError(
            f"the {label} URL host must be ASCII; give an internationalized name in its xn-- form"
        )


def redact_userinfo(text: object) -> str:
    """``text`` with the userinfo of every URL in it replaced by ``[redacted]``."""

    return _USERINFO_IN_TEXT.sub(rf"\1{REDACTED}@", str(text))


def redact_http_error(message: str, endpoint: object) -> tuple[str, bool]:
    """``message`` with userinfo redacted, and whether the original error must be dropped.

    The second value is true when ``endpoint`` or ``message`` carried
    userinfo. The httpx exception then still holds it (in its message and in
    ``exc.request.url``), so the caller must raise its own error with neither
    ``__cause__`` nor ``__context__`` pointing at it.
    """

    redacted = redact_userinfo(message)
    return redacted, redacted != message or has_userinfo(str(endpoint or ""))
