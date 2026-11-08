"""Create the first HR admin for password sign-in, or set a new password for an admin who is locked out.

Nobody can sign up as an admin, so this is how the first one is made. It needs access to the database,
and asks for the password without showing it:

    venv\\Scripts\\python -m ops.create_admin you@company.com --name "Your Name"
"""
import argparse
import getpass
import sys

from app import config
from app.db import get_engine
from app.identity import Organisations, Users
from app.passwords import PasswordAccounts, password_problem
from app.rag.store import run_migrations


def create_admin(engine, email: str, name: str, password: str) -> str:
    """Make this person an HR admin of the default organisation, with this password."""
    users = Users(engine)
    Organisations(engine).ensure(config.tenant)
    existing = users.find_by_email(email)
    if existing:
        user_id, tenant_id = existing.id, existing.tenant_id
        if existing.role == "employee":
            users.update(tenant_id, user_id, "hr_admin", None)
    else:
        tenant_id = config.TENANT_ID
        user_id = users.create(tenant_id, email, "hr_admin", display_name=name)["id"]
    PasswordAccounts(engine, users).set_password(user_id, tenant_id, password)
    return tenant_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an HR admin who signs in with a password")
    parser.add_argument("email")
    parser.add_argument("--name", default="", help="Shown on the chat screen")
    args = parser.parse_args()
    password = getpass.getpass("New password: ")
    problem = password_problem(password, args.email, args.name)
    if problem:
        sys.exit(problem)
    if password != getpass.getpass("Same password again: "):
        sys.exit("Those passwords don't match.")
    run_migrations()
    engine = get_engine()
    tenant_id = create_admin(engine, args.email, args.name, password)
    engine.dispose()
    print(f"{args.email} can now sign in as an HR admin of {tenant_id}.")


if __name__ == "__main__":
    main()
