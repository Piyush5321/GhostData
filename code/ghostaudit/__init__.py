"""Ghost Data Still Exists: deterministic deletion-audit agent.

Modules:
    io_utils    CSV loading and timestamp parsing
    classifier  does a request authorize deletion?
    identity    subject resolution and whole-token identifier matching
    evidence    evidence parsers (SQL, JSON, logs, handover text, PDF)
    state       evidence state normalization (presence, flags, exposure)
    policy      data category and deletion_rules.csv action
    audit       per-case reconstruction at audit_at and step generation
    validation  output contract checks and sample scoring
"""

__all__ = ["audit", "classifier", "evidence", "identity", "io_utils", "policy", "state", "validation"]
