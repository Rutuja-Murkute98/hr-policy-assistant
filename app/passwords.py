"""Email and password sign-in: accounts, hashed passwords, set-up codes and lasting sessions.

Passwords are only ever stored as Argon2id hashes. A session is a long random token: the browser keeps
it, and the database keeps only its hash, so sessions survive restarts and can be ended at any time.

Finding an account has to look across tenants, so these lookups run as the database owner. The app's
tenant role has no access to these tables at all. See migrations/versions/0006_password_sign_in.py.
"""
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Engine, exc, text

from app import config
from app.identity import IdentityError, Principal, Users, hash_key

TOKEN_PREFIX = "hrs_"
MAX_PASSWORD_CHARS = 128  # a very long password would only make hashing slow
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L, as a code may be read out or retyped
BAD_SIGN_IN = "That email and password don't match. Please try again."
BAD_CODE = "That email and set-up code don't match, or the code has expired. Please ask your HR team for a new one."
ALREADY_REGISTERED = ("That email is already registered. Please sign in. If HR added you, or you've forgotten "
                      "your password, ask your HR team for a set-up code.")
INACTIVE = "This account isn't active. Please contact your HR team."
# The most common choices that are long enough to pass the length check
COMMON_PASSWORDS = frozenset({
    "1234567890", "12345678910", "123456789012", "0123456789", "1111111111", "0000000000", "qwertyuiop",
    "qwertyuiop1", "password12", "password123", "password1234", "passw0rd123", "administrator", "letmein123",
    "welcome123", "welcome1234", "iloveyou12", "iloveyou123", "abcdefghij", "abc1234567", "1q2w3e4r5t",
    "qwerty12345", "qwerty123456", "changeme123",
})

# OWASP's minimum for Argon2id. It uses 19 MB per check, which the small hosted API can spare.
hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)
NO_ACCOUNT_HASH = hasher.hash(secrets.token_urlsafe(32))  # checked when there's no account, so timing reveals nothing


class AccountProblem(Exception):
    """The account can't be created or changed. The message is safe to show."""


@dataclass
class SignedIn:
    token: str  # the only time this is available: only its hash is stored
    principal: Principal
    max_age_seconds: int | None  # None = ends when the browser closes


def password_problem(password: str, email: str = "", name: str = "") -> str | None:
    """Say what's wrong with a new password, if anything. Length matters more than symbols."""
    if len(password) < config.MIN_PASSWORD_CHARS:
        return f"Please use a password of at least {config.MIN_PASSWORD_CHARS} characters."
    if len(password) > MAX_PASSWORD_CHARS:
        return f"Please use a password of at most {MAX_PASSWORD_CHARS} characters."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) < 4:
        return "That password is too easy to guess. A short phrase of a few words works well."
    personal = [email.lower(), email.split("@")[0].lower(), name.strip().lower()]
    if any(len(part) >= 4 and part in lowered for part in personal):
        return "Please don't use your name or email in your password."
    return None


def email_allowed(email: str) -> bool:
    domains = config.SIGNUP_EMAIL_DOMAINS
    return not domains or email.rsplit("@", 1)[-1].lower() in domains


def new_setup_code() -> str:
    letters = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return "-".join(letters[start:start + 4] for start in range(0, 12, 4))


def clean_code(code: str) -> str:
    return re.sub(r"\s", "", code).upper()


class PasswordAccounts:
    def __init__(self, engine: Engine, users: Users):
        self.engine = engine
        self.users = users

    def _start_session(self, connection, user_id: str, tenant_id: str, remember: bool) -> tuple[str, int | None]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        days, hours = (config.SESSION_REMEMBER_DAYS, 0) if remember else (0, config.SESSION_HOURS)
        connection.execute(text(
            "INSERT INTO user_sessions (id, token_hash, user_id, tenant_id, expires_at) "
            "VALUES (:id, :hash, :user_id, :tenant_id, :expires_at)"
        ), {"id": uuid.uuid4().hex, "hash": hash_key(token), "user_id": user_id, "tenant_id": tenant_id,
            "expires_at": datetime.now(UTC) + timedelta(days=days, hours=hours)})
        return token, days * 86_400 if remember else None

    @staticmethod
    def _principal(user) -> Principal:
        return Principal(user.tenant_id, user.employee_id or user.id, user.role, "password", user.email,
                         user.display_name, user.id)

    def sign_up(self, email: str, password: str, display_name: str, remember: bool = False) -> SignedIn:
        """Create an employee account in the default organisation, and sign them in.

        The role is always employee, and there's no link to HR records: an email here isn't proven, so
        it can't be allowed to claim anything. An HR admin can link the account to an employee later.
        """
        if not config.ALLOW_SIGNUP:
            raise AccountProblem("Creating an account isn't switched on here. Please ask your HR team.")
        email, display_name = email.strip(), " ".join(display_name.split())
        if not display_name:
            raise AccountProblem("Please enter your name.")
        if not email_allowed(email):
            raise AccountProblem("Please use your work email address.")
        problem = password_problem(password, email, display_name)
        if problem:
            raise AccountProblem(problem)
        # An existing account is never taken over, even with no password yet, or anyone could claim an admin's
        if self.users.find_by_email(email):
            raise AccountProblem(ALREADY_REGISTERED)
        password_hash = hasher.hash(password)
        user_id = uuid.uuid4().hex
        try:
            with self.engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, tenant_id, email, display_name, role) "
                    "VALUES (:id, :tenant_id, :email, :display_name, 'employee')"
                ), {"id": user_id, "tenant_id": config.TENANT_ID, "email": email, "display_name": display_name})
                connection.execute(text(
                    "INSERT INTO user_credentials (user_id, tenant_id, password_hash) "
                    "VALUES (:user_id, :tenant_id, :hash)"
                ), {"user_id": user_id, "tenant_id": config.TENANT_ID, "hash": password_hash})
                token, max_age = self._start_session(connection, user_id, config.TENANT_ID, remember)
        except exc.IntegrityError:  # someone else registered this email at the same moment
            raise AccountProblem(ALREADY_REGISTERED) from None
        return SignedIn(token, self.verify(token), max_age)

    def sign_in(self, email: str, password: str, remember: bool = False) -> SignedIn:
        user = self.users.find_by_email(email.strip())
        stored = None
        if user:
            with self.engine.connect() as connection:
                stored = connection.execute(text("SELECT password_hash FROM user_credentials WHERE user_id = :id"),
                                            {"id": user.id}).scalar()
        try:
            hasher.verify(stored or NO_ACCOUNT_HASH, password)
        except (VerificationError, InvalidHashError):
            raise IdentityError(BAD_SIGN_IN) from None
        if not user or not stored:
            raise IdentityError(BAD_SIGN_IN)
        if not user.active or not user.organisation_active:
            raise IdentityError(INACTIVE)
        with self.engine.begin() as connection:
            if hasher.check_needs_rehash(stored):  # the hashing settings have been raised since this was stored
                self._store_password(connection, user.id, user.tenant_id, password)
            token, max_age = self._start_session(connection, user.id, user.tenant_id, remember)
        return SignedIn(token, self._principal(user), max_age)

    def verify(self, token: str) -> Principal:
        """Say who a session belongs to. Checked on every request, so ending or suspending takes effect at once."""
        with self.engine.connect() as connection:
            user = connection.execute(text(
                "SELECT u.id, u.tenant_id, u.employee_id, u.role, u.email, u.display_name FROM user_sessions s "
                "JOIN users u ON u.id = s.user_id JOIN organisations o ON o.id = u.tenant_id "
                "WHERE s.token_hash = :hash AND s.revoked_at IS NULL AND s.expires_at > now() AND u.active AND o.active"
            ), {"hash": hash_key(token)}).first()
        if not user:
            raise IdentityError("Please sign in again.")
        return self._principal(user)

    def sign_out(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE user_sessions SET revoked_at = now() WHERE token_hash = :hash AND revoked_at IS NULL"
            ), {"hash": hash_key(token)})

    def issue_setup_code(self, tenant_id: str, user_id: str) -> str | None:
        """Make a one-time code for setting a password, for someone HR added or who has forgotten theirs.

        Only for this organisation's own people, and not for a platform admin, who works across organisations.
        """
        code = new_setup_code()
        with self.engine.begin() as connection:
            allowed = connection.execute(text(
                "SELECT 1 FROM users WHERE id = :id AND tenant_id = :tenant_id AND role <> 'platform_admin'"
            ), {"id": user_id, "tenant_id": tenant_id}).first()
            if not allowed:
                return None
            connection.execute(text(
                "INSERT INTO user_credentials (user_id, tenant_id, setup_code_hash, setup_code_expires_at) "
                "VALUES (:id, :tenant_id, :hash, :expires) ON CONFLICT (user_id) DO UPDATE "
                "SET setup_code_hash = :hash, setup_code_expires_at = :expires, updated_at = now()"
            ), {"id": user_id, "tenant_id": tenant_id, "hash": hash_key(clean_code(code)),
                "expires": datetime.now(UTC) + timedelta(hours=config.SETUP_CODE_HOURS)})
        return code

    def set_with_code(self, email: str, code: str, password: str, remember: bool = False) -> SignedIn:
        """Set a new password with a set-up code. The code then stops working, and so do older sessions."""
        user = self.users.find_by_email(email.strip())
        if not user:
            raise IdentityError(BAD_CODE)
        with self.engine.connect() as connection:
            stored = connection.execute(text(
                "SELECT setup_code_hash FROM user_credentials "
                "WHERE user_id = :id AND setup_code_expires_at > now()"
            ), {"id": user.id}).scalar()
        if not stored or not secrets.compare_digest(stored, hash_key(clean_code(code))):
            raise IdentityError(BAD_CODE)
        if not user.active or not user.organisation_active:
            raise IdentityError(INACTIVE)
        problem = password_problem(password, user.email, user.display_name)
        if problem:
            raise AccountProblem(problem)
        with self.engine.begin() as connection:
            self._store_password(connection, user.id, user.tenant_id, password)
            connection.execute(text("UPDATE user_sessions SET revoked_at = now() "
                                    "WHERE user_id = :id AND revoked_at IS NULL"), {"id": user.id})
            token, max_age = self._start_session(connection, user.id, user.tenant_id, remember)
        return SignedIn(token, self._principal(user), max_age)

    def set_password(self, user_id: str, tenant_id: str, password: str) -> None:
        """Set a password directly. Only for the server's operator: see ops/create_admin.py."""
        with self.engine.begin() as connection:
            self._store_password(connection, user_id, tenant_id, password)

    @staticmethod
    def _store_password(connection, user_id: str, tenant_id: str, password: str) -> None:
        connection.execute(text(
            "INSERT INTO user_credentials (user_id, tenant_id, password_hash) VALUES (:id, :tenant_id, :hash) "
            "ON CONFLICT (user_id) DO UPDATE SET password_hash = :hash, setup_code_hash = NULL, "
            "setup_code_expires_at = NULL, updated_at = now()"
        ), {"id": user_id, "tenant_id": tenant_id, "hash": hasher.hash(password)})

    def purge_expired(self) -> int:
        """Remove sessions that can no longer be used, at startup."""
        with self.engine.begin() as connection:
            return connection.execute(text(
                "DELETE FROM user_sessions WHERE expires_at < now() OR revoked_at IS NOT NULL"
            )).rowcount
