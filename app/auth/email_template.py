"""Small, self-contained transactional email template with a plain-text companion."""

from html import escape


def html_message(
    subject: str, body: str, *, action_url: str | None = None, action_label: str | None = None
) -> str:
    """Render untrusted message copy as safe, email-client-friendly HTML."""
    title = escape(subject)
    paragraphs = []
    for part in body.strip().split("\n\n"):
        lines = [line for line in part.splitlines() if line.strip() != action_url]
        if lines:
            paragraphs.append(
                '<p style="margin:0 0 18px;color:#43564a;font-size:15px;line-height:1.65;">'
                + "<br>".join(escape(line) for line in lines)
                + "</p>"
            )
    button = ""
    fallback = ""
    if action_url and action_label:
        safe_url = escape(action_url, quote=True)
        button = (
            '<p style="margin:28px 0;text-align:left;">'
            f'<a href="{safe_url}" style="display:inline-block;background:#1c5744;'
            "border-radius:8px;color:#ffffff;font-size:15px;font-weight:700;"
            'line-height:20px;padding:14px 22px;text-decoration:none;">'
            f"{escape(action_label)}</a></p>"
        )
        fallback = (
            '<p style="margin:22px 0 0;color:#6d7f72;font-size:12px;line-height:1.5;">'
            "If the button does not work, copy this link into your browser:<br>"
            f'<a href="{safe_url}" style="color:#1c5744;word-break:break-all;">'
            f"{safe_url}</a></p>"
        )
    content = "".join(paragraphs)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title></head>"
        '<body style="margin:0;padding:0;background:#f4f7f4;'
        'font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background:#f4f7f4;"><tr><td align="center" '
        'style="padding:32px 16px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:560px;background:#ffffff;border:1px solid #e0e9e1;'
        'border-radius:14px;">'
        '<tr><td style="padding:28px 36px 22px;border-bottom:1px solid #e9eee9;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="width:42px;height:42px;border-radius:10px;background:#173f34;'
        'color:#b9e5cc;text-align:center;font-size:26px;font-weight:700;">iv</td>'
        '<td style="padding-left:12px;color:#173f34;font-size:20px;font-weight:700;">'
        'Ive <span style="color:#277253;">POS</span></td></tr></table></td></tr>'
        '<tr><td style="padding:32px 36px 34px;">'
        f'<h1 style="margin:0 0 20px;color:#173f34;font-size:24px;line-height:1.25;">{title}</h1>'
        f"{content}{button}{fallback}"
        '</td></tr><tr><td style="padding:19px 36px;border-top:1px solid #e9eee9;'
        'color:#7b8a7e;font-size:12px;line-height:1.5;">'
        "Ive POS · Transactional account email</td></tr></table>"
        "</td></tr></table></body></html>"
    )
