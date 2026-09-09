import argparse
import json
import os
import random
import re
import string
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError as _exc:
    sys.exit(f"missing dependency '{_exc.name}'. run: pip install -r requirements.txt")

BASE_URL = "https://auth.roblox.com"
SIGNUP_URL = f"{BASE_URL}/v2/signup"
CSRF_URL = f"{BASE_URL}/v1/login"

MAILTM_BASE = "https://api.mail.tm"

def _data_dir():
    base = Path.home() / ".local" / "roblox_account_generator"
    base.mkdir(parents=True, exist_ok=True)
    return base

def _random_username(length=12):
    vowels = "aeiou"
    cons = "bcdfgjklmnpqrstvwxz"
    name = []
    for i in range(length):
        pool = vowels if i % 2 else cons
        name.append(random.choice(pool))
    return "".join(name)

def _random_password(length=16):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(random.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw

def _get_csrf(client: httpx.Client) -> str:
    r = client.post(CSRF_URL, follow_redirects=True)
    return r.headers.get("x-csrf-token", "")

def _create_mailtm_account(client: httpx.Client):
    domain_resp = client.get(f"{MAILTM_BASE}/domains")
    domain_resp.raise_for_status()
    domains = domain_resp.json()
    if not domains:
        raise RuntimeError("no mail.tm domains available")
    domain = domains[0]["domain"]

    local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    address = f"{local}@{domain}"
    password = _random_password(12)

    reg_resp = client.post(
        f"{MAILTM_BASE}/accounts",
        json={"address": address, "password": password},
    )
    reg_resp.raise_for_status()
    account = reg_resp.json()

    token_resp = client.post(
        f"{MAILTM_BASE}/token",
        json={"address": address, "password": password},
    )
    token_resp.raise_for_status()
    bearer = token_resp.json()["token"]

    return {
        "id": account["id"],
        "address": address,
        "password": password,
        "token": bearer,
    }

def _get_verification_code(client: httpx.Client, mail_account: dict, timeout: int = 60):
    headers = {"Authorization": f"Bearer {mail_account['token']}"}
    start = time.time()
    while time.time() - start < timeout:
        resp = client.get(f"{MAILTM_BASE}/messages", headers=headers)
        resp.raise_for_status()
        messages = resp.json().get("hydra:member", [])
        for msg in messages:
            if "roblox" in msg.get("subject", "").lower():
                detail_resp = client.get(
                    f"{MAILTM_BASE}/messages/{msg['id']}", headers=headers
                )
                detail_resp.raise_for_status()
                body = detail_resp.json().get("text", "")
                match = re.search(r"\b\d{6}\b", body)
                if match:
                    return match.group(0)
        time.sleep(3)
    return None

def generate_account(use_email: bool = False, proxy: str = None, retries: int = 3):
    creds = {
        "username": _random_username(),
        "password": _random_password(),
        "birthday": {
            "month": random.randint(1, 12),
            "day": random.randint(1, 28),
            "year": random.randint(1990, 2005),
        },
        "gender": random.choice([1, 2]),
    }

    proxies = {"https://": proxy, "http://": proxy} if proxy else None

    with httpx.Client(proxies=proxies, timeout=30, follow_redirects=True) as client:
        csrf = _get_csrf(client)

        if use_email:
            mail = _create_mailtm_account(client)
            creds["email"] = mail["address"]

        payload = {
            "username": creds["username"],
            "password": creds["password"],
            "birthday": creds["birthday"],
            "gender": creds["gender"],
            "isTosAgreementBoxChecked": True,
            "isRobloxToURequired": True,
            "referer": "https://www.roblox.com/",
            "captchaToken": "",
        }

        headers = {
            "x-csrf-token": csrf,
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0.36",
        }

        last_err = None
        for attempt in range(retries):
            r = client.post(SIGNUP_URL, json=payload, headers=headers)
            # print(f"status {r.status_code}: {r.text[:200]}")  # debug
            if r.status_code == 403 and "Token Validation Failed" in r.text:
                csrf = _get_csrf(client)
                headers["x-csrf-token"] = csrf
                last_err = "csrf rotated, retrying"
                continue
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                last_err = "rate limited"
                continue
            if r.status_code >= 500:
                last_err = f"server error {r.status_code}"
                time.sleep(1)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError(f"signup failed after {retries} attempts: {last_err}")

        data = r.json()

        if use_email:
            code = _get_verification_code(client, mail)
            if code:
                creds["verification_code"] = code

    return creds

def _load_accounts():
    p = _data_dir() / "accounts.json"
    if not p.exists():
        return []
    return json.loads(p.read_text())

def _save_accounts(accounts):
    p = _data_dir() / "accounts.json"
    p.write_text(json.dumps(accounts, indent=2))

def cmd_generate(args):
    count = args.count
    if count < 1:
        print("count must be at least 1", file=sys.stderr)
        sys.exit(2)

    accounts = _load_accounts()
    for i in range(count):
        try:
            acc = generate_account(use_email=args.email, proxy=args.proxy)
            accounts.append(acc)
            print(f"generated {acc['username']}")
        except Exception as e:
            print(f"failed on account {i+1}: {e}", file=sys.stderr)

    _save_accounts(accounts)
    print(f"saved {len(accounts)} total account(s)")

def cmd_list(args):
    accounts = _load_accounts()
    if not accounts:
        print("no accounts yet. generate one with 'generate' subcommand")
        return 0
    for idx, acc in enumerate(accounts, 1):
        print(f"{idx}: {acc['username']} ({acc.get('email', 'no email')})")
    return 0

def main():
    parser = argparse.ArgumentParser(
        description="Generate disposable Roblox accounts.",
        usage="python roblox_account_generator.py <command> [options]",
    )
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Generate new accounts")
    gen.add_argument("--email", action="store_true", help="Enable email verification via mail.tm")
    gen.add_argument("--count", type=int, default=1, help="Number of accounts to generate")
    gen.add_argument("--proxy", default=None, help="HTTPS proxy URL")

    lst = sub.add_parser("list", help="List stored accounts")

    args = parser.parse_args()

    if args.command is None:
        parser.print_usage()
        sys.exit(2)

    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_usage()
        sys.exit(2)

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
