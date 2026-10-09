"""Command line for the repair Wing account (see wing_repair.py).

    venv/bin/python wing_rep_cli.py find "#13485_REPARATION"
    venv/bin/python wing_rep_cli.py next-ref 13485
    venv/bin/python wing_rep_cli.py create 13485 --weight 1.5 --designation mocassin
    venv/bin/python wing_rep_cli.py cancel "#13485_REPARATION"
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import wing_repair  # noqa: E402  (needs the env loaded first)


def _summary(order):
    return {
        "ref": order['ref'],
        "status": order['status'],
        "service": order['service'],
        "recipient": order['recipient'].get('company') or order['recipient'].get('lastName'),
        "products": [{"designation": op['product']['designation'], "weight": op['product']['weight'],
                      "quantity": op['quantity']} for op in order['orderProducts']],
        "parcels": [{"tracking_id": p['trackingId'], "status": p['status'], "label_url": p['labelURL']}
                    for fo in order['fulfillmentOrders'] for p in fo['parcels']],
    }


def main():
    parser = argparse.ArgumentParser(description="Compte Wing réparation")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find").add_argument("ref")
    sub.add_parser("next-ref").add_argument("order_number")
    create = sub.add_parser("create")
    create.add_argument("order_number")
    create.add_argument("--weight", type=float, required=True)
    create.add_argument("--designation")
    sub.add_parser("cancel").add_argument("ref")
    args = parser.parse_args()

    token = wing_repair._get_token()

    if args.cmd == "find":
        order = wing_repair._find_order(token, args.ref)
        result = _summary(order) if order else None
    elif args.cmd == "next-ref":
        result = wing_repair.next_free_ref(args.order_number, token)
    elif args.cmd == "create":
        result = wing_repair.create_repair_label(args.order_number, weight_kg=args.weight,
                                                 designation=args.designation)
    elif args.cmd == "cancel":
        result = wing_repair.cancel_label(args.ref, token)

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
