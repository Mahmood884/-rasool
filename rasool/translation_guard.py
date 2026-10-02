"""Protect technical identifiers while translating text."""
import re

URL_PATTERN = re.compile(r"https?://[^\s]+")
REPO_PATTERN = re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b")
VERSION_PATTERN = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
FILE_EXTENSION_PATTERN = re.compile(r"(?i:\.(?:py|js|json|md)\b)")
IDENT_PATTERN = re.compile(
    r"\b(?:"
    r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+"  # PascalCase
    r"|[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+"  # camelCase
    r"|[a-z]+(?:_[a-z0-9]+)+"  # snake_case
    r"|[A-Z]{2,}(?:_[A-Z0-9]+)+"  # SCREAMING_CASE
    r")\b"
)

ACRONYMS = {
    "API", "HTTP", "HTTPS", "JSON", "YAML", "URL", "CLI", "SDK", "SQL",
    "REST", "GRPC", "CPU", "GPU", "RAM", "SSD", "HDD", "USB", "DNS",
    "TCP", "UDP", "IP", "SSH", "TLS", "SSL", "JWT", "OAuth",
    "ECU", "CAN", "OBD", "MQTT", "LoRa",
}
LANGUAGES = {
    "Python", "Rust", "Go", "JavaScript", "TypeScript", "Java",
    "Kotlin", "Swift", "Ruby", "PHP", "C", "C++", "C#", "Bash", "Shell",
}
PLATFORMS = {
    "Linux", "Windows", "macOS", "Docker", "Kubernetes", "Android",
    "iOS", "Ubuntu", "Debian", "Arch",
}

_PROTECTED_WORDS = ACRONYMS | LANGUAGES | PLATFORMS
_WORD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(word) for word in sorted(_PROTECTED_WORDS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])"
)
_TOKEN_PATTERN = re.compile(
    "|".join(
        f"(?:{pattern.pattern})"
        for pattern in (
            URL_PATTERN,
            REPO_PATTERN,
            VERSION_PATTERN,
            FILE_EXTENSION_PATTERN,
            IDENT_PATTERN,
            _WORD_PATTERN,
        )
    )
)
_REPOSITORY_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
)
_MIXED_REPOSITORY_TITLE_PATTERN = re.compile(
    r"(?P<description>.+?)(?P<separator>\s+[—–-]\s+)"
    r"(?P<identifier>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*"
)


def is_repository_identifier(text: str) -> bool:
    """Return whether the whole title is a GitHub-style owner/repository ID."""
    return _REPOSITORY_IDENTIFIER_PATTERN.fullmatch(text.strip()) is not None


def split_repository_title(text: str) -> tuple[str, str, str] | None:
    """Split a descriptive title from a trailing repository identifier."""
    match = _MIXED_REPOSITORY_TITLE_PATTERN.fullmatch(text)
    if match is None:
        return None
    return (
        match.group("description").rstrip(),
        match.group("separator"),
        match.group("identifier"),
    )


def split_protected(text: str) -> list[tuple[str, bool]]:
    """Split text into translatable spans and exact technical tokens."""
    segments: list[tuple[str, bool]] = []
    position = 0
    for match in _TOKEN_PATTERN.finditer(text):
        if position < match.start():
            segments.append((text[position:match.start()], False))
        segments.append((match.group(0), True))
        position = match.end()
    if position < len(text):
        segments.append((text[position:], False))
    if not segments and text:
        segments.append((text, False))
    return segments


def protect(text: str) -> tuple[str, dict[str, str]]:
    """Replace technical tokens with placeholders and return their originals."""
    mapping: dict[str, str] = {}
    counter = 0

    def replace_token(match: re.Match[str]) -> str:
        nonlocal counter
        token = match.group(0)
        placeholder = f"__GUARD_{counter}__"
        while placeholder in text or placeholder in mapping:
            counter += 1
            placeholder = f"__GUARD_{counter}__"
        counter += 1
        mapping[placeholder] = token
        return placeholder

    for pattern in (
        URL_PATTERN,
        REPO_PATTERN,
        VERSION_PATTERN,
        FILE_EXTENSION_PATTERN,
        IDENT_PATTERN,
        _WORD_PATTERN,
    ):
        text = pattern.sub(replace_token, text)
    return text, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """Restore protected tokens from placeholders."""
    for placeholder, token in mapping.items():
        text = text.replace(placeholder, token)
    return text
