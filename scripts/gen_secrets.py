#!/usr/bin/env python3
"""Generate deployment secrets and synthetic demo principals (never commit the output).

Writes  .env               (from .env.example, with random secrets filled in)
        secrets/users.json (PBKDF2-SHA256 hashes of the synthetic demo users)
        secrets/demo_passwords.txt (plain demo passwords, for local testing only)
All users are synthetic: S0001..S0050 (student), FAC01..FAC20 (faculty), REG01, FIN01, ADM01.
"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from ftlib.security import hash_password  # noqa: E402

USERS = ([(f"S{i:04d}", "student") for i in range(1, 51)] + [(f"FAC{i:02d}", "faculty") for i in range(1, 21)]
         + [("REG01", "registrar"), ("FIN01", "finance"), ("ADM01", "admin")])


def main():
    env = ROOT / ".env"
    if env.exists() and "--force" not in sys.argv:
        sys.exit(".env exists (use --force to overwrite)")
    vals = {k: secrets.token_urlsafe(36) for k in
            ("POSTGRES_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "REPLICATION_PASSWORD", "JWT_SECRET", "INTERNAL_AUTH_SECRET", "ADMIN_TOKEN")}
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        k = line.split("=", 1)[0]
        lines.append(f"{k}={vals[k]}" if k in vals else line)
    env.write_text("\n".join(lines) + "\n")
    out = ROOT / "secrets"
    out.mkdir(exist_ok=True)
    pw = {u: secrets.token_urlsafe(12) for u, _ in USERS}
    import json
    (out / "users.json").write_text(json.dumps({u: {"role": r, "hash": hash_password(pw[u])} for u, r in USERS}, indent=1))
    (out / "demo_passwords.txt").write_text("\n".join(f"{u}\t{r}\t{pw[u]}" for u, r in USERS) + "\n")
    print(f"wrote {env} and {out}/ (users.json, demo_passwords.txt)")


if __name__ == "__main__":
    main()
