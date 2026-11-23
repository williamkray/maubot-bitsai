from bitsai.backend.base import (
    Conclusion,
    Investigation,
    InvestigationStatus,
    PendingAction,
    ResourceKind,
    ResourceSummary,
)
from bitsai.matrix.render import (
    TRUNCATION_TAIL,
    render_alert,
    render_chat_answer,
    render_error,
    render_investigation_result,
    render_investigation_status,
    render_pending_action,
    render_resource_card,
    render_thinking,
)
from bitsai.sites import Site

SITE = Site("datadoghq.com")


def test_relative_link_absolutised():
    c = render_chat_answer("See [mon](/monitors/123) and **bold**", SITE)
    assert 'href="https://app.datadoghq.com/monitors/123"' in c.formatted_body
    assert "<strong>bold</strong>" in c.formatted_body
    assert c.msgtype.value == "m.notice"


def test_custom_subdomain():
    c = render_chat_answer("[m](/monitors/1)", Site("datadoghq.com", "acme"))
    assert "https://acme.datadoghq.com/monitors/1" in c.formatted_body


def test_raw_html_escaped_and_js_links_dropped():
    c = render_chat_answer("<script>alert(1)</script>\n\n[x](javascript:alert(1))", SITE)
    assert "<script>" not in c.formatted_body
    assert "&lt;script&gt;" in c.formatted_body
    assert "javascript:" not in c.formatted_body


def test_truncation():
    c = render_chat_answer("x" * 100_000, SITE)
    assert len(c.body.encode()) < 31_000
    assert TRUNCATION_TAIL in c.body
    assert TRUNCATION_TAIL in c.formatted_body


def test_alert_missing_keys_and_escaping():
    assert render_alert({}, SITE).body
    c = render_alert({"title": "<b>x</b>", "tags": ["a<b"], "link": "/monitors/5"}, SITE)
    assert "<b>x</b>" not in c.formatted_body
    assert "https://app.datadoghq.com/monitors/5" in c.formatted_body
    c = render_alert({"link": "javascript:x", "snapshot_url": "javascript:y"}, SITE)
    assert "javascript" not in c.formatted_body


def test_simple_renderers():
    assert "thinking" in render_thinking().body
    assert "&lt;" in render_error("<x>").formatted_body
    pa = render_pending_action(PendingAction("1", "Delete **thing**"), SITE)
    assert "✅" in pa.body and "❌" in pa.body


def test_investigation():
    inv = Investigation(
        "i1",
        InvestigationStatus.CONCLUSIVE,
        "conclusive",
        title="T<",
        conclusions=(Conclusion("C1", "sum", "long desc"),),
        url="https://app.datadoghq.com/bits-ai/investigations/i1",
    )
    r = render_investigation_result(inv, SITE)
    assert "C1" in r.body and "sum" in r.body and "long desc" not in r.body
    assert "Open in Datadog" in r.formatted_body
    assert "long desc" in render_investigation_result(inv, SITE, detailed=True).body
    assert "1m05s" in render_investigation_status(inv, 65).body


def test_resource_card():
    r = ResourceSummary(
        ResourceKind.MONITOR, "1", "CPU <high>", "https://x/1", "Alert", (("Type", "metric"),)
    )
    c = render_resource_card(r)
    assert "&lt;high&gt;" in c.formatted_body and "Type: metric" in c.body


def test_render_notice_formats_code_and_escapes_html():
    from bitsai.matrix.render import render_notice

    c = render_notice("Linked as <b>eve</b>. Not you? Run `!bits logout` now.")
    assert "<code>!bits logout</code>" in c.formatted_body
    assert "<b>eve</b>" not in c.formatted_body and "&lt;b&gt;" in c.formatted_body
    assert c.body.startswith("Linked as")
