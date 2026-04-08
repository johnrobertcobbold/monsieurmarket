#!/usr/bin/env python3
"""
find_epic.py — Test internal positions endpoint.
"""

import os
import json
import requests as req
from dotenv import load_dotenv
load_dotenv()

from trading_ig import IGService

demo_user    = os.getenv("IG_USERNAME")
demo_pass    = os.getenv("IG_PASSWORD")
demo_key     = os.getenv("IG_API_KEY")
demo_acc_num = os.getenv("IG_ACC_NUMBER")

demo_svc = IGService(demo_user, demo_pass, demo_key, acc_type="DEMO")
demo_svc.create_session()
if demo_acc_num:
    demo_svc.switch_account(demo_acc_num, False)
print("✅ DEMO connected\n")

demo_headers_raw = demo_svc.crud_session.session.headers
demo_headers = {
    'X-IG-API-KEY':        demo_headers_raw.get('X-IG-API-KEY'),
    'CST':                 demo_headers_raw.get('CST'),
    'X-SECURITY-TOKEN':    demo_headers_raw.get('X-SECURITY-TOKEN'),
    'IG-ACCOUNT-ID':       demo_acc_num,
    'Content-Type':        'application/json',
    'Accept':              'application/json; charset=UTF-8',
    'x-device-user-agent': 'vendor=IG Group | applicationType=ig | platform=WTP | version=0.6631.0+073d24c3',
}

# place an order then check confirm via public API
ref = "2FHKCG01KNPVZX8FX"  # from earlier test

r = req.get(
    f'https://demo-api.ig.com/gateway/deal/confirms/{ref}',
    headers={**demo_headers, 'Version': '1'},
)
print(f"Public confirm: {r.status_code} — {r.text[:300]}")