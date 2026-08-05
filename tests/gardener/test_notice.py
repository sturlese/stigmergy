"""`gardener.notice` — the SLA Slack notice: composition and posting, fully offline
(`FakeSlackGateway`, no DB, no real Slack credentials).

Stated plainly, because a check that stops running must be impossible to miss: NO live check
produces an `sla` finding. Both arms of the contradiction SLA — the only producers there have ever
been — went with the canon lane. The notice mechanism is severity-driven and generic, so it
survives and is still worth testing; the findings below are synthetic, and carry those two retired
slugs because the mechanism itself never reads a slug.
"""

import pytest

from stigmergy.gardener import notice, schema
from stigmergy.gardener.errors import GardenerError


def _finding(check="contradiction-sla-open", severity=schema.SEVERITY_SLA, **extra):
    f = {"check": check, "severity": severity, "source": schema.SOURCE_DETERMINISTIC,
        "subject": "x.md", "detail": "plain detail", "suggested_action": "plain action"}
    f.update(extra)
    return f



# ── compose_notice ──────────────────────────────────────────────────────────────────────────────
def test_compose_notice_singular_wording_no_literal_parenthetical_s():
    text = notice.compose_notice([_finding()], run_date="2026-07-31")
    assert text.startswith(f"{notice.WARNING_EMOJI} SLA: stigmergy-gardener found 1 issue this "
                           "run (2026-07-31)")
    assert "issue(s)" not in text


def test_compose_notice_plural_wording_and_numbered_list_and_footer():
    findings = [_finding(check="contradiction-sla-open"),
               _finding(check="contradiction-sla-orphaned")]
    text = notice.compose_notice(findings, run_date="2026-07-31")
    assert text.startswith(f"{notice.WARNING_EMOJI} SLA: stigmergy-gardener found 2 issues this "
                           "run (2026-07-31)")
    assert "\n1. contradiction-sla-open — " in text
    assert "\n2. contradiction-sla-orphaned — " in text
    assert text.rstrip("\n").endswith("Full report: `stigmergy-gardener`")


def test_compose_notice_prefers_notice_specific_wording_when_a_check_sets_it():
    f = _finding(detail="report wording", suggested_action="report action",
                _notice_detail="notice wording", _notice_action="notice action")
    text = notice.compose_notice([f], run_date="2026-07-31")
    assert "notice wording" in text
    assert "notice action" in text
    assert "report wording" not in text
    assert "report action" not in text


def test_compose_notice_falls_back_to_report_wording_when_notice_specific_is_absent():
    f = _finding(detail="report wording", suggested_action="report action")
    text = notice.compose_notice([f], run_date="2026-07-31")
    assert "report wording" in text
    assert "report action" in text



def test_compose_notice_uses_the_bell_never_the_warning_glyph_elsewhere_in_the_fleet():
    """The bell means "a decision is waiting in review_queue" everywhere else in this codebase,
    and no `review_decide` verdict ever closes an SLA finding — so this notice must never reuse
    it."""
    text = notice.compose_notice([_finding()], run_date="2026-07-31")
    assert "\U0001f514" not in text


# ── require_channel ───────────────────────────────────────────────────────────────────────────
def test_require_channel_refuses_when_unset_naming_the_var_and_the_fix():
    with pytest.raises(GardenerError, match=notice.DIGEST_CHANNEL_ID_ENV):
        notice.require_channel("")


def test_require_channel_returns_the_value_when_set():
    assert notice.require_channel("C0123456789") == "C0123456789"





