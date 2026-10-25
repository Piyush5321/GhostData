"""Decide whether a request text authorizes deletion (or verification of a deletion)."""

import re

# A negation tied to a deletion verb overrides everything else,
# e.g. "please don't delete or change anything", "not a request to erase it".
NEGATION_PATTERNS = (
    r"\b(?:do not|don't|dont|should not|shouldn't|never)\s+(?:delete|erase|close|remove|deactivate)",
    r"\bnot (?:a request|asking|requesting) (?:for|to) (?:the )?(?:delet|eras|remov|clos)",
    r"\bwithout (?:deleting|removing|erasing|closing)\b",
)

# Stems, so delete / deleted / deleting / deletion all match.
DELETION_PATTERNS = (
    r"\bdelet", r"\beras(?:e|ed|ing|ure)\b", r"\bremov", r"\bpurg", r"\bdestroy",
    r"right to be forgotten",
    r"\b(?:account|profile|data) (?:is|was|are|were) (?:gone|deleted|removed)\b",
    # Spanish / Portuguese / French / German / Italian
    r"\belimina", r"\bborra", r"\bexclus[ãa]o\b", r"\bexclu[ií]r", r"\bapaga",
    r"\bsuppression\b", r"\bsupprim", r"\beffac", r"\bl[öo]sch", r"\bcancellazion",
    # Japanese / Korean / Chinese
    r"削除", r"삭제", r"删除", r"刪除",
)

CLOSURE_PATTERN = r"\bclos(?:e|ed|ing|ure)\b"
CLOSURE_CONTEXT = ("account", "profile", "data", "information", "personal")


def is_deletion_request(request_text):
    """True when the text asks for deletion; access, correction, export and opt-out do not."""
    if not request_text:
        return False
    text = request_text.strip().lower().replace("’", "'")
    if any(re.search(p, text) for p in NEGATION_PATTERNS):
        return False
    if any(re.search(p, text) for p in DELETION_PATTERNS):
        return True
    # "close" counts only when it refers to the account or personal data
    if re.search(CLOSURE_PATTERN, text):
        return any(word in text for word in CLOSURE_CONTEXT)
    return False
