"""FindMy.py anisette provider that uses this Mac's own Apple device identity
(AOSKit), like export-findmy does. Apple rejects FindMy.py's emulated anisette
on macOS 26 ("This action cannot be completed at this time"); the native one
is the real Mac, so its headers are accepted.

NativeAsyncAppleAccount._gsa_authenticate is adapted from FindMy.py 0.10.2
(MIT License, Copyright (c) 2023 FindMy.py Contributors; see LICENSE). The
header and cpd values were chosen to match what Apple's own clients send, as
observed from rustpush/export-findmy; no rustpush code is included."""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import subprocess
import uuid
from functools import cache, cached_property
from pathlib import Path
from typing import TypedDict

from findmy.reports.anisette import BaseAnisetteProvider
from findmy.util.http import HttpResponse, HttpSession

logger = logging.getLogger("dadtracker.gsa")

_GSA_URL = "https://gsa.apple.com/grandslam/GsService2"
_LOGIN_DELEGATES_URL = "https://setup.icloud.com/setup/iosbuddy/loginDelegates"
_GSA_2FA_URLS = ("https://gsa.apple.com/auth", f"{_GSA_URL}/validate")
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko)")
_MOBILESMS_AUTHKIT = "<com.apple.AuthKit/1 (com.apple.MobileSMS/1262.500.151.1.2)>"


class NativeAnisetteMapping(TypedDict):
    type: str


def _retrieve_otp_headers() -> dict[str, str]:
    from Foundation import NSBundle, NSClassFromString

    bundle = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AOSKit.framework")
    if bundle is None or not bundle.load():
        raise RuntimeError("AOSKit framework unavailable; native anisette needs macOS")
    headers = NSClassFromString("AOSUtilities").retrieveOTPHeadersForDSID_("-2")
    if not headers or "X-Apple-MD" not in headers:
        raise RuntimeError("AOSKit returned no anisette headers")
    return {"otp": str(headers["X-Apple-MD"]), "machine": str(headers["X-Apple-MD-M"])}


@cache
def _device_ids() -> dict[str, str]:
    """This Mac's real AuthKit identifiers. FindMy.py otherwise sends random
    ones, which contradict the machine ID in the native OTP headers."""
    from Foundation import NSBundle, NSClassFromString

    NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AuthKit.framework").load()
    _retrieve_otp_headers()  # ensures AOSKit is loaded
    device = NSClassFromString("AKDevice").currentDevice()
    return {
        "X-Apple-I-MD-LU": str(device.localUserUUID()),
        "X-Mme-Device-Id": str(device.uniqueDeviceIdentifier()),
        "X-Apple-I-SRL-NO": str(NSClassFromString("AOSUtilities").machineSerialNumber()),
    }


@cache
def _user_agent(item: str) -> str:
    from Foundation import NSBundle

    cfnetwork = NSBundle.bundleWithPath_("/System/Library/Frameworks/CFNetwork.framework")
    version = cfnetwork.objectForInfoDictionaryKey_("CFBundleVersion")
    return f"{item} CFNetwork/{version} Darwin/{os.uname().release}"


def _akd_user_agent() -> str:
    return _user_agent("akd/1.0")


@cache
def _device_info() -> dict[str, str]:
    from Foundation import NSClassFromString

    _device_ids()  # loads AuthKit
    device = NSClassFromString("AKDevice").currentDevice()
    return {"locale": str(device.locale().localeIdentifier()),
            "client_info": str(device.serverFriendlyDescription())}


def _native_gsa_anisette() -> dict[str, str]:
    """The header set rustpush's AOSKit provider sends (the export tool's login
    succeeded on this Mac with exactly these)."""
    otp = _retrieve_otp_headers()
    return {"X-Apple-I-MD": otp["otp"], "X-Apple-I-MD-M": otp["machine"],
            "X-Apple-I-MD-LU": _device_ids()["X-Apple-I-MD-LU"],
            "X-Mme-Device-Id": _device_ids()["X-Mme-Device-Id"]}


def _gsa_cpd(anisette: dict[str, str], request_uuid: str) -> dict:
    return {**anisette,
            "X-Apple-I-Device-Configuration-Mode": "0",
            "X-Apple-I-Request-UUID": request_uuid,
            "X-Apple-Requested-Partition": "0",
            "X-Apple-Security-Upgrade-Context": "com.apple.authkit.generic",
            "capp": _GSA_CONTEXT["capp"], "cbid": _GSA_CONTEXT["cbid"],
            "cou": "US", "loc": "en_US", "svct": _GSA_CONTEXT["svct"],
            "X-Apple-Offer-Security-Upgrade": True, "at": 0, "bootstrap": True,
            "ckgen": True, "fcd": True, "icdrsDisabled": False, "icscrec": True,
            "pbe": False, "prkgen": True, "webAccessEnabled": False}


# Same login context rustpush uses; Apple accepted it from this Mac.
_GSA_CONTEXT = {"svct": "imessage", "capp": "Messages", "cbid": "com.apple.MobileSMS"}


class _AkdHttpSession(HttpSession):
    """Makes FindMy.py's Grand Slam (SRP) requests look like the ones the
    export tool sent successfully from this Mac, and adapts to Apple's
    Oct 2026 2FA signal. FindMy.py 0.10.2 sends a 2018 akd User-Agent, random
    device IDs and a minimal cpd, which Apple answers with HTTP 429."""

    def __init__(self) -> None:
        super().__init__()
        self._gsa_login: tuple[str, dict[str, str]] | None = None

    async def request(self, method, url, **kwargs):
        if url == _GSA_URL and isinstance(kwargs.get("data"), bytes):
            return await self._gsa(method, url, **kwargs)
        if url.startswith(_GSA_2FA_URLS):
            return await self._two_factor(method, url, **kwargs)
        if url == _LOGIN_DELEGATES_URL:
            return await self._login_delegates(method, url, **kwargs)
        return await super().request(method, url, **kwargs)

    async def _login_delegates(self, method, url, **kwargs):
        # FindMy.py hardcodes a MacBookPro18,3/macOS 13.4 identity here, which
        # contradicts this Mac's anisette (Apple answers status 1, "Apple ID or
        # password was entered incorrectly"). Send what rustpush sends: this
        # Mac's own identity throughout.
        old = kwargs.get("headers") or {}
        otp = _retrieve_otp_headers()
        ids = _device_ids()
        kwargs["headers"] = {
            "Accept-Encoding": "gzip",
            "User-Agent": _user_agent("com.apple.iCloudHelper/282"),
            "X-Apple-ADSID": old["X-Apple-ADSID"],
            "X-Apple-I-MD": otp["otp"], "X-Apple-I-MD-M": otp["machine"],
            "X-Apple-SRL-NO": ids["X-Apple-I-SRL-NO"],
            "X-Apple-I-MD-LU": ids["X-Apple-I-MD-LU"],
            "X-Apple-Locale": _device_info()["locale"],
            "X-Mme-Client-Info": _device_info()["client_info"],
            "X-Mme-Device-Id": ids["X-Mme-Device-Id"],
        }
        resp = await super().request(method, url, **kwargs)
        _log_delegates_status(resp)
        return resp

    async def _two_factor(self, method, url, **kwargs):
        # FindMy.py presents as Xcode here; send the headers rustpush uses.
        headers = {k: v for k, v in (kwargs.get("headers") or {}).items()
                   if k not in ("X-Apple-App-Info", "X-Xcode-Version")}
        client = headers.get("X-Mme-Client-Info", "")
        headers.update(_native_gsa_anisette())
        headers.update({
            "User-Agent": _BROWSER_UA,
            "X-Mme-Client-Info": client.rsplit("<", 1)[0] + _MOBILESMS_AUTHKIT if client else client,
            "X-Apple-Client-App-Name": _GSA_CONTEXT["capp"],
            "X-Apple-I-Client-Bundle-Id": _GSA_CONTEXT["cbid"],
            "X-Apple-AK-Context-Type": _GSA_CONTEXT["svct"],
            "X-Apple-Requested-Partition": "0", "X-Apple-I-DeviceUserMode": "0",
            "X-Apple-I-Locale": "en_US", "X-MMe-Country": "US",
            "X-Apple-Security-Upgrade-Context": "com.apple.authkit.generic",
            "Accept-Language": "en-US,en;q=0.9",
        })
        kwargs["headers"] = headers
        resp = await super().request(method, url, **kwargs)
        if url == _GSA_2FA_URLS[0] and not resp.ok:
            # The SMS-number page now returns 403 for non-Apple clients; an
            # empty page makes FindMy.py offer only the trusted-device method.
            return HttpResponse(200, b"")
        return resp

    async def _gsa(self, method, url, **kwargs):
        body = plistlib.loads(kwargs["data"])
        req = body["Request"]
        # init and complete share one request UUID and one OTP, as in akd.
        if req.get("o") == "init" or self._gsa_login is None:
            self._gsa_login = (str(uuid.uuid4()).upper(), _native_gsa_anisette())
        request_uuid, anisette = self._gsa_login
        req["cpd"] = _gsa_cpd(anisette, request_uuid)
        kwargs["data"] = plistlib.dumps(body)
        kwargs["headers"] = {
            "Content-Type": "text/x-xml-plist", "Accept": "*/*", **anisette,
            "X-Apple-AK-Context-Type": _GSA_CONTEXT["svct"],
            "X-Apple-Client-App-Name": _GSA_CONTEXT["capp"],
            "X-Apple-I-Client-Bundle-Id": _GSA_CONTEXT["cbid"],
            "X-MMe-Client-Info": kwargs.get("headers", {}).get("X-MMe-Client-Info", ""),
            "Accept-Language": "en-US,en;q=0.9", "User-Agent": _akd_user_agent(),
        }
        resp = await super().request(method, url, **kwargs)
        if resp.ok:
            _log_gsa_status(req.get("o"), resp)
        return resp


def _log_gsa_status(op, resp) -> None:
    try:
        status = plistlib.loads(resp._content)["Response"]["Status"]  # noqa: SLF001
    except Exception:
        return
    logger.info("GSA %s: ec=%s hsc=%s au=%s em=%s", op, status.get("ec"),
                status.get("hsc"), status.get("au"), status.get("em"))


def _log_delegates_status(resp) -> None:
    try:
        data = plistlib.loads(resp._content)  # noqa: SLF001
    except Exception:
        logger.info("loginDelegates: HTTP %s, non-plist body", resp.status_code)
        return
    mm = data.get("delegates", {}).get("com.apple.mobileme", {})
    logger.info("loginDelegates: HTTP %s status=%s mobileme_status=%s msg=%s tokens=%s",
                resp.status_code, data.get("status"), mm.get("status"),
                mm.get("status-message") or data.get("status-message"),
                sorted(mm.get("service-data", {}).get("tokens", {})))


def needs_2fa(status: dict, spd: dict) -> str | None:
    """Which second factor Apple wants after SRP, or None.

    Apple used to say so in Status.au; since 2026-10-12 it may instead send
    Status.hsc == 409 with no au, and then withholds the PET token. FindMy.py
    0.10.2 only checks au, skips 2FA, and loginDelegates fails with status 1
    ("Your Apple ID or password was entered incorrectly")."""
    au = status.get("au")
    if au in ("secondaryAuth", "trustedDeviceSecondaryAuth"):
        return au
    pet = spd.get("t", {}).get("com.apple.gs.idms.pet", {}).get("token")
    if str(status.get("hsc")) == "409" or not pet:
        return "trustedDeviceSecondaryAuth"
    return None


def _native_async_account_cls():
    import srp._pysrp as srp
    from findmy import util
    from findmy.errors import InvalidCredentialsError, UnhandledProtocolError
    from findmy.reports.account import AsyncAppleAccount
    from findmy.reports.state import LoginState

    class NativeAsyncAppleAccount(AsyncAppleAccount):
        async def _gsa_authenticate(self, username=None, password=None):
            # FindMy.py 0.10.2's SRP flow, unchanged except for 2FA detection.
            self._username = username or self._username
            self._password = password or self._password
            if not self._username or not self._password:
                raise ValueError("No username or password specified")

            usr = srp.User(self._username, b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
            _, a2k = usr.start_authentication()
            r = await self._gsa_request(
                {"A2k": a2k, "u": self._username, "ps": ["s2k", "s2k_fo"], "o": "init"})
            if r["Status"].get("ec") != 0:
                raise InvalidCredentialsError("Email verification failed: " + r["Status"].get("em"))
            sp = r.get("sp")
            if sp not in {"s2k", "s2k_fo"}:
                raise UnhandledProtocolError(f"Unsupported SRP protocol: {sp}")

            usr.p = util.crypto.encrypt_password(self._password, r["s"], r["i"], sp)
            m1 = usr.process_challenge(r["s"], r["B"])
            if m1 is None:
                raise UnhandledProtocolError("Failed to process challenge")
            r = await self._gsa_request(
                {"c": r["c"], "M1": m1, "u": self._username, "o": "complete"})
            if r["Status"].get("ec") != 0:
                raise InvalidCredentialsError("Password authentication failed: " + r["Status"].get("em"))
            usr.verify_session(r.get("M2"))
            if not usr.authenticated():
                raise UnhandledProtocolError("Failed to verify session")

            spd = util.parsers.decode_plist(
                util.crypto.decrypt_spd_aes_cbc(usr.get_session_key() or b"", r["spd"]))
            logger.info("GSA tokens issued: %s", sorted(spd.get("t", {})))
            factor = needs_2fa(r["Status"], spd)
            self._account_info = {
                "account_name": spd.get("acname"), "first_name": spd.get("fn"),
                "last_name": spd.get("ln"),
                "trusted_device_2fa": factor == "trustedDeviceSecondaryAuth",
            }
            if factor:
                return self._set_login_state(
                    LoginState.REQUIRE_2FA,
                    {"adsid": spd["adsid"], "idms_token": spd["GsIdmsToken"]})
            pet = spd["t"]["com.apple.gs.idms.pet"]["token"]
            return self._set_login_state(
                LoginState.AUTHENTICATED, {"idms_pet": pet, "adsid": spd["adsid"]})

    return NativeAsyncAppleAccount


def native_apple_account(state_info: dict | None = None):
    """An AppleAccount that presents as this Mac's own akd on every request."""
    from findmy import AppleAccount

    anisette = NativeAnisetteProvider()
    account = AppleAccount(anisette, state_info=state_info)
    account._asyncacc = _native_async_account_cls()(  # noqa: SLF001
        anisette=anisette, state_info=state_info)
    account._asyncacc._http = _AkdHttpSession()  # noqa: SLF001
    return account


class NativeAnisetteProvider(BaseAnisetteProvider):
    def __init__(self) -> None:
        super().__init__()
        self._last: dict[str, str] | None = None

    @property
    def otp(self) -> str:
        self._last = _retrieve_otp_headers()
        return self._last["otp"]

    @property
    def machine(self) -> str:
        # Fetched together with the OTP in get_headers(); fall back to a fresh read.
        return (self._last or _retrieve_otp_headers())["machine"]

    @cached_property
    def client(self) -> str:
        # Must describe this Mac: Apple rejects client info that contradicts the
        # device identity behind the anisette headers.
        model = subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True,
                               text=True).stdout.strip() or "Mac"
        build = subprocess.run(["sw_vers", "-buildVersion"], capture_output=True,
                               text=True).stdout.strip()
        return (f"<{model}> <macOS;{platform.mac_ver()[0]};{build}> "
                "<com.apple.AuthKit/1 (com.apple.akd/1.0)>")

    async def get_headers(self, user_id: str, device_id: str, serial: str = "0",
                          with_client_info: bool = False) -> dict[str, str]:
        self._last = None
        headers = await super().get_headers(user_id, device_id, serial, with_client_info)
        headers.update(_device_ids())
        return headers

    def to_json(self, dst: str | Path | None = None, /) -> NativeAnisetteMapping:
        return {"type": "aniNative"}

    @classmethod
    def from_json(cls, val, /) -> "NativeAnisetteProvider":
        return cls()

    async def close(self) -> None:
        pass
