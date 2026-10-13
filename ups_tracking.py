"""Follow the carton sent to a workshop on UPS.

Uses the UPS Tracking API (free developer account, UPS_CLIENT_ID and
UPS_CLIENT_SECRET). Without those keys nothing is checked and Samuel
moves the pairs himself; the page still links to the UPS tracking page.
"""
import os
import time
import uuid

import requests

import database
import repairs

TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details/{}"
_token = {"value": None, "until": 0.0}


def enabled():
    return bool(os.getenv("UPS_CLIENT_ID") and os.getenv("UPS_CLIENT_SECRET"))


def tracking_link(number):
    return f"https://www.ups.com/track?tracknum={number}&loc=fr_FR"


def _get_token():
    if _token["value"] and time.time() < _token["until"]:
        return _token["value"]
    resp = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                         auth=(os.getenv("UPS_CLIENT_ID"), os.getenv("UPS_CLIENT_SECRET")), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token.update(value=data["access_token"], until=time.time() + int(data.get("expires_in", 3600)) - 60)
    return _token["value"]


def delivered(number):
    """True once UPS reports the parcel delivered."""
    resp = requests.get(TRACK_URL.format(number), timeout=30, headers={
        "Authorization": f"Bearer {_get_token()}", "transId": uuid.uuid4().hex[:32], "transactionSrc": "maxsauveur"})
    resp.raise_for_status()
    shipments = resp.json().get("trackResponse", {}).get("shipment", [])
    for shipment in shipments:
        for package in shipment.get("package", []):
            status = package.get("currentStatus") or {}
            if status.get("type") == "D" or "deliver" in (status.get("description") or "").lower():
                return True
    return False


def refresh():
    """Scheduler job: pairs shipped to a workshop become "en cours de réparation" on delivery."""
    if not enabled():
        return 0
    shipped = [c for c in database.get_repair_cases()
               if c["status"] == "sent_workshop" and c.get("workshop_tracking")]
    moved = 0
    for number in {c["workshop_tracking"] for c in shipped}:
        try:
            if delivered(number):
                for c in shipped:
                    if c["workshop_tracking"] == number:
                        repairs.update_case(c["id"], status="in_repair")
                        moved += 1
        except Exception as e:
            print(f"[UPS] {number}: {e}")
    return moved
