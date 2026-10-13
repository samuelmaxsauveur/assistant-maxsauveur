"""Command line for avoirs after a return (see avoirs.py).

    venv/bin/python avoir_cli.py prepare 13485 "brogue derby marron"
    venv/bin/python avoir_cli.py send 13485 "brogue derby marron"

`prepare` only reads Shopify and prints the email for Samuel to check. `send`
creates the code and sends the email from hello@, and is run only once Samuel
has approved that exact email. --montant overrides the amount, --quantite
covers several identical items returned.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import avoirs  # noqa: E402  (needs the env loaded first)


def _recap(avoir):
    lines = [
        f"Commande  : {avoir['order']}",
        f"Client    : {avoir['full_name']} <{avoir['email']}>",
        f"Article   : {avoir['item']} ({avoir['variant']}) x{avoir['quantity']}",
        f"Prix catalogue à la commande : {avoir['unit_price']:.2f} €"
        + (f" (aujourd'hui : {avoir['current_price']:.2f} €)"
           if avoir['current_price'] is not None and avoir['current_price'] != avoir['unit_price'] else ""),
        f"Codes utilisés à la commande : {', '.join(avoir['codes_used']) or 'aucun'}",
        f"Montant de l'avoir : {avoir['amount']:.2f} €",
        f"Valable jusqu'au : {avoir['ends_at']:%d/%m/%Y}",
    ]
    for a in avoir["already"]:
        lines.append(f"ATTENTION avoir déjà existant : {a['title']} [{a['code']}, {a['status']}]")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Avoirs après retour")
    parser.add_argument("cmd", choices=["prepare", "send"])
    parser.add_argument("order_number")
    parser.add_argument("model")
    parser.add_argument("--quantite", type=int, default=1)
    parser.add_argument("--montant", type=float)
    parser.add_argument("--force", action="store_true", help="créer même si un avoir existe déjà")
    args = parser.parse_args()

    try:
        avoir = avoirs.prepare(args.order_number, args.model, args.quantite, args.montant)
    except RuntimeError as e:
        print(e)
        return 1
    print(_recap(avoir))

    if args.cmd == "prepare":
        subject, body = avoirs.email(avoir)
        print(f"\nObjet : {subject}\n\n{body}")
        return 0

    if avoir["already"] and not args.force:
        print("\nAvoir déjà créé pour cette commande : rien n'est fait (--force pour en créer un second).")
        return 1
    code = avoirs.create_code(avoir)
    print(f"\nCode créé dans Shopify : {code}")
    subject, body = avoirs.email(avoir, code)

    import gmail
    gmail.send_email(gmail.get_gmail_service(), avoir["email"], subject, body)
    print(f"E-mail envoyé à {avoir['email']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
