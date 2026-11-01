"""
src/analysis/announcement_labels.py — deterministic triage of NSE announcement labels for corporate_risk_halt
===========================================================================================================

The events lake stores NSE's category label (`subject`) for each filing, not its text; the LEGAL_RISK /
STRUCTURAL_RISK flags came from keywords in a summary that is not kept. This table sorts the labels seen in
the lake (2026-11-02: 103 carrying LEGAL_RISK, 121 carrying either flag) into three tiers:

  HARD_BLOCK        the label itself is a solvency / operations event: blocks whatever the keyword flags say.
  ALLOW_ROUTINE     an administrative label: its LEGAL_RISK keyword flag is ignored. STRUCTURAL_RISK still
                    blocks (a scheme notice published as a newspaper ad is still a demerger, ledger Issue 15).
  KEYWORD_FALLBACK  everything else, including any label not listed here: the legacy keyword flags decide.

Pure data + one lookup; no I/O. Editing a tier changes a live veto — record why in DECISIONS.md.
"""

HARD_BLOCK = "HARD_BLOCK"
ALLOW_ROUTINE = "ALLOW_ROUTINE"
KEYWORD_FALLBACK = "KEYWORD_FALLBACK"

HARD_BLOCK_LABELS = (
    "Corporate Insolvency Resolution Process",
    "CIRP - others",
    "CIRP - Commencement",
    "CIRP - Filing of application",
    "CIRP - Committee meeting updates",
    "CIRP - Approval of Resolution Plan",
    "CIRP - Filing of Resolution Plan",
    "CIRP - Change in Resolutional Professional",
    "CIRP - Revocation/rejection",
    "Liquidation",
    "Defaults on Payment of Interest/Principal",
    "Corporate Debt Restructuring",
    "Fraud/Default/Arrest",
    "Frauds/Default by employees",
    "Resignation of Statutory Auditor",
    "Suspension of Trading",
    "Suspension of trading in equity shares",
)

ALLOW_ROUTINE_LABELS = (
    "Copy of Newspaper Publication",
    "Newspaper Advertisements",
    "Shareholders meeting",
    "Annual General Meeting",
    "AGM/Book Closure",
    "Extension of Annual General Meeting",
    "Notice Of Shareholders Meetings-XBRL",
    "Extra Ordinary Meeting",
    "Record Date",
    "Revised Record date",
    "Record Date Updates",
    "Trading Window",
    "Closure of trading window",
    "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Transcript of Analysts/Institutional Investor Meet/Con. Call",
    "Investor Presentation",
    "Board Meeting Intimation",
    "Board meeting Cancelled",
    "Board Meeting Adjourned",
    "Dividend",
    "Bonus",
    "Bagging/Receiving of orders/contracts",
    "Awarding orders/contract",
    "Awarding of order(s)/contract(s)",
    "Capacity addition",
    "Publish Audited Results",
    "Integrated Filing- Financial",
    "Integrated Filing- Governance",
    "Annual Secretarial Compliance Report",
    "Code of Conduct under SEBI(PIT) Reg., 2015",
    "Disclosure under SEBI (PIT) Reg 2015",
    "Disclosure under SEBI Takeover Regulations",
    "Registrar & Share Transfer Agent Update",
    "ESOP/ESOS/ESPS",
    "Name Change",
)

# Named for the record; any unlisted label falls back the same way.
KEYWORD_FALLBACK_LABELS = (
    "Updates",
    "General Updates",
    "Clarification",
    "Reply to Clarification Sought",
    "News Verification",
    "News Clarification",
    "Press Release",
    "Outcome of Board Meeting",
    "Pendency of Litigation(s)/dispute(s) or the outcome impacting the Company",
    "Litigations/Disputes/Regulatory actions",
    "Action(s) taken or orders passed",
    "Action(s) initiated or orders passed",
    "Delay/default in the payment of fines/penalties/dues etc. to authority",
    "NCLT/ Court Convened Meeting",
    "Scheme of Arrangement",
    "Amalgamation/Merger",
    "Demerger",
    "Delisting",
    "Credit Rating",
    "Reasons for Delayed/Non-submission of Financial Results",
)


def _norm(label) -> str:
    return " ".join(str(label or "").split()).casefold()


_TIERS = {**{_norm(x): KEYWORD_FALLBACK for x in KEYWORD_FALLBACK_LABELS},
          **{_norm(x): ALLOW_ROUTINE for x in ALLOW_ROUTINE_LABELS},
          **{_norm(x): HARD_BLOCK for x in HARD_BLOCK_LABELS}}


def tier(label) -> str:
    """The label's tier; case and spacing are ignored, unknown labels fall back to the keywords."""
    return _TIERS.get(_norm(label), KEYWORD_FALLBACK)


def effective_flags(label, flags, risk_flags=("STRUCTURAL_RISK", "LEGAL_RISK")) -> list:
    """The risk flags that still count for one filing after its label is triaged."""
    t = tier(label)
    present = [f for f in (flags or []) if f in risk_flags]
    if t == HARD_BLOCK:
        return present or ["LEGAL_RISK"]
    if t == ALLOW_ROUTINE:
        return [f for f in present if f != "LEGAL_RISK"]
    return present
