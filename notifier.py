"""Deliver alerts: caregiver messages via ntfy push and/or Twilio SMS, calls via
Twilio voice. Each alert fans out to every caregiver/channel; it counts as
delivered if at least one recipient got it. DryRunNotifier only logs."""

from __future__ import annotations

import logging

import requests

from config import Config
from incident import Action

log = logging.getLogger("dad_tracker")

# ntfy priority + emoji tags per alert kind
_PUSH_STYLE = {
    "away_sms": ("urgent", "rotating_light", "{parent} left {residence}"),
    "failure_alert": ("high", "warning", "Tracker monitor is NOT working"),
    "stale_alert": ("high", "warning", "No recent location for {parent}'s wallet"),
    "returned_sms": ("default", "white_check_mark", "{parent} is back"),
    "failure_recovered": ("default", "white_check_mark", "Tracker monitor working again"),
    "stale_recovered": ("default", "white_check_mark", "Locations resumed"),
}


class DryRunNotifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, action: Action) -> None:
        log.info("[DRY RUN] %s to %s: %s", action.channel.upper(), action.target, action.text)


class Notifier:
    def __init__(self, cfg: Config):
        from twilio.rest import Client  # imported lazily so dry runs need no creds

        self.cfg = cfg
        self.client = Client(cfg.twilio_sid, cfg.twilio_token)

    def _numbers(self, target: str) -> tuple[str, ...]:
        return self.cfg.caregiver_phones if target == "caregiver" else (self.cfg.front_desk_phone,)

    def send(self, action: Action) -> None:
        if action.channel == "call":
            digits = self.cfg.front_desk_send_digits if action.target == "front_desk" else ""
            jobs = [(f"call {n}", lambda n=n: self.call(n, action.text, send_digits=digits))
                    for n in self._numbers(action.target)]
        else:
            jobs = []
            if "push" in self.cfg.caregiver_methods:
                jobs.append(("push", lambda: self.push(action)))
            if "sms" in self.cfg.caregiver_methods:
                jobs += [(f"sms {n}", lambda n=n: self.sms(n, action.text))
                         for n in self._numbers(action.target)]
        errors = []
        for label, job in jobs:
            try:
                job()
            except Exception as exc:
                log.error("%s failed for %s: %s", label, action.kind, exc)
                errors.append(f"{label}: {exc}")
        if errors and len(errors) == len(jobs):
            raise RuntimeError("; ".join(errors))

    def push(self, action: Action) -> None:
        r = self.cfg.rules
        priority, tag, title = _PUSH_STYLE.get(action.kind, ("default", "bell", "Tracker monitor"))
        headers = {
            "Title": title.format(parent=r.parent_name, residence=r.residence_name),
            "Priority": priority,
            "Tags": tag,
        }
        if action.url:
            headers["Click"] = action.url
            headers["Actions"] = f"view, Open map, {action.url}"
        resp = requests.post(f"{self.cfg.ntfy_server}/{self.cfg.ntfy_topic}",
                             data=action.text.encode("utf-8"), headers=headers, timeout=20)
        resp.raise_for_status()
        log.info("Push sent (%s): %s", action.kind, action.text)

    def sms(self, to: str, body: str) -> None:
        msg = self.client.messages.create(to=to, from_=self.cfg.twilio_from, body=body)
        log.info("SMS queued to %s (sid %s): %s", to, msg.sid, body)

    def call(self, to: str, spoken: str, send_digits: str = "") -> None:
        from twilio.twiml.voice_response import VoiceResponse

        twiml = VoiceResponse()
        twiml.pause(length=1)
        twiml.say(spoken, voice="Polly.Joanna")
        kwargs = {"send_digits": send_digits} if send_digits else {}
        call = self.client.calls.create(to=to, from_=self.cfg.twilio_from,
                                        twiml=str(twiml), **kwargs)
        log.info("Call placed to %s (sid %s)", to, call.sid)
