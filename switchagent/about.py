"""Where the About page sends people: one place for every address."""

from __future__ import annotations

REPO_URL = "https://github.com/Steklorez/SwitchAgent"
ISSUE_URL = REPO_URL + "/issues/new/choose"
RELEASES_URL = REPO_URL + "/releases"

# Telegram chat for questions. Left empty, the page
# then shows nothing about Telegram rather than a dead link.
TELEGRAM_CHAT_URL = "https://t.me/+IRBCrKQ2k-8wOGNi"

LINKS = [
    ("Source code", REPO_URL, "GitHub repository"),
    ("Releases", RELEASES_URL, "download a new version, see what changed"),
    ("Report a bug or ask a question", ISSUE_URL, "GitHub Issues"),
    ("Support the author on Ko-fi", "https://ko-fi.com/I3I0273OYI", "optional, appreciated"),
    ("Support the author on Boosty", "https://boosty.to/steklorez", "optional, appreciated"),
]
