"""Load CozySetup's business information from config/business.toml.

The rest of the system never reads the TOML file directly. It calls
load_business_info(), which either returns a complete BusinessInfo object
or stops with a list of every problem it found in the file.
"""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "business.toml"

REQUIRED_POLICIES = ("cancellation", "rescheduling", "weather", "security_deposit")

# {placeholders} filled in from the business info file. Any reply or policy may use these.
FILE_VALUES = frozenset({
    "base_price", "extra_hour_price", "security_deposit", "currency",
    "start_time", "end_time", "deposit_percent", "location_list", "wamd_details",
})

# Every reply the system sends, and the booking {placeholders} it may use on top
# of FILE_VALUES. A booking value only exists where the system has that booking
# information at hand - e.g. there is no {reference} yet when quoting a price.
REQUIRED_REPLIES = {
    "price": set(),
    "locations": set(),
    "extra_hours": set(),
    "booking_question": set(),
    "date_unavailable": {"date"},
    "payment_choice": {"rental_price", "deposit_amount", "remaining_amount"},
    "ask_contact": set(),
    "booking_summary": {"location", "date", "rental_price", "amount_now", "due_on_day", "name", "phone"},
    "booking_created": {"reference", "amount_now"},
    "screenshot_received": set(),
    "booking_confirmed": {"reference", "location", "date", "due_on_day"},
    "reminder": {"location", "due_on_day"},
    "due_on_day_after_deposit": {"remaining_amount"},
    "due_on_day_after_full_payment": set(),
    "handoff": set(),
    "same_day": set(),
    "date_conflict_status": set(),
}


class BusinessInfoError(Exception):
    """The business info file is missing, unreadable, or has mistakes."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        listed = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"The business info file has {len(problems)} problem(s):\n{listed}")


# --- The shape of the business information ---------------------------------
# frozen=True: once loaded, these values cannot be replaced by accident
# while the system is running.

@dataclass(frozen=True)
class Text:
    """Customer-facing wording. An empty `ar` means not provided yet."""
    en: str
    ar: str


@dataclass(frozen=True)
class Pricing:
    currency: str
    base_price: int
    start_time: time
    end_time: time
    extra_hour_price: int
    security_deposit: int


@dataclass(frozen=True)
class Payment:
    deposit_percent: int
    deposit_min_days_ahead: int
    wamd_details: str


@dataclass(frozen=True)
class Location:
    id: str
    name: Text
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class BusinessInfo:
    name: str
    timezone: ZoneInfo
    pricing: Pricing
    payment: Payment
    locations: tuple[Location, ...]
    included_items: tuple[Text, ...]
    policies: dict[str, Text]
    arabizi_rule: str
    replies: dict[str, Text]

    def warnings(self) -> list[str]:
        """Things that are allowed for now but still unfinished."""
        return _find_warnings(self)


# --- Loading ------------------------------------------------------------------

def load_business_info(path: Path = DEFAULT_PATH) -> BusinessInfo:
    """Read the business info file. Raises BusinessInfoError listing every problem."""
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise BusinessInfoError([f"File not found: {path}"]) from None
    except tomllib.TOMLDecodeError as error:
        raise BusinessInfoError([f"{Path(path).name} is not valid TOML: {error}"]) from None

    problems: list[str] = []
    info = _read_business_info(_Section(data, "", problems))
    if not problems:
        # Business rules can only be checked once every value has the right type.
        problems = _check_rules(info)
    if problems:
        raise BusinessInfoError(problems)
    return info


def _read_business_info(root: _Section) -> BusinessInfo:
    business = root.section("business")
    name = business.value("name", str)
    timezone = _read_timezone(business)
    business.done()

    pricing = root.section("pricing")
    pricing_info = Pricing(
        currency=pricing.value("currency", str),
        base_price=pricing.value("base_price", int),
        start_time=pricing.value("start_time", time),
        end_time=pricing.value("end_time", time),
        extra_hour_price=pricing.value("extra_hour_price", int),
        security_deposit=pricing.value("security_deposit", int),
    )
    pricing.done()

    payment = root.section("payment")
    wamd = payment.section("wamd")
    payment_info = Payment(
        deposit_percent=payment.value("deposit_percent", int),
        deposit_min_days_ahead=payment.value("deposit_min_days_ahead", int),
        wamd_details=wamd.value("details", str),
    )
    wamd.done()
    payment.done()

    locations = tuple(_read_location(item) for item in root.list_of_sections("locations"))

    setup = root.section("setup")
    included_items = tuple(_read_text(item) for item in setup.list_of_sections("included_items"))
    setup.done()

    policies = _read_texts(root.section("policies"))

    languages = root.section("languages")
    if languages.value("arabizi_source", str) not in (None, "ar"):
        languages.problem("'arabizi_source' must be \"ar\"")
    arabizi_rule = languages.value("arabizi_rule", str)
    languages.done()

    replies = _read_texts(root.section("replies"))
    root.done()

    return BusinessInfo(
        name=name,
        timezone=timezone,
        pricing=pricing_info,
        payment=payment_info,
        locations=locations,
        included_items=included_items,
        policies=policies,
        arabizi_rule=arabizi_rule,
        replies=replies,
    )


def _read_timezone(section: _Section) -> ZoneInfo | None:
    timezone_name = section.value("timezone", str)
    if timezone_name is None:
        return None
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        section.problem(f"unknown timezone {timezone_name!r} (expected a name like \"Asia/Kuwait\")")
        return None


def _read_location(section: _Section) -> Location:
    location = Location(
        id=section.value("id", str),
        name=_read_text(section.section("name")),
        aliases=tuple(section.list_of("aliases", str)),
    )
    section.done()
    return location


def _read_text(section: _Section) -> Text:
    text = Text(en=section.value("en", str), ar=section.value("ar", str))
    section.done()
    return text


def _read_texts(section: _Section) -> dict[str, Text]:
    """A section whose sub-sections are all wording, e.g. [policies] or [replies]."""
    texts = {key: _read_text(section.section(key)) for key in section.keys()}
    section.done()
    return texts


# --- Values for {placeholders} ------------------------------------------------------

def file_values(info: BusinessInfo) -> dict[str, object]:
    """The FILE_VALUES placeholders, ready to show to customers (English formatting)."""
    return {
        "base_price": info.pricing.base_price,
        "extra_hour_price": info.pricing.extra_hour_price,
        "security_deposit": info.pricing.security_deposit,
        "currency": info.pricing.currency,
        "start_time": format_time_en(info.pricing.start_time),
        "end_time": format_time_en(info.pricing.end_time),
        "deposit_percent": info.payment.deposit_percent,
        "location_list": " · ".join(location.name.en for location in info.locations),
        "wamd_details": info.payment.wamd_details,
    }


def format_time_en(value: time) -> str:
    """18:00 -> "6 PM", 23:30 -> "11:30 PM", 00:00 -> "12 AM"."""
    hour = value.hour % 12 or 12
    minutes = f":{value.minute:02d}" if value.minute else ""
    return f"{hour}{minutes} {'AM' if value.hour < 12 else 'PM'}"


# --- Business rules ---------------------------------------------------------------
# The file can have the right types and still make no business sense,
# e.g. a negative price or one alias shared by two locations.

def _check_rules(info: BusinessInfo) -> list[str]:
    problems: list[str] = []
    if not info.name.strip():
        problems.append("[business]: 'name' is empty")
    _check_pricing(info.pricing, problems)
    _check_payment(info.payment, problems)
    _check_locations(info.locations, problems)
    _check_included_items(info.included_items, problems)
    _check_policies(info.policies, problems)
    _check_replies(info.replies, problems)
    if not info.arabizi_rule.strip():
        problems.append("[languages]: 'arabizi_rule' is empty")
    return problems


def _check_pricing(pricing: Pricing, problems: list[str]) -> None:
    if not pricing.currency.strip():
        problems.append("[pricing]: 'currency' is empty")
    for field in ("base_price", "extra_hour_price", "security_deposit"):
        amount = getattr(pricing, field)
        if amount <= 0:
            problems.append(f"[pricing]: '{field}' must be more than 0, not {amount}")
    if pricing.start_time >= pricing.end_time:
        problems.append(
            f"[pricing]: 'start_time' ({pricing.start_time:%H:%M}) must be before "
            f"'end_time' ({pricing.end_time:%H:%M})"
        )


def _check_payment(payment: Payment, problems: list[str]) -> None:
    if not 1 <= payment.deposit_percent <= 99:
        problems.append(f"[payment]: 'deposit_percent' must be between 1 and 99, not {payment.deposit_percent}")
    if payment.deposit_min_days_ahead < 1:
        problems.append(
            f"[payment]: 'deposit_min_days_ahead' must be 1 or more, not {payment.deposit_min_days_ahead} "
            "(same-day bookings are handled by the owner)"
        )


_LOCATION_ID = re.compile(r"[a-z][a-z0-9_]*")


def _check_locations(locations: tuple[Location, ...], problems: list[str]) -> None:
    if not locations:
        problems.append("[[locations]]: there must be at least one location")

    seen_ids: set[str] = set()
    # Every way of naming a location (official names and aliases) -> the location id.
    # One name must never point to two different locations.
    name_owner: dict[str, str] = {}

    for number, location in enumerate(locations, start=1):
        where = f"[locations #{number}]"
        if not _LOCATION_ID.fullmatch(location.id):
            problems.append(
                f"{where}: id {location.id!r} must be lowercase letters, digits and _ "
                "(starting with a letter)"
            )
        if location.id in seen_ids:
            problems.append(f"{where}: id {location.id!r} is used by more than one location")
        seen_ids.add(location.id)

        for language in ("en", "ar"):
            if not getattr(location.name, language).strip():
                problems.append(f"{where} ({location.id}): official name '{language}' is empty")

        for name in (location.name.en, location.name.ar, *location.aliases):
            key = normalize_name(name)
            if not key:
                problems.append(f"{where} ({location.id}): has an empty alias")
                continue
            owner = name_owner.setdefault(key, location.id)
            if owner != location.id:
                problems.append(
                    f"{where} ({location.id}): the name {name!r} also belongs to location "
                    f"{owner!r} - customers writing it could mean either"
                )


def normalize_name(name: str) -> str:
    """How names are compared: ignoring upper/lower case and extra spaces."""
    return " ".join(name.casefold().split())


def _check_included_items(items: tuple[Text, ...], problems: list[str]) -> None:
    if not items:
        problems.append("[setup]: 'included_items' must list at least one item")
    for number, item in enumerate(items, start=1):
        if not item.en.strip():
            problems.append(f"[setup]: included item {number} has empty English wording")


def _check_policies(policies: dict[str, Text], problems: list[str]) -> None:
    for name in REQUIRED_POLICIES:
        if name not in policies:
            problems.append(f"[policies]: missing the '{name}' policy")
    for name, text in policies.items():
        _check_text(f"[policies.{name}]", text, FILE_VALUES, problems)


def _check_replies(replies: dict[str, Text], problems: list[str]) -> None:
    for name in REQUIRED_REPLIES:
        if name not in replies:
            problems.append(f"[replies]: missing the '{name}' reply")
    for name, text in replies.items():
        if name not in REQUIRED_REPLIES:
            problems.append(f"[replies.{name}]: unknown reply - the system never sends it (misspelled?)")
            continue
        _check_text(f"[replies.{name}]", text, FILE_VALUES | REQUIRED_REPLIES[name], problems)


def _check_text(where: str, text: Text, allowed: frozenset[str] | set[str], problems: list[str]) -> None:
    """English must be present; both languages may only use allowed {placeholders}."""
    if not text.en.strip():
        problems.append(f"{where}: 'en' is empty")
    for language in ("en", "ar"):
        wording = getattr(text, language)
        try:
            fields = [
                (name, spec, conversion)
                for _, name, spec, conversion in string.Formatter().parse(wording)
                if name is not None
            ]
        except ValueError:
            problems.append(f"{where}: '{language}' has a {{ or }} without its partner")
            continue
        for name, spec, conversion in fields:
            if name not in allowed:
                problems.append(f"{where}: '{language}' uses unknown placeholder {{{name}}}")
            elif spec or conversion:
                problems.append(f"{where}: '{language}' placeholder {{{name}}} must not contain ':' or '!'")


# --- Warnings ---------------------------------------------------------------------
# Not mistakes - the file is valid - but things the owner still has to provide.

def _find_warnings(info: BusinessInfo) -> list[str]:
    warnings: list[str] = []

    if not info.payment.wamd_details.strip():
        warnings.append(
            "[payment.wamd]: 'details' is empty - customers cannot be told where to pay. "
            "Must be filled in before real customers use the system."
        )

    groups = (
        ("included item", {item.en: item for item in info.included_items}),
        ("policy", info.policies),
        ("reply", info.replies),
    )
    for kind, texts in groups:
        missing = [name for name, text in texts.items() if not text.ar.strip()]
        if missing:
            warnings.append(
                f"No Arabic wording yet for {len(missing)} of {len(texts)} {kind} texts "
                f"(Arabic and Arabizi customers need these): {', '.join(missing)}"
            )

    # An Arabic version that drops or adds a {placeholder} would show the
    # customer different facts than the English version.
    for kind, texts in (("policies", info.policies), ("replies", info.replies)):
        for name, text in texts.items():
            if text.ar.strip() and _placeholder_names(text.ar) != _placeholder_names(text.en):
                warnings.append(
                    f"[{kind}.{name}]: the Arabic uses different {{placeholders}} than the English "
                    f"(English: {sorted(_placeholder_names(text.en))}, "
                    f"Arabic: {sorted(_placeholder_names(text.ar))})"
                )

    return warnings


def _placeholder_names(wording: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(wording) if name}


# --- Reading one section safely -----------------------------------------------

_KIND_NAMES = {
    str: "text in quotes",
    int: "a whole number",
    time: "a time like 18:00:00",
    list: "a list [ ... ]",
    dict: "a section",
}


def _is_kind(value: object, kind: type) -> bool:
    if kind is int:
        # In Python, true/false count as numbers - reject them here.
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


class _Section:
    """One section of the file.

    Reads values with the expected type, and records a problem - instead of
    crashing - for anything missing, of the wrong type, or unexpected.
    That way one run reports every mistake in the file, not just the first.
    """

    def __init__(self, data: dict, path: str, problems: list[str]):
        self.data = data
        self.path = path
        self.problems = problems
        self._read_keys: set[str] = set()

    @property
    def label(self) -> str:
        return f"[{self.path}]" if self.path else "The file"

    def problem(self, message: str) -> None:
        self.problems.append(f"{self.label}: {message}")

    def keys(self) -> list[str]:
        return list(self.data)

    def value(self, key: str, kind: type):
        self._read_keys.add(key)
        if key not in self.data:
            self.problem(f"missing '{key}'")
            return None
        value = self.data[key]
        if not _is_kind(value, kind):
            self.problem(f"'{key}' should be {_KIND_NAMES[kind]}, not {value!r}")
            return None
        return value

    def section(self, key: str) -> _Section:
        table = self.value(key, dict)
        child_path = f"{self.path}.{key}" if self.path else key
        if table is None:
            # Already reported. Give the child a throwaway problem list so a
            # missing section is reported once, not once per missing field.
            return _Section({}, child_path, [])
        return _Section(table, child_path, self.problems)

    def list_of(self, key: str, kind: type) -> list:
        items = self.value(key, list) or []
        for number, item in enumerate(items, start=1):
            if not _is_kind(item, kind):
                self.problem(f"'{key}' item {number} should be {_KIND_NAMES[kind]}, not {item!r}")
        return [item for item in items if _is_kind(item, kind)]

    def list_of_sections(self, key: str) -> list[_Section]:
        child_path = f"{self.path}.{key}" if self.path else key
        return [
            _Section(item, f"{child_path} #{number}", self.problems)
            for number, item in enumerate(self.list_of(key, dict), start=1)
        ]

    def done(self) -> None:
        """Report any keys that were never read - usually a typo or a value
        that ended up under the wrong [section]."""
        for key in self.data:
            if key not in self._read_keys:
                self.problem(f"unexpected '{key}' (misspelled, or under the wrong [section]?)")
