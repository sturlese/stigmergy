#!/usr/bin/env python3
"""Write the long sources of `evals/graph_corpus` from fixed seeds (spec retrieval-and-curation §7).

Each source is ordinary filler with planted details at fixed positions and, near some of them, a
distractor that shares their subject but not their answer. `evals/graph_detail_golden.json` asks
about every planted detail. Running this again rewrites the same bytes.

  python evals/make_long_sources.py
"""
from __future__ import annotations

import hashlib
import random
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "graph_corpus"


def _page(title: str, text: str, *, capture: uuid.UUID, captured_at: str, occurred_at: str,
          acl: str | None) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()
    size = len(text.encode())
    audience = f"acl:\n- {acl}\n" if acl else ""
    occurred = f"occurred_at: '{occurred_at}'\n" if occurred_at else ""
    return f"""---
id: {capture}
type: source
submitted_by: graph-corpus
{audience}captured_at: '{captured_at}'
{occurred}origin: mcp
title: {title}
participants: []
artifacts:
- sha256: {digest}
  bytes: {size}
  media_type: text/markdown
  original_name: null
  source_url: null
  readable_sha256: {digest}
  extractor: graph-corpus
  extractor_version: '1'
  ocr_pages: []
---

# {title}

## Artifact 1: artifact-1

- Media type: text/markdown
- Bytes: {size}
- SHA-256: {digest}
- Extraction: graph-corpus@1

### Readable content

{text}"""


def _write(relative_folder: str, capture: uuid.UUID, body: str) -> Path:
    path = ROOT / relative_folder / f"{capture}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def all_hands() -> Path:
    title = "Tidewell all-hands transcript, August 2026"
    rng = random.Random(20260827)
    speakers = ["Priya Raman", "Rafael Duarte", "Hannah Brandt", "Tomasz Zielinski", "Ines Okafor", "Dmitri Volkov",
                "Aurélie Fontaine", "Amara Nwosu", "Lukas Becker", "Maren Vogt"]
    subjects = ["the dwell-time dashboard", "probe calibration audits", "the Sentinel gateway rollout",
                "route planning for the summer peak", "the customer advisory board follow-ups",
                "alert fatigue on night shifts", "the SOC 2 evidence binder", "hiring for the Hamburg office",
                "the per-asset pricing migration", "excursion budget reports", "the Frostvale renewal terms",
                "temperature mapping workshops", "driver onboarding", "the analytics store backups",
                "support ticket backlog", "the Casa Verde rollout", "spare parts inventory", "the quarterly roadmap",
                "trailer telemetry gaps", "the partner integration queue"]
    verbs = ["walked through", "gave an update on", "raised a concern about", "asked for help with",
             "summarised the status of", "proposed a change to", "shared numbers on", "flagged a risk in",
             "closed the loop on", "reviewed the plan for"]
    outcomes = ["the team agreed to revisit it at the next planning session", "nobody objected and the plan stands",
                "two people volunteered to pair on it this week",
                "the owner will post a written summary in the channel",
                "it moves to the backlog until after the peak", "the decision waits for more data from the pilots",
                "the next checkpoint is in two weeks", "a short design note will follow",
                "the metric goes on the weekly report", "customers will hear about it in the newsletter"]
    planted = {
        0.08: "Tomasz Zielinski: the Gdansk depot switches its night shift to four-hour handovers starting on 14 "
              "September, because the old eight-hour handover kept losing trailer notes.",
        0.17: "Rafael Duarte: Sentinel firmware 3.4.1 fixed the battery drain we saw on reefer trailers running "
              "sub-zero routes; field units drew 40 percent less current overnight.",
        0.26: "Hannah Brandt: the Lisbon warehouse lease is signed and runs until March 2029 at 41,000 euros a year, "
              "with a break option after the second year.",
        0.35: "Amara Nwosu: after the August release the Coldpath mobile app crash rate fell to 0.3 percent of "
              "sessions, down from 2.1 percent in July.",
        0.44: "Aurélie Fontaine: customer support now runs a follow-the-sun on-call rotation, and overnight European "
              "hours are covered by our Manila partner Brightdesk.",
        0.53: "Ines Okafor: the quarterly hackathon prize went to the team that built a reefer door-open detector "
              "from the accelerometer data the gateways already collect.",
        0.62: "Lukas Becker: procurement capped spot purchases of temperature probes at 2,500 units a month until "
              "the calibration backlog clears.",
        0.71: "Priya Raman: the Valencia pilot customer, Fruteria Albufera, asked for Spanish-language alert texts "
              "by the end of October.",
        0.80: "Hannah Brandt: insurance premiums for the truck fleet rose 7 percent at the August renewal, mostly "
              "because of two cargo claims in the spring.",
        0.92: "Dmitri Volkov: the data team retired the legacy read replica called coldpath-ro-2 on 2 August, after "
              "moving its last dashboards to the analytics store.",
    }
    lines = []
    total = 1150
    marks = {int(position * total): text for position, text in planted.items()}
    for index in range(total):
        if index in marks:
            lines.append(marks[index])
        speaker = rng.choice(speakers)
        lines.append(f"{speaker}: I {rng.choice(verbs)} {rng.choice(subjects)}, item {index}, "
                     f"and {rng.choice(outcomes)}.")
    text = ("All-hands meeting on 27 August 2026, recorded and transcribed for the team. "
            "Every team lead gave a short update; the transcript keeps each remark in order.\n\n"
            + "\n".join(lines) + "\n")
    capture = uuid.uuid5(uuid.NAMESPACE_URL, "stigmergy-graph-corpus:all-hands-2026-08")
    page = _page(title, text, capture=capture, captured_at="2026-08-27T18:00:00+00:00",
                 occurred_at="2026-08-27", acl=None)
    return _write("sources/2026/08", capture, page)


def _remarks(rng: random.Random, speakers: list[str], openers: list[str], subjects: list[str],
             details: list[str], closers: list[str], total: int, planted: dict[float, str]) -> list[str]:
    """`total` filler remarks with each planted line inserted at its fraction of the way through."""
    marks = {int(position * total): text for position, text in planted.items()}
    lines = []
    for index in range(total):
        if index in marks:
            lines.append(marks[index])
        lines.append(f"{rng.choice(speakers)}: {rng.choice(openers)} {rng.choice(subjects)}; "
                     f"{rng.choice(details)}, and {rng.choice(closers)}.")
    return lines


def advisory_board() -> Path:
    title = "Customer advisory board session, September 2026"
    rng = random.Random(20260916)
    speakers = ["Priya Raman", "Amara Nwosu", "Rafael Duarte", "Ines Okafor", "Hannah Brandt",
                "Marta Soler (Casa Verde Markets)", "Julien Marchetti (Altamira Pharma Logistics)",
                "Pieter Hendriks (Frostvale Distribution)", "Siobhan Keane (Glenmore Dairies)",
                "Oskar Berglund (Nordkyl Logistik)"]
    openers = ["on the agenda item about", "picking up the earlier point on", "from our side, regarding",
               "a quick reaction to the slide on", "speaking for our planners about", "to add context on",
               "we compared notes internally on", "the question from our operations team was about"]
    subjects = ["alert routing between depots", "the monthly excursion summary", "driver feedback in the app",
                "how the dashboard groups trailers", "data exports for auditors", "pallet labels and scanning",
                "the onboarding checklist for new sites", "notification fatigue at weekends",
                "integration with our transport planning tool", "temperature mapping before peak season",
                "how incidents are closed out", "the cadence of these board sessions",
                "spare gateway stock at our depots", "training for warehouse staff", "invoice detail per asset"]
    details = ["the group discussed it for a few minutes", "two members shared similar experiences",
               "the slides already covered most of it", "the Tidewell team took notes for the follow-up list",
               "opinions differed between the dairy and pharma members", "a member asked to see real examples",
               "the discussion stayed at a high level", "someone suggested a smaller working group"]
    closers = ["it goes on the follow-up list", "nobody asked for a change yet", "a written answer will come by email",
               "it will come back at the next session", "the product team will look at the data first",
               "members will send examples after the meeting", "it stays as it is for now",
               "a short survey will go out next month"]
    planted = {
        0.06: "Marta Soler (Casa Verde Markets): our Murcia hub wants dwell alerts after 20 minutes at the dock "
              "instead of the default.",
        0.09: "Marta Soler (Casa Verde Markets): the Valencia hub keeps its current dock threshold for now.",
        0.13: "Julien Marchetti (Altamira Pharma Logistics): we need the excursion report within two hours of a breach "
              "for any shipment carrying insulin.",
        0.19: "Pieter Hendriks (Frostvale Distribution): Frostvale will move 60 trailers from the Eindhoven yard onto "
              "Sentinel gateways in the first quarter of 2027.",
        0.22: "Pieter Hendriks (Frostvale Distribution): the Antwerp yard stays on the older loggers until its "
              "lease ends.",
        0.26: "Siobhan Keane (Glenmore Dairies): Glenmore asked for a milk-tanker template where the alarm band is "
              "2 to 6 degrees instead of the standard 2 to 8.",
        0.32: "Priya Raman: the advisory board voted the shared excursion dashboard as the top roadmap item for the "
              "first half of 2027, with 9 of 12 votes.",
        0.39: "Oskar Berglund (Nordkyl Logistik): Nordkyl will pay for its own SIM contracts because Swedish roaming "
              "through your carrier costs us 30 percent more.",
        0.45: "Amara Nwosu: the Coldpath app gets an offline mode for drivers in tunnels and rural areas in the "
              "November release.",
        0.52: "Julien Marchetti (Altamira Pharma Logistics): Altamira's auditors want the probe calibration "
              "certificates kept for seven years, not five.",
        0.55: "Siobhan Keane (Glenmore Dairies): our regulator is content with five years of calibration records.",
        0.58: "Rafael Duarte: the gateway battery now lasts 19 days on a single charge in unpowered trailers, up "
              "from 11.",
        0.65: "Marta Soler (Casa Verde Markets): Casa Verde will pilot reusable insulated crates in 16 stores around "
              "Valencia starting in January.",
        0.72: "Hannah Brandt: customers who sign a three-year renewal before December get the excursion-insurance "
              "add-on free for the first year.",
        0.79: "Oskar Berglund (Nordkyl Logistik): Nordkyl wants alert texts in Swedish and Finnish, Finnish first "
              "because of the Turku depot.",
        0.86: "Ines Okafor: the next advisory board session will be held in Porto on 18 March 2027.",
    }
    lines = _remarks(rng, speakers, openers, subjects, details, closers, 620, planted)
    text = ("Customer advisory board, 16 September 2026, held online. Five customer members and the Tidewell product "
            "and operations leads attended; the transcript keeps each remark in order.\n\n" + "\n".join(lines) + "\n")
    capture = uuid.uuid5(uuid.NAMESPACE_URL, "stigmergy-graph-corpus:advisory-board-2026-09")
    page = _page(title, text, capture=capture, captured_at="2026-09-16T19:00:00+00:00",
                 occurred_at="2026-09-16", acl=None)
    return _write("sources/2026/09", capture, page)


def operations_review() -> Path:
    title = "Tidewell operations review, second quarter 2026"
    rng = random.Random(20260708)
    sections = {
        "Summary": ["on-time delivery", "the excursion trend", "customer growth", "the quarter's main incidents"],
        "Fleet and telemetry": ["reefer maintenance", "gateway uptime", "route planning", "trailer age",
                                "fuel use", "telemetry gaps"],
        "Warehousing": ["cross-dock throughput", "cold room qualification", "pallet dwell", "mapping studies",
                        "dock scheduling"],
        "Customer support": ["ticket volume", "first-response time", "the overnight rotation", "escalations",
                             "the support knowledge base"],
        "Engineering": ["deploy frequency", "the analytics store", "incident reviews", "the mobile app",
                        "infrastructure costs"],
        "People": ["hiring", "attrition", "training hours", "the Hamburg office"],
        "Risks": ["supplier concentration", "licences and permits", "seasonal peaks", "key-person dependencies"],
    }
    openers = ["In the quarter,", "Compared with the first quarter,", "Across all sites,", "As planned,",
               "Against the targets set in January,", "For most customers,"]
    middles = ["work on {} continued without major changes", "the team kept a close eye on {}",
               "{} stayed within the expected range", "reporting on {} moved to the weekly review",
               "{} needed more attention than planned", "a small project improved {}"]
    endings = ["The next review will check whether this holds through the summer peak.",
               "No customer raised it as a problem.", "The owners documented the steps in the runbook.",
               "Further detail sits in the team dashboards.", "It remains on the watch list.",
               "The numbers will be refreshed in the third-quarter review."]
    planted = {
        "Summary": ["On-time delivery for the quarter was 96.8 percent across all customers."],
        "Fleet and telemetry": [
            "In the second quarter the fleet logged 412 temperature excursions, of which 37 lasted longer than thirty "
            "minutes.",
            "Night shifts in the first quarter had recorded most of the long excursions.",
            "Reefer fuel use per 100 kilometres fell to 31.4 litres after the route planner began avoiding the A7 "
            "detour.",
            "Average trailer age is 6.2 years; the 22 oldest trailers are scheduled for replacement in 2027.",
        ],
        "Warehousing": [
            "The Rotterdam cross-dock moved to a second shift on 3 May, which cut average pallet dwell from 6.1 to "
            "3.8 hours.",
            "Cold room 4 in Hamburg failed its mapping study in April and was re-qualified after the evaporator fans "
            "were replaced.",
            "Cold room 2 in Hamburg passed its mapping study on the first attempt.",
        ],
        "Customer support": [
            "First-response time on priority tickets averaged 11 minutes in June, against a target of 15.",
            "Brightdesk handled 2,940 overnight tickets in the quarter, 18 percent of the total.",
        ],
        "Engineering": [
            "The analytics store migration finished on 21 June, and query costs dropped by 27 percent.",
            "Deploys to production went from weekly to daily in May, with a change failure rate of 4 percent.",
        ],
        "People": [
            "Voluntary attrition was 3 percent for the half year, the lowest since 2023.",
        ],
        "Risks": [
            "The largest open risk is the single supplier for the Sentinel gateway modem, Kestrel Radio, with 9 weeks "
            "of stock on hand.",
            "A second risk is the Portuguese carrier licence, which must be renewed by 30 November or the Lisbon "
            "routes stop.",
        ],
    }
    parts = ["Quarterly operations review for April to June 2026, prepared by the operations leads for the "
             "management team on 8 July 2026."]
    for heading, topics in sections.items():
        sentences = []
        for _ in range(150):
            sentences.append(f"{rng.choice(openers)} {rng.choice(middles).format(rng.choice(topics))}. "
                             f"{rng.choice(endings)}")
        for offset, fact in enumerate(planted[heading]):
            sentences.insert(int((offset + 1) * len(sentences) / (len(planted[heading]) + 1)), fact)
        paragraphs = [" ".join(sentences[start:start + 6]) for start in range(0, len(sentences), 6)]
        parts.append(f"## {heading}\n\n" + "\n\n".join(paragraphs))
    text = "\n\n".join(parts) + "\n"
    capture = uuid.uuid5(uuid.NAMESPACE_URL, "stigmergy-graph-corpus:operations-review-2026-q2")
    page = _page(title, text, capture=capture, captured_at="2026-07-08T17:00:00+00:00",
                 occurred_at="2026-07-08", acl=None)
    return _write("sources/2026/07", capture, page)


def due_diligence() -> Path:
    title = "Series A due diligence call, August 2026"
    rng = random.Random(20260819)
    speakers = ["Hannah Brandt", "Ines Okafor", "Julian Ashworth (Seabright Partners)",
                "Clara Whitfield (Seabright Partners)", "Mei Tanaka (Oxbow Ventures)"]
    openers = ["on the data room folder for", "coming back to", "a follow-up question on", "for the record, on",
               "we reviewed the material about", "one more point about"]
    subjects = ["the customer contracts", "the cap table", "the hiring plan", "the product roadmap",
                "the cloud costs", "the board minutes", "employee agreements", "the IP assignments",
                "the security policies", "the revenue recognition policy", "the supplier contracts"]
    details = ["the documents are in the data room", "the summary matches what we saw last week",
               "we need one more signed copy", "the numbers reconcile with the management accounts",
               "the lawyers will review the wording", "there is nothing unusual in it"]
    closers = ["we can tick it off", "the open question goes on the tracker", "we will confirm by email",
               "it will be covered in the next call", "no action is needed from Tidewell",
               "the analyst will take a closer look"]
    planted = {
        0.07: "Julian Ashworth (Seabright Partners): our diligence team wants the top three customers below 30 "
              "percent of revenue combined by closing.",
        0.14: "Hannah Brandt: gross margin on the per-asset plans was 64 percent in July.",
        0.21: "Hannah Brandt: monthly burn is 190,000 euros, which gives us 11 months of runway before the round.",
        0.24: "Hannah Brandt: burn was higher in the spring because of the Hamburg hiring.",
        0.29: "Julian Ashworth (Seabright Partners): our technical diligence will be run by an outside firm, Ardent "
              "Systems Review.",
        0.36: "Ines Okafor: the option pool will be topped up to 12 percent before the round closes.",
        0.43: "Hannah Brandt: net revenue retention over the last twelve months is 118 percent.",
        0.50: "Julian Ashworth (Seabright Partners): we will ask for a seat on the audit committee as well as the "
              "board seat.",
        0.57: "Hannah Brandt: the two largest receivables, both from Altamira, total 380,000 euros and are due in "
              "October.",
        0.64: "Ines Okafor: the Hamburg office lease has a change-of-control clause that needs the landlord's "
              "consent.",
        0.67: "Ines Okafor: the Lisbon warehouse lease has no change-of-control clause.",
        0.71: "Julian Ashworth (Seabright Partners): legal diligence must finish by 10 October for a closing in "
              "November.",
        0.78: "Hannah Brandt: we carry 1.1 million euros of venture debt from Tern Bank, repayable from 2027.",
        0.85: "Ines Okafor: no founder will sell secondary shares in this round.",
        0.92: "Hannah Brandt: the SOC 2 Type II auditors finish their fieldwork on 24 September.",
    }
    lines = _remarks(rng, speakers, openers, subjects, details, closers, 360, planted)
    text = ("Due diligence call with Seabright Partners on 19 August 2026. Confidential to the finance layer; the "
            "transcript keeps each remark in order.\n\n" + "\n".join(lines) + "\n")
    capture = uuid.uuid5(uuid.NAMESPACE_URL, "stigmergy-graph-corpus:due-diligence-2026-08")
    return _write("layers/finance/sources/2026/08", capture,
                  _page(title, text, capture=capture, captured_at="2026-08-19T18:00:00+00:00", occurred_at="2026-08-19",
                        acl="finance"))


if __name__ == "__main__":
    for make in (all_hands, advisory_board, operations_review, due_diligence):
        path = make()
        words = len(path.read_text(encoding="utf-8").split())
        print(f"{path.relative_to(ROOT)}  {words} words")
