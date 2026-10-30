"""The new-appointee card: who joins or leaves, from the filing's own words.

For a management_change filing of tier cxo or director, the `lite` model reads the attachment text
and returns, for each person named, {name, role, change (joins|leaves), effective_date,
brief_profile}. Every field carries a verbatim quote, and a field survives only if its quote is in
the text (quote_match >= 0.4); one retry when anything fails, then the failing fields are dropped
(CLAUDE.md rules 3 and 5). A person whose name does not survive is dropped.

Only the filing's own brief-profile text is shown: the card keeps the quote, copied from the filing,
and only when it matches the text at PROFILE_MATCH or better. The model's wording is never shown.
Dates are parsed in code from the filing's own words.

LinkedIn: one Tavily search "<name> <company> LinkedIn" per person who joins (not a re-appointment),
cached so the same person is never searched twice; the first linkedin.com/in/ result is kept and
nothing else. It is dropped if its address does not carry the person's name (the first hit for a
common name is often someone else). The profile itself is never fetched or scraped, and a kept link
is a search hit, not a verified match.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx
from pydantic import BaseModel

from finn.enrich.pdf import pdf_excerpt
from finn.llm.router import MemoryStore, Router, Store, default_router
from finn.normalise.periods import PeriodError, parse_date_text
from finn.normalise.quote import THRESHOLD, quote_match

log = logging.getLogger(__name__)

STEP = "people"
PDF_PAGES = 6
MAX_OUT = 4096  # a copied profile is long; a cut-off reply is not valid JSON
TEXT_CHARS = 14000
PROFILE_MATCH = 0.8  # the shown profile text must be (nearly) word for word the filing's own
FIELDS = ("name", "role", "change", "effective_date", "brief_profile")
HONORIFICS = re.compile(r"^(?:mr|mrs|ms|miss|dr|shri|smt|prof|sri)\.?\s+", re.IGNORECASE)
COMPANY_SUFFIX = re.compile(r"\s+(?:limited|ltd\.?|corporation)\s*$", re.IGNORECASE)
REAPPOINT = re.compile(r"re-?\s?appoint", re.IGNORECASE)
LINKEDIN = re.compile(r"^https?://(?:[a-z0-9-]+\.)?linkedin\.com/in/[^/?#\s]+", re.IGNORECASE)


# -- extraction ------------------------------------------------------------------------------


class Field(BaseModel):
    value: str | None = None
    quote: str | None = None


class PersonOut(BaseModel):
    name: Field = Field()
    role: Field = Field()
    change: Field = Field()
    effective_date: Field = Field()
    brief_profile: Field = Field()


class PeopleOut(BaseModel):
    people: list[PersonOut] = []


def build_prompt(subject: str, text: str, nudge: str = "") -> str:
    return (
        "A company filed this notice about a change in its management or board. Use only the "
        "provided text. Do not use outside knowledge.\n\n"
        "List every person whose appointment, re-appointment, resignation, retirement or "
        "cessation the notice reports, one entry per person. For each person give these fields, "
        "each as {\"value\": ..., \"quote\": ...} where the quote is copied word for word from "
        "the text:\n"
        "- name: the person's name as written\n"
        "- role: the position, as written\n"
        '- change: "joins" if they are appointed or take the role, "leaves" if they resign, '
        "retire, finish a tenure or cease\n"
        "- effective_date: the date the change takes effect, as written in the text\n"
        "- brief_profile: only if the notice itself contains a brief profile of the person "
        "(education, career): put that profile, copied word for word, in quote and leave value "
        "null. Never write a profile yourself.\n"
        "Set both value and quote to null for a field the text does not give.\n\n"
        f"Subject: {subject}\n"
        f"Text:\n{text[:TEXT_CHARS]}\n\n"
        'Answer as JSON: {"people": [{"name": {...}, "role": {...}, "change": {...}, '
        '"effective_date": {...}, "brief_profile": {...}}]}.'
        f"{nudge}"
    )


@dataclass
class Person:
    name: str
    role: str | None = None
    change: Literal["joins", "leaves"] | None = None
    effective_date: str | None = None  # ISO date, parsed in code from the filing's own words
    brief_profile: str | None = None  # the filing's own text, quoted
    evidence: dict[str, dict] = field(default_factory=dict)  # field -> {quote, quote_match}
    other_roles: list[str] = field(default_factory=list)  # same person named under another role
    reappointment: bool = False  # the change quote says re-appointment: not a new face
    linkedin_url: str | None = None
    linkedin_state: str = "not_searched"  # found | none | pending | no_api_key | not_searched

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "change": self.change,
            "effective_date": self.effective_date,
            "other_roles": self.other_roles,
            "reappointment": self.reappointment,
            "brief_profile": self.brief_profile,
            "linkedin_url": self.linkedin_url,
            "linkedin_state": self.linkedin_state,
            "evidence": self.evidence,
        }


def _clean(person: PersonOut, source: str, dropped: list[str]) -> Person | None:
    """Keep each field whose quote is in the text and whose value is in its quote."""
    kept: dict[str, tuple[str, float]] = {}
    for name in FIELDS:
        f: Field = getattr(person, name)
        if not f.quote or (not f.value and name != "brief_profile"):
            continue
        match = quote_match(f.quote, source)
        if match < THRESHOLD:
            dropped.append(f"{name}: quote_match {match:.2f}")
            continue
        if name == "brief_profile":
            if match < PROFILE_MATCH:
                dropped.append(f"brief_profile: quote_match {match:.2f} under {PROFILE_MATCH}")
                continue
            kept[name] = (f.quote.strip(), match)
            continue
        if name == "change":
            if (f.value or "").strip().lower() not in ("joins", "leaves"):
                dropped.append(f"change: {f.value!r}")
                continue
            kept[name] = (f.value.strip().lower(), match)
            continue
        if quote_match(f.value, f.quote) < 1.0:  # the value has to be inside its own quote
            dropped.append(f"{name}: value not in its quote")
            continue
        kept[name] = (f.value.strip(), match)
    if "name" not in kept:
        dropped.append("person: no usable name")
        return None
    if "change" not in kept:  # named in passing (e.g. a contact person): not a joiner or leaver
        dropped.append(f"person {kept['name'][0]}: no checked joins/leaves")
        return None
    out = Person(kept["name"][0])
    out.evidence = {k: {"quote": getattr(person, k).quote, "quote_match": round(m, 4)}
                    for k, (_, m) in kept.items()}  # fmt: skip
    out.role = kept.get("role", (None,))[0]
    out.change = kept.get("change", (None,))[0]
    out.reappointment = bool(
        out.change == "joins" and REAPPOINT.search(person.change.quote or "")
    )
    out.brief_profile = kept.get("brief_profile", (None,))[0]
    if "effective_date" in kept:
        try:
            out.effective_date = parse_date_text(kept["effective_date"][0]).isoformat()
        except PeriodError:
            dropped.append("effective_date: not a date")
    return out


def extract_people(subject: str, text: str, router: Router) -> tuple[list[Person], list[str]]:
    """The people named in the filing, checked. One retry when any field is dropped."""
    source = f"{subject}\n{text}"
    best: tuple[list[Person], list[str]] = ([], ["no answer"])
    nudge = ""
    for _attempt in (1, 2):
        prompt = build_prompt(subject, text, nudge)
        out = router.call(STEP, "lite", prompt, PeopleOut, max_output_tokens=MAX_OUT).data
        dropped: list[str] = []
        people = [p for raw in out.people if (p := _clean(raw, source, dropped))]
        if _score(people) > _score(best[0]) or not best[0]:
            best = (people, dropped)
        if not dropped:
            break
        nudge = (
            "\n\nSome fields were rejected: every quote must be copied word for word from the "
            "text above and the value must appear inside its quote."
        )
    return best


def _score(people: list[Person]) -> int:
    return sum(len(p.evidence) for p in people)


def merge_same_person(people: list[Person]) -> list[Person]:
    """One entry per person and change: a person named twice (Additional Director now, MD & CEO
    from a later date) keeps the entry with more checked fields and lists the other role."""
    out: dict[tuple, Person] = {}
    for p in people:
        key = (" ".join(re.split(r"[^a-z]+", HONORIFICS.sub("", p.name).lower())).strip(), p.change)
        old = out.get(key)
        if old is None:
            out[key] = p
            continue
        keep, other = (p, old) if len(p.evidence) > len(old.evidence) else (old, p)
        if other.role and other.role != keep.role:
            keep.other_roles.append(other.role)
        out[key] = keep
    return list(out.values())


# -- Tavily ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkedIn:
    url: str | None
    state: str  # found | none | pending | no_api_key
    cached: bool = False


class Tavily:
    """One search per person, results cached in the `cache` table (or memory)."""

    URL = "https://api.tavily.com/search"

    def __init__(
        self,
        store: Store | None = None,
        client: httpx.Client | None = None,
        env: dict | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store or MemoryStore()
        self.client = client or httpx.Client(timeout=30)
        self.env = os.environ if env is None else env
        self.sleep = sleep
        self.searches = 0

    @staticmethod
    def query(name: str, company: str) -> str:
        person = HONORIFICS.sub("", name.strip())
        return f"{person} {COMPANY_SUFFIX.sub('', company.strip())} LinkedIn"

    @staticmethod
    def cache_key(query: str) -> str:
        return "tavily:" + hashlib.sha256(query.lower().encode("utf-8")).hexdigest()

    def linkedin(self, name: str, company: str) -> LinkedIn:
        query = self.query(name, company)
        key = self.cache_key(query)
        hit = self.store.get(key)
        if hit is not None:
            return self._answer(name, hit.get("url"), True)
        api_key = self.env.get("TAVILY_API_KEY")
        if not api_key:
            return LinkedIn(None, "no_api_key")
        results = self._search(query, api_key)
        if results is None:  # rate limited or unwell: not cached, so a later run tries again
            return LinkedIn(None, "pending")
        url = next((m.group(0) for r in results if (m := LINKEDIN.match(r.get("url") or ""))), None)
        self.store.put(key, {"query": query, "url": url, "results_seen": len(results)})
        return self._answer(name, url, False)

    @staticmethod
    def _answer(name: str, url: str | None, cached: bool) -> LinkedIn:
        if url and not slug_matches(url, name):
            return LinkedIn(None, "none", cached)  # the first hit is not this person
        return LinkedIn(url, "found" if url else "none", cached)

    def _search(self, query: str, api_key: str) -> list[dict] | None:
        body = {"query": query, "max_results": 5, "search_depth": "basic",
                "include_domains": ["linkedin.com"]}  # fmt: skip
        for attempt in (1, 2):
            self.searches += 1
            try:
                resp = self.client.post(
                    self.URL, json=body, headers={"Authorization": f"Bearer {api_key}"}
                )
            except httpx.HTTPError as exc:
                log.warning("tavily request failed: %s", type(exc).__name__)
                return None
            if resp.status_code == 200:
                try:
                    return list(resp.json().get("results") or [])
                except ValueError:
                    return None
            if resp.status_code == 429 and attempt == 1:
                self.sleep(_retry_after(resp))
                continue
            log.warning("tavily answered HTTP %s", resp.status_code)
            return None
        return None


def slug_matches(url: str, name: str) -> bool:
    """Does the profile address carry the person's name? The first hit for a common name is often
    someone else (V. N. Srivatsan -> vinod-sp-...); such a hit is not kept. The longest word of the
    name (the surname, or the first name when that is longer) has to appear in the address."""
    slug = url.rsplit("/in/", 1)[-1].lower()
    words = [w for w in re.split(r"[^a-z]+", HONORIFICS.sub("", name).lower()) if len(w) >= 3]
    if not words:
        return False
    key = max(reversed(words), key=len)
    return key in slug


def _retry_after(resp: httpx.Response, cap: float = 20.0) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "5")), cap)
    except ValueError:
        return 5.0


# -- the card --------------------------------------------------------------------------------


@dataclass
class PeopleCard:
    fragment: dict
    facts: list = field(default_factory=list)  # always empty: a people card shows no figures
    figures: dict[str, str] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.fragment["state"]


@dataclass
class Deps:
    router: Router | None = None
    tavily: Tavily | None = None
    pdf: Callable = pdf_excerpt


def build_people_card(row: dict, company: str, deps: Deps | None = None) -> PeopleCard:
    """The people card for one management_change filing of tier cxo or director."""
    deps = deps or Deps()
    subject = row.get("subject") or ""
    url = row.get("attachment_url")
    excerpt = deps.pdf(url, PDF_PAGES) if url else None
    text = excerpt.text if excerpt else ""
    base = {"card_type": "people", "symbol": row["symbol"], "company": company, "source_url": url}
    if not text.strip():
        return PeopleCard({**base, "state": "check_source", "reason": "no attachment text",
                           "people": []})  # fmt: skip
    router = deps.router or default_router()
    people, dropped = extract_people(subject, text, router)
    people = merge_same_person(people)
    if not people:
        return PeopleCard({**base, "state": "check_source", "reason": "no person passed the quote "
                           "check", "dropped": dropped, "people": []})  # fmt: skip
    tavily = deps.tavily or Tavily()
    for p in people:
        if p.change == "joins" and not p.reappointment:  # a new appointee, not a re-appointment
            found = tavily.linkedin(p.name, company)
            p.linkedin_url, p.linkedin_state = found.url, found.state
    figures = {}
    for p in people:
        when = f"effective {p.effective_date}" if p.effective_date else None
        detail = ", ".join(x for x in (p.role, when) if x)
        label = f"{p.name} {'re-appointed' if p.reappointment else p.change or 'named'}"
        figures[label] = detail or "no role given"
    return PeopleCard(
        {**base, "state": "ok", "people": [p.as_json() for p in people], "dropped": dropped},
        figures=figures,
    )
