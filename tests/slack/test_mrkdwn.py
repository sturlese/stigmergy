"""`stigmergy.slack.mrkdwn` — CommonMark -> Slack mrkdwn, asserted per construct rather than on one
sample string."""
from stigmergy.slack.mrkdwn import escape_mrkdwn, to_mrkdwn


def test_bold_becomes_single_asterisk():
    assert to_mrkdwn("this is **important**") == "this is *important*"


def test_link_becomes_angle_bracket_pipe_form():
    assert to_mrkdwn("see [the page](https://example.com/x)") == "see <https://example.com/x|the page>"


def test_bare_link_with_no_text_keeps_only_the_url():
    assert to_mrkdwn("[](https://example.com/x)") == "<https://example.com/x>"


def test_bullet_list_gets_a_bullet_glyph():
    out = to_mrkdwn("- first\n- second\n* third")
    assert out == "• first\n• second\n• third"


def test_numbered_list_is_left_alone():
    assert to_mrkdwn("1. first\n2. second") == "1. first\n2. second"


def test_inline_code_is_unchanged():
    assert to_mrkdwn("run `stigmergy-index --rebuild`") == "run `stigmergy-index --rebuild`"


def test_inline_code_protects_bold_markers_inside_it():
    # a literal "**" inside a code span must NOT become mrkdwn bold
    assert to_mrkdwn("the value is `a**b`") == "the value is `a**b`"


def test_fenced_code_block_is_unchanged_besides_the_language_tag():
    src = "```python\nx = 1\n```"
    assert to_mrkdwn(src) == "```\nx = 1\n```"


def test_fenced_code_block_with_no_language_tag_is_untouched():
    src = "```\nx = 1\n```"
    assert to_mrkdwn(src) == src


def test_fenced_code_protects_bold_and_link_syntax_inside_it():
    src = "```\n**not bold** [not a link](http://x)\n```"
    assert to_mrkdwn(src) == src


def test_heading_becomes_bold_with_no_leading_hashes():
    assert to_mrkdwn("## Section title") == "*Section title*"


def test_every_construct_together_in_one_answer():
    """All the constructs at once: bold, a link, a list, inline code, a fenced block."""
    src = ("**Summary**\n\n- point one with `code`\n- [a link](https://example.com)\n\n"
          "```\nraw block\n```")
    out = to_mrkdwn(src)
    assert "*Summary*" in out
    assert "• point one with `code`" in out
    assert "• <https://example.com|a link>" in out
    assert "```\nraw block\n```" in out


def test_empty_and_none_input_do_not_raise():
    assert to_mrkdwn("") == ""
    assert to_mrkdwn(None) == ""


# ── escape_mrkdwn ─────────────────────────────────────────────────────────────────────────────
def test_escape_mrkdwn_escapes_amp_lt_gt_in_that_order():
    assert escape_mrkdwn("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_escape_mrkdwn_does_not_double_escape_an_existing_entity():
    # `&` is escaped first; a literal "&lt;" in source text becomes "&amp;lt;" — correct HTML-style
    # escaping, not an attempt to detect pre-escaped input.
    assert escape_mrkdwn("&lt;") == "&amp;lt;"


def test_escape_mrkdwn_handles_empty_and_none():
    assert escape_mrkdwn("") == ""
    assert escape_mrkdwn(None) == ""


def test_a_heading_that_is_already_bold_does_not_double_the_asterisks():
    """OLD BEHAVIOUR: `## **Q3 results**` came out as `**Q3 results**`, and a heading with two
    bold runs as `**A* and *B**` — asterisks that do not pair at all, so Slack renders stray
    literal ones in the middle of an answer.

    The heading rewrite wrapped the still-`**`-marked text in one more pair, and `_BOLD_RE` then
    re-paired the resulting run from the left. mrkdwn has ONE emphasis level, so a heading that is
    already bold cannot be bolded again: the inner markers are dropped, which is exactly what this
    module's own contract line says a heading becomes (`headings # text -> *text*`).
    """
    assert to_mrkdwn("## **Q3 results**") == "*Q3 results*"
    assert to_mrkdwn("# **Title**") == "*Title*"
    assert to_mrkdwn("### **A** and **B**") == "*A and B*"


def test_a_plain_heading_and_ordinary_bold_are_untouched():
    """The benign twin: the fix must not cost the two shapes that already worked."""
    assert to_mrkdwn("## Section title") == "*Section title*"
    assert to_mrkdwn("some **bold** prose") == "some *bold* prose"
