"""Follow-ups after a territory pack goes out: the task schedule, the email each task carries, and SMTP.

Plan, Phase 4: "tasks created at day 2/5/9/14 after pack_sent with templated emails". The four templates
(day 2 check-in, day 5 numbers walk-through, day 9 objections, day 14 close or park) are Jinja2 text,
rendered into the task on the opportunity page so the text can be copied into any mail client, and sent
from here only when SMTP_HOST is configured (app.config: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
SMTP_STARTTLS, MAIL_FROM). The prospect is corresponding with Hello New Home, so MAIL_FROM should be a
Hello New Home mailbox and the signature names the founder.

Money rule: the day 5 email mentions the quoted fees only when every assumption in the territory model is
confirmed (app.services.packs.assumptions_confirmed). Until then it says the figures will follow from head
office in writing, which is also the truth.

British English, no exclamation marks, no em dashes, nothing promised that the prospectus does not say.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from jinja2 import Environment, StrictUndefined

from .. import config
from . import settings
from .packs import pounds

# (days after the pack was sent, template key, task title). The keys are what tasks.template_key stores.
SCHEDULE: tuple[tuple[int, str, str], ...] = (
    (2, "followup_day2", "Day 2: check the pack arrived and offer a call"),
    (5, "followup_day5", "Day 5: walk through the numbers"),
    (9, "followup_day9", "Day 9: answer the usual objections"),
    (14, "followup_day14", "Day 14: close or park"),
)
TEMPLATE_KEYS = tuple(key for _days, key, _title in SCHEDULE)

_SUBJECTS = {
    "followup_day2": "Your Hello New Home territory pack for {{ territory }}",
    "followup_day5": "The numbers behind the {{ territory }} territory report",
    "followup_day9": "Three questions people ask about the Local Partner programme",
    "followup_day14": "{{ territory }}: go ahead, or park it for now",
}

_BODIES = {
    "followup_day2": """Hi {{ first_name }},

I sent the Local Partner pack for {{ territory }} on {{ pack_sent_on }}: the prospectus and the territory report. Has it arrived, and have you had a chance to read it?

Two places to start: section 3 of the prospectus, which sets out what an Area Partner does week to week, and the territory report, which lists the postcode districts and the public data behind each one.

If anything is unclear, reply to this email. I can also book a 20 minute call to walk through it together; tell me two times that suit and I will confirm one.

Best wishes,
{{ sender_name }}
Hello New Home
""",
    "followup_day5": """Hi {{ first_name }},

A short note on the numbers in the territory report for {{ territory }}.

{% if confirmed -%}
The report prices the territory at {{ upfront }} upfront and {{ monthly }} a month, and shows the modelled partner earnings at 30 managed units on the published fee sheet (12% of rent, capped at 150 a month). The band is {{ band }}.
{%- else -%}
The report shows the public data behind each district: private-rented households and the local rents. The fee schedule and the modelled earnings are being finalised and will follow from head office in writing; nothing in the pack is a quote until then.
{%- endif %}

The figures are modelled, not observed: nobody has yet run a territory under this programme, and the prospectus says so in section 11. What I can show you on a call is how the model gets from local rents to a monthly figure, and what you would need to do each week to reach 30 units.

Would a 30 minute call this week suit? Reply with two times that work and I will confirm one.

Best wishes,
{{ sender_name }}
Hello New Home
""",
    "followup_day9": """Hi {{ first_name }},

When people read the Local Partner pack, the same three questions come up, so here they are with straight answers.

1. "Is this a franchise?" A solicitor is confirming the status of the programme before any agreement is signed, and the paperwork will follow what they say. Nothing is signed until that is settled.

2. "What if I cannot do the viewings?" Head office runs contracts, referencing, deposits, rent collection and compliance. The on-the-ground part is yours, and section 5 of the prospectus sets out the hours it takes week to week.

3. "What happens if it does not work out?" Either side can end the agreement on 30 days' written notice, and during the first three months either side can walk away at once (prospectus, section 10).

If your question is a different one, reply and I will answer it directly.

Best wishes,
{{ sender_name }}
Hello New Home
""",
    "followup_day14": """Hi {{ first_name }},

It is two weeks since the pack for {{ territory }} went out, so I would like to settle where things stand.

If you want to go ahead, reply and I will send the next steps: the agreement from the solicitor, the checks in section 6 of the prospectus, and the onboarding dates.

If the timing is wrong, that is fine. Tell me and I will park it. The districts stay open until someone else takes them, and you are welcome to come back.

If I do not hear from you by {{ park_on }}, I will assume it is parked for now.

Best wishes,
{{ sender_name }}
Hello New Home
""",
}

_env = Environment(autoescape=False, undefined=StrictUndefined, trim_blocks=False, lstrip_blocks=False, keep_trailing_newline=True)
_subjects = {key: _env.from_string(text) for key, text in _SUBJECTS.items()}
_bodies = {key: _env.from_string(text) for key, text in _BODIES.items()}


@dataclass(frozen=True)
class Email:
    subject: str
    body: str


def due_dates(start: date) -> list[tuple[date, str, str]]:
    """[(due_on, template_key, title), ...] from the day the pack went out."""
    return [(start + timedelta(days=days), key, title) for days, key, title in SCHEDULE]


def create_followups(db, opportunity, start: date) -> list:
    """One task per schedule entry that does not already have an open task on this opportunity, so sending
    the pack twice never doubles the list while a cadence is running. Caller commits."""
    from ..models import Task
    open_keys = {t.template_key for t in db.query(Task).filter(Task.opportunity_id == opportunity.id, Task.state == "open").all()}
    created = []
    for due_on, key, title in due_dates(start):
        if key in open_keys:
            continue
        task = Task(opportunity_id=opportunity.id, due_on=due_on, title=title, template_key=key, state="open")
        db.add(task)
        created.append(task)
    return created


def first_name(person) -> str:
    name = (getattr(person, "name", "") or "").strip()
    return name.split()[0] if name else "there"


def territory_label(opportunity) -> str:
    codes = [str(c).upper() for c in (opportunity.districts or [])]
    if not codes:
        return "your territory"
    if len(codes) <= 4:
        return ", ".join(codes)
    return ", ".join(codes[:3]) + f" and {len(codes) - 3} more districts"


def email_for(template_key: str, opportunity, *, confirmed: bool, pack_sent_on: date | None = None,
              today: date | None = None, sender_name: str | None = None) -> Email | None:
    """The rendered email for a task, or None for a task that is not one of the four templates. Money appears
    only with confirmed=True; the caller passes assumptions_confirmed(db)."""
    if template_key not in _bodies:
        return None
    today = today or date.today()
    sent = pack_sent_on or (opportunity.stage_changed_at.date() if opportunity.stage_changed_at else today)
    band = (opportunity.band or "").replace("_", " ")
    ctx = {
        "first_name": first_name(opportunity.person),
        "territory": territory_label(opportunity),
        "pack_sent_on": sent.strftime("%d %B %Y"),
        "park_on": (sent + timedelta(days=21)).strftime("%d %B %Y"),
        "confirmed": bool(confirmed),
        "upfront": pounds(opportunity.quoted_upfront_pence) if confirmed else "",
        "monthly": pounds(opportunity.quoted_monthly_pence) if confirmed else "",
        "band": band,
        "sender_name": sender_name or str(settings.get("owner_name") or "Hello New Home"),
    }
    return Email(subject=_subjects[template_key].render(ctx).strip(), body=_bodies[template_key].render(ctx))


# ---- SMTP ----------------------------------------------------------------------------------------------------
def smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.MAIL_FROM)


class MailNotConfigured(RuntimeError):
    pass


def send_email(to: str, subject: str, body: str, *, reply_to: str = "") -> str:
    """Send one plain-text email and return its Message-Id. Raises MailNotConfigured when SMTP_HOST or
    MAIL_FROM is unset and smtplib.SMTPException (or OSError) when the server refuses; the caller records
    the failure. The password is used for login only and never logged."""
    if not smtp_configured():
        raise MailNotConfigured("SMTP_HOST and MAIL_FROM are not configured; copy the text into your mail client instead.")
    message = EmailMessage()
    message["From"] = config.MAIL_FROM
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=config.MAIL_FROM.split("@")[-1] if "@" in config.MAIL_FROM else None)
    message["Message-ID"] = message_id
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
        if config.SMTP_STARTTLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(message)
    return message_id
