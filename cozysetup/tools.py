"""The 6 tools the AI employee can use, and the code that runs them.

Claude only *asks* to use a tool. The code here decides what happens, always
through the BookingService, so every booking rule applies. Tool results give
Claude the owner's exact reply text, so it never types facts itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from cozysetup.bookings import (
    BookingPreview,
    BookingRefused,
    BookingService,
    DateStatus,
    RefusalReason,
)
from cozysetup.business_info import BusinessInfo, format_date_en
from cozysetup.database import BookingStatus, HandoffType
from cozysetup.replies import amount_values, booking_values, location_name, render
from cozysetup.rules import PaymentChoice, format_kwd

# The AI may hand over any case except date conflicts, which only the system creates.
AI_HANDOFF_TYPES = [t.value for t in HandoffType if t is not HandoffType.DATE_CONFLICT]


# --- What the tools know about the current conversation ---------------------------

@dataclass
class Conversation:
    """Filled in by our code, never by Claude: who the customer is, and what they sent."""
    channel: str
    channel_user_id: str
    # Images the customer sent, numbered from 1. Claude never sees the images.
    attachments: dict[int, bytes] = field(default_factory=dict)
    # The booking details last shown to the customer as a summary.
    last_preview: BookingPreview | None = None

    def add_attachment(self, data: bytes) -> int:
        number = len(self.attachments) + 1
        self.attachments[number] = data
        return number


@dataclass(frozen=True)
class ToolResult:
    content: str       # JSON text given to Claude
    is_error: bool = False


# --- The tool descriptions Claude reads ------------------------------------------

def _strict_tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,  # the API guarantees the input matches this schema
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def _optional_text(description: str) -> dict:
    return {"anyOf": [{"type": "string"}, {"type": "null"}], "description": description}


DATE = {"type": "string", "format": "date", "description": "The booking date, YYYY-MM-DD (Kuwait calendar)."}
REFERENCE = {"type": "string", "description": "The booking reference the customer gave, e.g. CS-0001."}
PHONE = {"type": "string", "description": "The phone number exactly as the customer wrote it."}


def tool_definitions(info: BusinessInfo) -> list[dict]:
    """The same list, in the same order, every time - so it can be cached."""
    location_ids = [location.id for location in info.locations]
    booking_details = {
        "date": DATE,
        "location_id": {"type": "string", "enum": location_ids,
                        "description": "The location's id (aliases like Arifjan mean julaia)."},
        "customer_name": {"type": "string", "description": "The customer's name."},
        "customer_phone": PHONE,
        "payment_choice": {"type": "string", "enum": [c.value for c in PaymentChoice],
                           "description": "deposit = 50% now, full = 100% now. Must be one of "
                                          "the options check_availability returned."},
    }
    return [
        _strict_tool(
            "check_availability",
            "Check whether a date can be booked and what the customer would pay. Call this as soon "
            "as the customer gives a date, before saying anything about availability or price for it.",
            {"date": DATE},
        ),
        _strict_tool(
            "show_booking_summary",
            "Get the booking summary to send the customer, once you have their location, date, "
            "payment choice, name and phone. It only checks the details and returns the summary - "
            "it never creates or confirms anything.",
            booking_details,
        ),
        _strict_tool(
            "create_booking",
            "Create the booking - call it once, after the customer has said the summary from "
            "show_booking_summary is correct, with exactly the same details. The booking waits for "
            "payment (pending payment); only the owner can confirm it after checking the payment.",
            booking_details,
        ),
        _strict_tool(
            "attach_payment_proof",
            "Save a payment screenshot the customer sent for their booking. Call this when the "
            "customer sends an image after being asked to pay. You never judge the image.",
            {
                "reference": REFERENCE,
                "customer_phone": PHONE,
                "attachment_number": {"type": "integer",
                                      "description": "The number of the image, from '[Customer attached image #N]'."},
            },
        ),
        _strict_tool(
            "get_booking_status",
            "Look up an existing booking when the customer asks about it. Needs both the "
            "reference and the phone number used for the booking.",
            {"reference": REFERENCE, "customer_phone": PHONE},
        ),
        _strict_tool(
            "handoff_to_human",
            "Pass the case to the owner. Use it for same-day bookings, cancellations, rescheduling, "
            "weather, damage, payment problems, a customer asking for a person, or anything you are "
            "unsure about. Never guess instead.",
            {
                "type": {"type": "string", "enum": AI_HANDOFF_TYPES},
                "summary": {"type": "string",
                            "description": "What the customer wants, in English, in one or two sentences."},
                "customer_name": _optional_text("The customer's name, if known."),
                "customer_phone": _optional_text("The customer's phone, if known."),
                "booking_reference": _optional_text("The booking reference, if one is involved."),
            },
        ),
    ]


# --- Running the tools ------------------------------------------------------------------

# What Claude should do when the booking service refuses something.
REFUSAL_INSTRUCTIONS = {
    RefusalReason.PAST_DATE: "That date has passed. Ask the customer for another date.",
    RefusalReason.SAME_DAY: "Same-day bookings are handled by the owner: call handoff_to_human with type same_day.",
    RefusalReason.DATE_TAKEN: "This date is no longer available. Call check_availability for it to get "
                              "the exact reply to send, and do not continue the booking for this date.",
    RefusalReason.UNKNOWN_LOCATION: "Unknown location. Ask which of our locations they want.",
    RefusalReason.INVALID_NAME: "Ask the customer for their name again.",
    RefusalReason.INVALID_PHONE: "That phone number is not valid. Ask for it again; "
                                 "a non-Kuwaiti number needs its country code, e.g. +966.",
    RefusalReason.PAYMENT_CHOICE_NOT_ALLOWED: "That payment choice is not allowed for this date. "
                                              "Call check_availability to see the allowed options.",
    RefusalReason.BOOKING_NOT_FOUND: "No booking matches that reference and phone number. "
                                     "Ask the customer to check both.",
    RefusalReason.WRONG_STATUS: "This booking is not waiting for a payment screenshot. "
                                "Call get_booking_status to see its status.",
    RefusalReason.INVALID_FILE: "That file can't be accepted. Ask for a screenshot (JPG, PNG, HEIC, WEBP or PDF).",
}


class Tools:
    def __init__(self, service: BookingService, conversation: Conversation):
        self.service = service
        self.info = service.info
        self.conversation = conversation

    def run(self, name: str, tool_input: dict) -> ToolResult:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return _error({"error": f"Unknown tool {name!r}."})
        try:
            return handler(**tool_input)
        except BookingRefused as refused:
            return self._refusal(refused)
        except (TypeError, ValueError) as problem:
            return _error({"error": f"Invalid input: {problem}"})

    # --- The five tools ---------------------------------------------------------------

    def _tool_check_availability(self, date: str) -> ToolResult:
        booking_date = _parse_date(date)
        availability = self.service.check_availability(booking_date)
        result: dict[str, object] = {"date": format_date_en(booking_date), "status": availability.status.value}

        if availability.status is DateStatus.PAST:
            result["instruction"] = REFUSAL_INSTRUCTIONS[RefusalReason.PAST_DATE]
        elif availability.status is DateStatus.SAME_DAY:
            result["instruction"] = REFUSAL_INSTRUCTIONS[RefusalReason.SAME_DAY]
        elif availability.status is DateStatus.TAKEN:
            result["reply"] = render(self.info, "date_unavailable", {"date": format_date_en(booking_date)})
            result["instruction"] = "Send this reply. Do not continue the booking for this date."
        else:
            result["payment_options"] = [
                {
                    "choice": choice.value,
                    "pay_now_via_wamd": f"{format_kwd(amounts.amount_now)} {self.info.pricing.currency}",
                    "pay_on_booking_day": amount_values(self.info, choice, amounts)["due_on_day"],
                }
                for choice, amounts in availability.options.items()
            ]
            if PaymentChoice.DEPOSIT in availability.options:
                amounts = availability.options[PaymentChoice.DEPOSIT]
                result["reply"] = render(self.info, "payment_choice", amount_values(self.info, PaymentChoice.DEPOSIT, amounts))
                result["instruction"] = "The date is available. Send this reply so the customer chooses how to pay."
            else:
                result["instruction"] = ("The date is available. Only full payment is possible because it is "
                                         "one day away: tell the customer the amount in payment_options.")
        return ToolResult(_json(result))

    def _tool_show_booking_summary(
        self, date: str, location_id: str, customer_name: str, customer_phone: str, payment_choice: str,
    ) -> ToolResult:
        """Checks every booking rule and returns the owner's summary. Saves nothing."""
        preview = self.service.preview_booking(**_booking_request(
            date, location_id, customer_name, customer_phone, payment_choice))
        self.conversation.last_preview = preview
        return ToolResult(_json({
            "saved": False,
            "reply": render(self.info, "booking_summary", booking_values(self.info, preview)),
            "instruction": "Send this summary. Only if the customer says it is correct, call create_booking "
                           "with exactly the same details.",
        }))

    def _tool_create_booking(
        self, date: str, location_id: str, customer_name: str, customer_phone: str, payment_choice: str,
    ) -> ToolResult:
        """Creates a PENDING_PAYMENT booking - never a confirmed one."""
        request = _booking_request(date, location_id, customer_name, customer_phone, payment_choice)
        preview = self.service.preview_booking(**request)
        if preview != self.conversation.last_preview:
            return _error({
                "saved": False,
                "error": "The customer has not seen a summary with these details.",
                "instruction": "Call show_booking_summary, send the summary, and wait for the customer "
                               "to say it is correct.",
            })

        booking = self.service.create_booking(
            **request, channel=self.conversation.channel, channel_user_id=self.conversation.channel_user_id,
        )
        self.conversation.last_preview = None  # a second "yes" can't create a second booking
        return ToolResult(_json({
            "saved": True,
            "reference": booking.reference,
            "reply": render(self.info, "booking_created", booking_values(self.info, booking)),
            "instruction": "Send this reply.",
        }))

    def _tool_attach_payment_proof(self, reference: str, customer_phone: str, attachment_number: int) -> ToolResult:
        data = self.conversation.attachments.get(attachment_number)
        if data is None:
            return _error({"error": f"The customer has not sent image #{attachment_number} in this conversation."})
        self.service.attach_payment_proof(reference, customer_phone, data)
        return ToolResult(_json({
            "saved": True,
            "reply": render(self.info, "screenshot_received"),
            "instruction": "Send this reply. Never say the payment is received or the booking is confirmed.",
        }))

    def _tool_get_booking_status(self, reference: str, customer_phone: str) -> ToolResult:
        booking = self.service.find_customer_booking(reference, customer_phone)
        if booking is None:
            return ToolResult(_json({"found": False,
                                     "instruction": REFUSAL_INSTRUCTIONS[RefusalReason.BOOKING_NOT_FOUND]}))
        if booking.date_conflict:
            # The owner decides what happens; the customer is told nothing else.
            return ToolResult(_json({
                "found": True,
                "reply": render(self.info, "date_conflict_status"),
                "instruction": "Send only this reply. Do not share any other detail about this booking.",
            }))
        values = booking_values(self.info, booking)
        result: dict[str, object] = {
            "found": True,
            "reference": booking.reference,
            "status": booking.status.value,
            "status_meaning": STATUS_MEANINGS[booking.status],
            "date": values["date"],
            "location": location_name(self.info, booking.location_id),
            "paid_or_to_pay_via_wamd": f"{values['amount_now']} {self.info.pricing.currency}",
            "pay_on_booking_day": values["due_on_day"],
        }
        if booking.status is BookingStatus.CONFIRMED:
            result["reply"] = render(self.info, "booking_confirmed", values)
            result["instruction"] = "Send this reply."
        return ToolResult(_json(result))

    def _tool_handoff_to_human(
        self, type: str, summary: str, customer_name: str | None,
        customer_phone: str | None, booking_reference: str | None,
    ) -> ToolResult:
        if type == HandoffType.DATE_CONFLICT:
            return _error({"error": "date_conflict handoffs are created by the system only."})
        handoff = self.service.create_handoff(
            type, summary, customer_name=customer_name, customer_phone=customer_phone,
            channel=self.conversation.channel, channel_user_id=self.conversation.channel_user_id,
            booking_reference=booking_reference,
        )
        reply = "same_day" if handoff.type is HandoffType.SAME_DAY else "handoff"
        return ToolResult(_json({"handed_off": True, "reply": render(self.info, reply),
                                 "instruction": "Send this reply."}))

    # --- Helpers ------------------------------------------------------------------------

    def _refusal(self, refused: BookingRefused) -> ToolResult:
        result: dict[str, object] = {"refused": refused.reason.value, "detail": str(refused)}
        result["instruction"] = REFUSAL_INSTRUCTIONS.get(refused.reason, "Hand the case to the owner.")
        return _error(result)


STATUS_MEANINGS = {
    BookingStatus.PENDING_PAYMENT: "Waiting for the customer's Wamd transfer. The date is not secured yet.",
    BookingStatus.PAYMENT_SUBMITTED: "Screenshot received; the owner will verify the payment. The date is not secured yet.",
    BookingStatus.CONFIRMED: "Confirmed by the owner. The date is secured.",
    BookingStatus.COMPLETED: "The setup took place.",
    BookingStatus.CANCELLED: "Cancelled.",
}


def _booking_request(date: str, location_id: str, customer_name: str,
                     customer_phone: str, payment_choice: str) -> dict:
    return dict(
        booking_date=_parse_date(date), location_id=location_id, customer_name=customer_name,
        customer_phone=customer_phone, payment_choice=payment_choice,
    )


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a date in YYYY-MM-DD form") from None


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _error(data: dict) -> ToolResult:
    return ToolResult(_json(data), is_error=True)
