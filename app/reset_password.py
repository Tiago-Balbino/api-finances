import os
import secrets
import sys
from pathlib import Path

from .main import connect, password_hash


def main():
    socket_dir = Path(__file__).resolve().parents[1] / ".pgsocket"
    if not os.getenv("DATABASE_URL") and socket_dir.exists():
        os.environ.setdefault("PGHOST", str(socket_dir))
        os.environ.setdefault("PGPORT", "55432")
    email = sys.argv[1].strip().lower() if len(sys.argv) > 1 else None
    with connect() as db:
        rows = db.execute("SELECT id,email FROM users WHERE email=%s FOR UPDATE" if email else
            "SELECT id,email FROM users WHERE role='admin' FOR UPDATE", (email,) if email else ()).fetchall()
        if len(rows) != 1:
            raise SystemExit("Administrador não encontrado ou ambíguo. Informe o email.")
        password = secrets.token_urlsafe(18)
        db.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash(password), rows[0]["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=%s", (rows[0]["id"],))
    print(f"Email: {rows[0]['email']}")
    print(f"Senha temporária: {password}")
    print("Entre no site e troque a senha em Perfil.")


if __name__ == "__main__":
    main()
